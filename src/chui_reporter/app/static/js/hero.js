// The top of the rail: one headline, one sentence, and at most one thing to do.
//
// This is where Hick's law is applied hardest. The interface asks "what does this person need to
// do next?" and shows exactly that: a question to answer, a folder to choose, a button to press,
// or nothing at all. Everything else lives behind the tabs.

import { post } from './api.js';
import * as folder from './folder.js';
import { model, subscribe } from './live.js';
import { $, ICON, ago, clear, h, list, plural, svg, toast } from './util.js';

const SYNCING = new Set(['scanning', 'hashing', 'uploading', 'committing']);

const run = async (fn) => { try { await fn(); } catch (e) { if (e.status !== 401) toast(e.message || 'Something went wrong.', 'bad'); } };
const primary = (label, onclick) => h('button', { class: 'primary', type: 'button', onclick: () => run(onclick) }, label, svg(ICON.arrow, { size: 16 }));
const link = (label, onclick) => h('button', { class: 'link', type: 'button', onclick: () => run(onclick) }, label);
const eyebrow = (text, tone, extra) => h('div', { class: 'eyebrow' }, h('span', { class: 'dot', 'data-s': tone }), text, extra ? h('span', { class: 'eb-end' }, extra) : null);

const pickOrReconnect = () => (model.folder.needsPermission ? folder.reconnect() : folder.pick());

function needsList(items) {
  return h('ul', { class: 'needs' }, items.map((c) => h('li', {}, h('b', {}, c.label),
    c.hint, ' ', h('code', {}, c.folder + '/'))));
}

function questionView(q, S) {
  const ask = q.kind === 'sources';
  if (ask) {
    const slots = (S.coverage || []).filter((c) => (q.slots || []).includes(c.id));
    return { key: 'q:' + q.id, nodes: [
      eyebrow('Waiting for files', 'ask'),
      h('h1', { class: 'headline small' }, `The agent needs ${list(slots.map((s) => s.label.toLowerCase()))}`),
      q.why && h('p', { class: 'detail' }, q.why),
      needsList(slots),
      h('p', { class: 'detail faint' }, model.folder.name
        ? `Add ${slots.length > 1 ? 'them' : 'it'} to “${model.folder.name}”. The agent carries on by itself when ${slots.length > 1 ? 'they appear' : 'it appears'}.`
        : 'Choose your folder, or attach them in the box below.'),
      h('div', { class: 'actions' },
        model.folder.name ? link('Check the folder now', () => folder.checkNow()) : primary('Choose folder', pickOrReconnect),
        link('Continue without', () => post(`/api/questions/${q.id}/answer`, { skip: true }))),
    ] };
  }
  const send = (a) => run(() => post(`/api/questions/${q.id}/answer`, { answer: a }));
  return { key: 'q:' + q.id, nodes: [
    eyebrow('A question for you', 'ask'),
    h('h1', { class: 'headline small' }, q.prompt),
    q.why && h('p', { class: 'detail' }, q.why),
    (q.options || []).length ? h('div', { class: 'choices' }, q.options.map((o) =>
      h('button', { class: 'choice', type: 'button', onclick: () => send(o) }, h('span', {}, o), svg(ICON.arrow, { size: 16 })))) : null,
    h('p', { class: 'detail faint' }, (q.options || []).length ? 'Or type your own answer in the box below.' : 'Type your answer in the box below.'),
    h('div', { class: 'actions' }, link('Decide for me', () => post(`/api/questions/${q.id}/answer`, { skip: true }))),
  ] };
}

function syncView() {
  const f = model.folder, p = f.progress;
  const label = { scanning: 'Looking through your folder', hashing: 'Reading your files', uploading: 'Sending new files',
                  committing: 'Saving' }[f.phase];
  return { key: 'sync:' + f.phase, nodes: [
    eyebrow('Syncing', 'working'),
    h('h1', { class: 'headline' }, label),
    h('p', { class: 'detail', id: 'hero-detail' }, syncDetail()),
    h('div', { class: 'bar' + (p && p.total ? '' : ' indet'), id: 'hero-bar' }, h('i', { style: { width: p && p.total ? `${(p.done / p.total) * 100}%` : '' } })),
  ] };
}
const syncDetail = () => {
  const p = model.folder.progress;
  if (!p) return model.folder.name ? `“${model.folder.name}”` : '';
  return `${p.done} of ${plural(p.total, 'file')}${p.current ? ' · ' + p.current.split('/').pop() : ''}`;
};

