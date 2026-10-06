// The one place the user talks to the agent.
//
// It behaves the way people now expect an agent's input to behave: it grows as you type, Enter sends
// and Shift+Enter breaks the line, files can be attached (button, drag-and-drop or paste), and the
// send button is also the stop button. What a message *means* depends on what the agent is doing:
//
//   a question is open  -> it is the answer
//   the agent is working -> it is steering, delivered at the next step boundary (shown as queued)
//   the agent is idle    -> it is a message to the agent: it answers, or does what is asked
//   a review note chosen -> it is more information about that note (the agent uses it and resolves the note)

import { post } from './api.js';
import { model, refreshNotes, subscribe } from './live.js';
import { $, ICON, clear, h, svg, toast } from './util.js';

const MAX_FILES = 8;
const MAX_BYTES = 60 * 1024 * 1024;
const OK_EXT = /\.(pdf|xlsx|xlsm|xls|docx|csv|txt|md|json|png|jpg|jpeg|webp|gif)$/i;
const IMG = /\.(png|jpe?g|webp|gif)$/i;
const MAX_H = 216;
const coarse = matchMedia('(pointer: coarse)').matches;

export function mountComposer() {
  const form = $('#composer'), box = $('.cbox', form), input = $('#say'), send = $('#send'), attach = $('#attach');
  const chips = $('#chips'), file = $('#file'), hint = $('#hint'), rail = $('.rail');
  attach.append(svg('M21 11.5l-8.6 8.6a5.5 5.5 0 0 1-7.8-7.8l9-9a3.7 3.7 0 0 1 5.2 5.2l-9 9a1.8 1.8 0 0 1-2.6-2.6l8.4-8.4', { size: 18, stroke: 1.6 }));
  if (coarse) hint.hidden = true;

  let files = [];            // { id, name, kind, status: uploading|ready, path, thumb }
  let queued = null;         // { id, text }
  let n = 0;

  // ---- what the box is for right now -------------------------------------------------------
  const ctx = () => {
    const S = model.state;
    const q = S?.questions?.[0];
    const busy = !!S?.run && ['queued', 'running'].includes(S.run.status);
    return { S, q, busy, hasReport: !!S?.version };
  };
  const placeholder = () => {
    const { q, busy, hasReport } = ctx();
    if (past()) return 'Read-only. Start a new session to continue.';
    if (model.replyTo) return 'Give the agent more information about this note…';
    if (q) return q.kind === 'sources' ? 'Add a note, or attach the files…' : 'Type your answer…';
    if (busy) return 'Steer the agent…';
    if (hasReport) return 'Ask for a change to the report…';
    return 'Message the agent, or attach files…';
  };
  const past = () => !!model.viewingSession;
  const hasContent = () => !!input.value.trim() || files.some((f) => f.status === 'ready');
  const mode = () => (past() ? 'idle' : hasContent() ? 'send' : ctx().busy ? 'stop' : 'idle');

  function paint() {
    const m = mode();
    send.dataset.mode = m;
    clear(send).append(m === 'stop' ? svg(ICON.stop, { size: 12, stroke: 0 }) : svg(ICON.up, { size: 16, stroke: 2.1 }));
    if (m === 'stop') send.firstChild.setAttribute('fill', 'currentColor');
    send.setAttribute('aria-label', m === 'stop' ? 'Stop' : 'Send');
    send.title = m === 'stop' ? 'Stop (after the current step)' : m === 'send' ? 'Send' : '';
    input.placeholder = placeholder();
    input.disabled = past(); attach.disabled = past(); box.classList.toggle('past', past());
    hint.textContent = ctx().busy ? 'Enter to send · applied after the current step' : 'Enter to send · Shift+Enter for a new line';
    hint.classList.toggle('on', !!input.value.trim() && !coarse);
    renderChips();
  }

  function grow() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, MAX_H) + 'px';
    box.classList.toggle('tall', input.scrollHeight > 52);
  }

  // ---- attachments ---------------------------------------------------------------------------
  function renderChips() {
    clear(chips);
    if (model.replyTo) chips.append(h('span', { class: 'chip reply', title: 'Your next message is about this review note' },
      h('span', { class: 'nm' }, `About: ${model.replyTo.area}`),
      h('button', { type: 'button', 'aria-label': 'Stop replying to this note', onclick: () => { model.replyTo = null; paint(); } }, '✕')));
    if (queued) chips.append(h('span', { class: 'chip queued', title: queued.text },
      h('span', { class: 'dot', 'data-s': 'working' }), h('span', { class: 'nm' }, `Queued · ${queued.text}`)));
    for (const f of files) {
      chips.append(h('span', { class: 'chip', 'data-s': f.status },
        f.thumb ? h('img', { src: f.thumb, alt: '' }) : svg(ICON.doc, { size: 14 }),
        h('span', { class: 'nm', title: f.name }, f.name),
        f.status === 'uploading' ? h('span', { class: 'dot', 'data-s': 'working' }) : null,
        h('button', { type: 'button', 'aria-label': `Remove ${f.name}`, onclick: () => removeFile(f) }, '✕')));
    }
    chips.hidden = !chips.childElementCount;
  }

  async function addFiles(list) {
    for (const f of [...list]) {
      if (files.length >= MAX_FILES) { toast(`You can attach up to ${MAX_FILES} files at a time.`, 'warn'); break; }
      if (!OK_EXT.test(f.name)) { toast(`${f.name}: that kind of file can't be attached. PDF, Excel, Word, CSV, text and images can.`, 'warn'); continue; }
      if (f.size > MAX_BYTES) { toast(`${f.name} is over 60 MB.`, 'warn'); continue; }
      const entry = { id: ++n, name: f.name, kind: IMG.test(f.name) ? 'image' : 'document', status: 'uploading', thumb: IMG.test(f.name) ? URL.createObjectURL(f) : null };
      files.push(entry); paint();
      try {
        const res = await fetch(`/api/attachments?name=${encodeURIComponent(f.name)}`, { method: 'PUT', credentials: 'same-origin', body: f });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(body.detail || `Upload failed (${res.status})`);
        Object.assign(entry, { status: 'ready', path: body.path, name: body.name });
      } catch (e) {
        files = files.filter((x) => x !== entry); toast(e.message, 'bad');
      }
      paint();
    }
    input.focus();
  }

  async function removeFile(f) {
    files = files.filter((x) => x !== f); paint();
    if (f.path) fetch(`/api/attachments?path=${encodeURIComponent(f.path)}`, { method: 'DELETE', credentials: 'same-origin' }).catch(() => {});
  }

  attach.addEventListener('click', () => file.click());
  file.addEventListener('change', () => { addFiles(file.files); file.value = ''; });
  input.addEventListener('paste', (e) => {
    const pasted = [...(e.clipboardData?.files || [])];
    if (pasted.length) { e.preventDefault(); addFiles(pasted.map((f) => (f.name && f.name !== 'image.png' ? f : new File([f], `pasted-image-${Date.now()}.${(f.type.split('/')[1] || 'png')}`, { type: f.type })))); }
  });
  let depth = 0;
  const dragging = (on) => rail.classList.toggle('drop', on);
  rail.addEventListener('dragenter', (e) => { if ([...(e.dataTransfer?.types || [])].includes('Files')) { depth++; dragging(true); } });
  rail.addEventListener('dragleave', () => { depth = Math.max(0, depth - 1); if (!depth) dragging(false); });
  rail.addEventListener('dragover', (e) => { if ([...(e.dataTransfer?.types || [])].includes('Files')) e.preventDefault(); });
  rail.addEventListener('drop', (e) => { e.preventDefault(); depth = 0; dragging(false); if (e.dataTransfer?.files?.length) addFiles(e.dataTransfer.files); });

  // ---- sending -----------------------------------------------------------------------------
  async function submit() {
    const m = mode();
    if (m === 'idle') return;
    if (m === 'stop') {
      try { await post('/api/run/stop'); toast('Stopping after the current step…'); } catch (e) { toast(e.message, 'bad'); }
      return;
    }
    if (files.some((f) => f.status === 'uploading')) { toast('Still uploading your files…', 'info'); return; }
    const text = input.value.trim();
    const attachments = files.map((f) => f.path);
    const { q, busy, hasReport } = ctx();
    send.disabled = true;
    try {
      let r = { ok: true };
      if (q) await post(`/api/questions/${q.id}/answer`, { answer: text, attachments });
      else if (!text && !hasReport && !busy && !model.replyTo) await post('/api/attachments/commit', { attachments });   // files alone join the sources
      else {
        r = await post('/api/steer', { text, attachments, note: model.replyTo?.id });
        if (r.ok === false) { toast('That could not be sent.', 'warn'); return; }
        if (r.applied === 'next_step') queued = { id: r.id, text: text || `${attachments.length} file${attachments.length > 1 ? 's' : ''}` };
        if (model.replyTo) { model.replyTo = null; refreshNotes().catch(() => {}); }
      }
      input.value = ''; files = []; grow();
    } catch (e) { toast(e.message, 'bad'); }
    finally { send.disabled = false; paint(); }
  }

  form.addEventListener('submit', (e) => { e.preventDefault(); submit(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && !coarse) { e.preventDefault(); submit(); }
  });
  input.addEventListener('input', () => { grow(); paint(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === '/' && !['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName)) { e.preventDefault(); input.focus(); }
  });

  subscribe((what, data) => {
    if (what === 'reply') { paint(); input.focus(); }
    if (what === 'quarter') { queued = null; files = []; input.value = ''; grow(); paint(); }
    if (what === 'state' || what === 'session-view') { if (what === 'session-view') { queued = null; } paint(); }
    if (what === 'event' && ['steer.applied', 'steer.dropped'].includes(data.ev.kind) && queued && data.ev.detail?.id === queued.id) { queued = null; paint(); }
    if (what === 'event' && data.ev.kind === 'run.end') { queued = null; paint(); }
  });
  grow(); paint();
}
