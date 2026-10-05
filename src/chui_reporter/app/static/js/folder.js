// The user's folder, kept in step with the server.
//
// The agent runs on a server and cannot see a laptop, so this module is the bridge. In Chromium
// browsers the folder is chosen once with the File System Access API; the handle is remembered,
// and the folder is re-scanned every few seconds while the tab is open. Only files whose size
// or modified time changed are re-hashed, and only files the server does not already hold are
// uploaded. Elsewhere (Safari, Firefox) the same flow runs from a one-off folder selection.

import { post, put } from './api.js';
import { emit, model } from './live.js';
import { toast } from './util.js';

const MAX_BYTES = 60 * 1024 * 1024;
const WATCH_MS = 15000;
const SETTLE_MS = 2500;                    // a file saved this recently may still be being written
const ignored = (n) => n.startsWith('.') || n.startsWith('~$') || /^(thumbs\.db|desktop\.ini)$/i.test(n);

// ---- remembered handle (IndexedDB) ----------------------------------------------------------
const db = () => new Promise((res, rej) => {
  const r = indexedDB.open('chui', 1);
  r.onupgradeneeded = () => r.result.createObjectStore('kv');
  r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
});
async function kv(mode, fn) {
  try { const d = await db(); return await new Promise((res, rej) => { const t = d.transaction('kv', mode); const q = fn(t.objectStore('kv')); t.oncomplete = () => res(q?.result); t.onerror = () => rej(t.error); }); }
  catch { return undefined; }
}
const kvGet = (k) => kv('readonly', (s) => s.get(k));
const kvSet = (k, v) => kv('readwrite', (s) => s.put(v, k));
const kvDel = (k) => kv('readwrite', (s) => s.delete(k));

// ---- sources: a real directory handle, or a one-off FileList ---------------------------------
async function* walkHandle(dir, prefix = '', depth = 0) {
  if (depth > 8) return;
  for await (const [name, h] of dir.entries()) {
    if (ignored(name)) continue;
    if (h.kind === 'directory') { yield* walkHandle(h, `${prefix}${name}/`, depth + 1); continue; }
    let f; try { f = await h.getFile(); } catch { continue; }
    yield { path: prefix + name, size: f.size, mtime: f.lastModified, file: async () => (await h.getFile()) };
  }
}
const fsSource = (handle) => ({ name: handle.name, watch: true, walk: () => walkHandle(handle) });
const listSource = (files) => {
  const first = files[0]?.webkitRelativePath.split('/')[0] || 'Folder';
  return { name: first, watch: false, async *walk() {
    for (const f of files) {
      const parts = f.webkitRelativePath.split('/').slice(1);
      if (!parts.length || parts.some(ignored)) continue;
      yield { path: parts.join('/'), size: f.size, mtime: f.lastModified, file: async () => f };
    }
  } };
};

// ---- state ------------------------------------------------------------------------------------
let source = null, handle = null, busy = false, timer = null, again = false;
let hashes = new Map();                    // path -> { size, mtime, sha }
const F = model.folder;
const set = (patch) => { Object.assign(F, patch); emit('folder'); };

async function sha256(file) {
  const buf = await file.arrayBuffer();
  const d = new Uint8Array(await crypto.subtle.digest('SHA-256', buf));
  return [...d].map((b) => b.toString(16).padStart(2, '0')).join('');
}
async function pool(items, n, fn) {
  let i = 0;
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, async () => {
    while (i < items.length) { const item = items[i++]; await fn(item); }
  }));
}