function statusView(S) {
  const st = S.status, f = model.folder;
  const files = S.workspace.files;
  const offline = !model.online;
  const tone = { working: 'working', current: 'ok', attention: 'bad', waiting_user: 'ask', waiting_data: 'ask', stale: 'ask', ready: 'ask' }[st.phase] || '';
  const eb = offline ? 'Reconnecting' : { empty: 'Get started', needs_sources: 'Almost there', ready: 'Ready', working: 'Working',
    stale: 'Folder changed', attention: 'Needs attention', current: 'Up to date', waiting_data: 'Waiting', waiting_user: 'Waiting' }[st.phase] || '';
  const nodes = [eyebrow(eb, offline ? 'bad' : tone), h('h1', { class: 'headline' }, st.headline)];
  let detail = st.detail;
  if (st.phase === 'empty' && !f.supported) detail += ' Chrome or Edge keep it connected for you; here you will need to choose it again to check for changes.';
  if (st.phase === 'empty' && f.needsPermission) detail = `Reconnect to “${f.name}” so the agent can read it again.`;
  if (st.phase === 'current' && S.version) detail = `Version ${S.version.version} · ${plural(S.version.pages, 'page')} · ${ago(S.version.created_at)}`;
  if (st.phase === 'stale' && S.pending?.sections?.length) detail = `The report does not yet reflect the new files. They affect ${list(S.pending.sections)}.`;
  if (detail) nodes.push(h('p', { class: 'detail', id: 'hero-detail' }, detail));

  if (st.phase === 'needs_sources') {
    nodes.push(needsList((S.coverage || []).filter((c) => c.state === 'missing')));
    if (f.name && !f.needsPermission) nodes.push(h('p', { class: 'detail faint' }, `Add them to “${f.name}” and they will be picked up on their own.`));
  }
  if (st.phase === 'working') nodes.push(h('div', { class: 'bar indet' }, h('i')));

  const actions = h('div', { class: 'actions' });
  const a = st.action;
  const connected = f.name && !f.needsPermission;
  if (a?.id === 'pick') {
    if (!connected || st.phase === 'empty') actions.append(primary(f.needsPermission ? `Reconnect to ${f.name}` : 'Choose folder', pickOrReconnect));
    else actions.append(link('Check the folder now', () => folder.checkNow()));
  } else if (a?.id === 'stop') { /* the send button becomes Stop while the agent works */ }
  else if (a) actions.append(primary(a.label, () => post(a.id === 'resume' ? '/api/run/resume' : '/api/run', {})));
  if (st.phase === 'current' && connected) actions.append(link('Check the folder now', () => folder.checkNow()));
  if (f.needsPermission && st.phase !== 'empty') actions.append(link(`Reconnect to ${f.name}`, () => folder.reconnect()));
  if (actions.childElementCount) nodes.push(actions);
  return { key: `s:${st.phase}:${st.headline}:${detail}:${a?.id}:${offline}:${connected}:${files ? 1 : 0}:${f.needsPermission}`, nodes };
}

export function mountHero(root) {
  let key = '';
  const render = () => {
    const S = model.state;
    if (!S) return;
    const f = model.folder;
    const q = S.questions?.[0];
    const view = q ? questionView(q, S) : SYNCING.has(f.phase) ? syncView() : statusView(S);
    if (view.key === key) { patchProgress(); return; }
    key = view.key;
    clear(root).append(...view.nodes.filter(Boolean));
    root.classList.remove('swap'); void root.offsetWidth; root.classList.add('swap');
    if (view.focus && document.activeElement === document.body) view.focus.focus({ preventScroll: true });
  };
  const patchProgress = () => {
    if (!SYNCING.has(model.folder.phase)) return;
    const d = $('#hero-detail', root), b = $('#hero-bar i', root), p = model.folder.progress;
    if (d) d.textContent = syncDetail();
    if (b && p?.total) b.style.width = `${(p.done / p.total) * 100}%`;
  };
  subscribe((what) => { if (what === 'state' || what === 'folder' || what === 'online') render(); });
  setInterval(() => { if (model.state?.status.phase === 'current') { key = ''; render(); } }, 30000);   // "4 min ago" stays true
  render();
}
