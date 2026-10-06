// The browser's copy of what the server says is happening: the latest state, plus the ordered
// event stream. Components subscribe; nothing else holds truth.

import { get } from './api.js';
import { debounce } from './util.js';

export const model = {
  viewingSession: null,                 // null = the current session; otherwise an earlier one's id
  replyTo: null,                        // the review note the next message is about: { id, area }
  state: null, report: null, notes: null, events: [], lastId: 0, online: true,
  folder: { name: null, supported: 'showDirectoryPicker' in window, phase: 'none', checkedAt: null,
            progress: null, error: null, needsPermission: false },
};

const subs = new Set();
export const subscribe = (fn) => { subs.add(fn); return () => subs.delete(fn); };
export const emit = (what, data) => subs.forEach((f) => f(what, data));

const STATEFUL = new Set(['run.queued', 'run.start', 'run.end', 'question', 'question.answered', 'source.sync',
  'version', 'waiting.sources', 'period', 'recovered', 'steer', 'source.cleared', 'source.moved']);

export async function refreshState() {
  model.state = await get('/api/state');
  emit('state');
  return model.state;
}
const refreshSoon = debounce(() => refreshState().catch(() => {}), 220);

export async function refreshReport() {
  model.report = await get('/api/report');
  emit('report');
  return model.report;
}
export async function refreshNotes() {
  model.notes = (await get('/api/notes')).notes;
  emit('notes');
}

export function ingest(ev, { live = true } = {}) {
  if (ev.id <= model.lastId) return;
  model.lastId = ev.id;
  model.events.push(ev);
  if (model.events.length > 800) model.events.splice(0, 200);
  emit('event', { ev, live });
  if (!live) return;
  if (STATEFUL.has(ev.kind)) refreshSoon();
  if (ev.kind === 'version') { refreshReport().catch(() => {}); refreshNotes().catch(() => {}); }
  if (ev.kind === 'step.done' && ['report_review_note', 'report_resolve_review_note', 'report_remove_review_note', 'build_macro_table'].includes(ev.detail?.tool)) refreshNotes().catch(() => {});
  if (ev.kind === 'run.end') refreshNotes().catch(() => {});
}

let es;
export function connect() {
  es?.close();
  es = new EventSource(`/api/events?after=${model.lastId}`);
  es.onopen = () => { if (!model.online) { model.online = true; emit('online'); refreshState().catch(() => {}); } };
  es.onmessage = (m) => { try { ingest(JSON.parse(m.data)); } catch { /* ignore a malformed frame */ } };
  es.onerror = () => { if (model.online) { model.online = false; emit('online'); } };
}

export async function start() {
  await refreshState();
  const hist = await get('/api/history?n=300');
  hist.events.forEach((e) => ingest(e, { live: false }));
  emit('hydrated');
  await Promise.allSettled([refreshReport(), refreshNotes()]);
  connect();
  setInterval(() => refreshState().catch(() => {}), 20000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshState().catch(() => {}); });
}