export async function syncNow({ force = false } = {}) {
  if (!source) return;
  if (busy) { again = true; return; }
  busy = true;
  try {
    set({ phase: 'scanning', error: null, progress: null });
    const entries = [];
    for await (const e of source.walk()) { entries.push(e); if (entries.length > 5000) throw new Error('This folder has more than 5,000 files.'); }
    const seen = new Set(entries.map((e) => e.path));
    const changed = entries.filter((e) => { const c = hashes.get(e.path); return !c || c.size !== e.size || c.mtime !== e.mtime; });
    const removed = [...hashes.keys()].filter((p) => !seen.has(p));

    if (!force && !changed.length && !removed.length) { set({ phase: source.watch ? 'watching' : 'manual', checkedAt: Date.now() }); return; }
    if (changed.some((e) => Date.now() - e.mtime < SETTLE_MS)) {       // still being saved: look again shortly
      set({ phase: 'watching', checkedAt: Date.now() }); setTimeout(() => syncNow(), SETTLE_MS + 500); return;
    }

    const tooLarge = [];
    let done = 0;
    set({ phase: 'hashing', progress: { done, total: changed.length } });
    await pool(changed, 4, async (e) => {
      if (e.size > MAX_BYTES) { tooLarge.push(e.path); hashes.set(e.path, { size: e.size, mtime: e.mtime, sha: null }); }
      else hashes.set(e.path, { size: e.size, mtime: e.mtime, sha: await sha256(await e.file()) });
      set({ progress: { done: ++done, total: changed.length, current: e.path } });
    });
    for (const p of removed) hashes.delete(p);

    const byPath = new Map(entries.map((e) => [e.path, e]));
    const manifest = entries.filter((e) => hashes.get(e.path)?.sha)
      .map((e) => ({ path: e.path, size: e.size, mtime: e.mtime, sha256: hashes.get(e.path).sha }));
    const plan = await post('/api/sync/plan', { manifest });
    const need = plan.need;
    if (need.length) {
      let sent = 0;
      set({ phase: 'uploading', progress: { done: 0, total: need.length } });
      await pool(need, 3, async (path) => {
        const e = byPath.get(path), f = await e.file();
        await put(`/api/sync/file?path=${encodeURIComponent(path)}&sha256=${hashes.get(path).sha}&mtime=${e.mtime}`, f);
        set({ progress: { done: ++sent, total: need.length, current: path } });
      });
    }
    set({ phase: 'committing', progress: null });
    const res = await post('/api/sync/commit', { manifest });
    persistHashes();
    set({ phase: source.watch ? 'watching' : 'manual', checkedAt: Date.now(), tooLarge: [...tooLarge, ...(plan.too_large || [])] });
    if (tooLarge.length) toast(`${tooLarge.length} file${tooLarge.length > 1 ? 's are' : ' is'} over 60 MB and was skipped.`, 'warn');
    emit('synced', res);
  } catch (err) {
    set({ phase: 'error', error: err.message || String(err) });
    if (err.status !== 401) toast(`Could not sync your folder: ${err.message}`, 'bad');
  } finally {
    busy = false;
    if (again) { again = false; setTimeout(() => syncNow(), 400); }
  }
}

const persistHashes = () => kvSet('hashes', Object.fromEntries(hashes));

function startWatching() {
  clearInterval(timer);
  if (!source?.watch) return;
  timer = setInterval(() => { if (!document.hidden) syncNow(); }, WATCH_MS);
}

async function attach(src, h = null, { force = true } = {}) {
  source = src; handle = h;
  set({ name: src.name, needsPermission: false, phase: 'scanning' });
  startWatching();
  await syncNow({ force });
}

// ---- public -----------------------------------------------------------------------------------
export async function init() {
  document.addEventListener('visibilitychange', () => { if (!document.hidden) syncNow(); });
  addEventListener('focus', () => syncNow());
  if (!F.supported) return;
  const saved = await kvGet('handle');
  if (!saved) return;
  const stored = await kvGet('hashes'); if (stored) hashes = new Map(Object.entries(stored));
  let perm = 'prompt';
  try { perm = await saved.queryPermission({ mode: 'read' }); } catch { /* handle no longer valid */ }
  if (perm === 'granted') { handle = saved; await attach(fsSource(saved), saved, { force: false }); }
  else { handle = saved; set({ name: saved.name, needsPermission: true, phase: 'needs_permission' }); }
}

export async function pick() {
  if (new URLSearchParams(location.search).has('mock')) {
    const { mockSource } = await import('./dev-mock.js');
    hashes = new Map(); return attach(await mockSource());
  }
  if (F.supported) {
    let h;
    try { h = await showDirectoryPicker({ id: 'chui-quarter', mode: 'read' }); }
    catch (e) { if (e.name === 'AbortError') return; throw e; }
    hashes = new Map();
    await kvSet('handle', h);
    return attach(fsSource(h), h);
  }
  const input = Object.assign(document.createElement('input'), { type: 'file', multiple: true, webkitdirectory: true });
  input.onchange = () => { const files = [...input.files]; if (files.length) { hashes = new Map(); attach(listSource(files)); } };
  input.click();
}

export async function reconnect() {
  if (!handle) return pick();
  const perm = await handle.requestPermission({ mode: 'read' });
  if (perm === 'granted') return attach(fsSource(handle), handle, { force: false });
  toast('Access to the folder was not granted.', 'warn');
}

export const checkNow = () => (source ? syncNow({ force: false }) : pick());

export async function forget() {
  clearInterval(timer); source = null; handle = null; hashes = new Map();
  await kvDel('handle'); await kvDel('hashes');
  set({ name: null, phase: 'none', needsPermission: false });
}
