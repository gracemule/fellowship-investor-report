// Instances of the open quarter: start again without losing what was done. The open one stays on screen; earlier ones are kept
// (up to three in all) and can be opened again or, to make room, deleted. A quarter's folder belongs to its open instance, so
// whenever the open instance changes the browser lets go of the folder and it is connected again for the one now open.

import { get, post } from './api.js';
import * as folder from './folder.js';
import { model } from './live.js';
import { follow } from './quarter.js';
import { ago, h, list, plural, toast } from './util.js';

let data = null;          // what the server says: { cap, used, items, can_create, reason }
let ask = null;           // null | { kind: 'new' } | { kind: 'delete', slot }
let busy = false;
let repaint = () => {};

export const onChange = (fn) => { repaint = fn; };
export const cancel = () => { ask = null; };

export async function refresh() {
  try { data = await get('/api/instances'); } catch (e) { data = null; if (e.status !== 401) toast(e.message, 'bad'); }
  repaint();
}

async function act(path, body, then) {
  busy = true; ask = null; repaint();
  try {
    const out = await post(path, { period: model.state.period.code, ...body });
    data = out.list;
    await then(out);
  } catch (e) { if (e.status !== 401) toast(e.message, 'bad'); }
  busy = false; repaint();
}

const reopened = (message) => async (out) => { await folder.forget(); await follow(out.state); toast(message, 'ok'); };

const what = (i) => list([i.files && plural(i.files, 'file'), i.versions && plural(i.versions, 'report version'),
  i.notes && plural(i.notes, 'review note')].filter(Boolean)) || 'nothing yet';
const when = (iso) => (iso ? ago(iso) : '');

function row(i) {
  const confirming = ask?.kind === 'delete' && ask.slot === i.slot;
  const title = i.current ? 'This instance' : `Started ${when(i.started_at)}`;
  const info = h('div', { class: 'inst-info' }, h('b', {}, title), h('small', {}, `${what(i)}${i.last_at ? ` · worked on ${when(i.last_at)}` : ''}`));
  if (i.current) return h('div', { class: 'inst current' }, info, h('span', { class: 'inst-tag' }, 'Open'));
  if (confirming) {
    return h('div', { class: 'inst' }, info,
      h('p', { class: 'inst-ask' }, 'Delete it for good? Its report, versions, notes, conversations and files go. This cannot be undone.'),
      h('div', { class: 'reset-actions' },
        h('button', { class: 'danger-solid', type: 'button', onclick: () => act('/api/instances/delete', { slot: i.slot }, () => toast('Deleted.', 'ok')) }, 'Delete'),
        h('button', { class: 'link', type: 'button', onclick: () => { ask = null; repaint(); } }, 'Cancel')));
  }
  return h('div', { class: 'inst' }, info,
    h('div', { class: 'inst-actions' },
      h('button', { class: 'link', type: 'button', disabled: busy, onclick: () => act('/api/instances/open', { slot: i.slot }, reopened('Opened. Connect its folder again to keep watching it.')) }, 'Open'),
      h('button', { class: 'link danger', type: 'button', disabled: busy, onclick: () => { ask = { kind: 'delete', slot: i.slot }; repaint(); } }, 'Delete')));
}

export function view() {
  if (!data) return h('div', { class: 'insts' }, h('div', { class: 'insts-h' }, 'Instances'), h('p', { class: 'inst-note' }, 'Loading…'));
  const cur = data.items.find((i) => i.current);
  const label = model.state.period.label;
  const make = busy ? h('p', { class: 'inst-note' }, 'Working on it. This takes a few seconds…')
    : ask?.kind === 'new'
    ? h('div', { class: 'reset-ask' },
      h('p', {}, h('b', {}, `Start a new instance of ${label}?`)),
      h('p', {}, `This one is kept (${what(cur)}) and you can open it again from here. The new one starts blank: no files, report, notes or conversation, and you connect the folder again.`),
      h('div', { class: 'reset-actions' },
        h('button', { class: 'primary-sm', type: 'button', onclick: () => act('/api/instances/new', {}, reopened('A new instance. Connect its folder to begin.')) }, 'Start new instance'),
        h('button', { class: 'link', type: 'button', onclick: () => { ask = null; repaint(); } }, 'Cancel')))
    : h('button', { class: 'row newinst', type: 'button', role: 'menuitem', disabled: busy || !data.can_create, onclick: () => { ask = { kind: 'new' }; repaint(); } },
      h('span', {}, h('span', { class: 'l1' }, h('b', {}, 'New instance')),
        h('small', {}, data.can_create ? `Keeps this one and starts blank. ${data.used} of ${data.cap} used.` : data.reason)));
  return h('div', { class: 'insts' }, h('div', { class: 'insts-h' }, `Instances of ${label}`), ...data.items.map(row), make);
}
