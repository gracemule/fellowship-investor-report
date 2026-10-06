// Review notes: what the agent found about the data that a person should decide or check.
// They live here, never inside the report itself, and they do not stay for ever: each can be settled by telling the agent
// more ("I have more information"), or closed by the person ("Resolve"). Settled notes fold away under "Resolved".

import { post } from './api.js';
import { emit, model, refreshNotes, subscribe } from './live.js';
import { ago, clear, h, toast } from './util.js';

const GROUPS = [['decision', 'For your decision'], ['warning', 'To check'], ['info', 'For information']];
let showResolved = false;

const act = async (fn) => { try { await fn(); await refreshNotes(); } catch (e) { if (e.status !== 401) toast(e.message || 'That did not work.', 'bad'); } };

export function mountNotes(root, tabCount) {
  const give = (n) => { model.replyTo = { id: n.id, area: n.area }; emit('reply'); };

  const card = (n) => {
    const answered = n.status === 'answered';
    return h('div', { class: 'note', 'data-sev': n.severity, 'data-status': n.status },
      h('div', { class: 'area' }, n.area),
      h('p', {}, n.text),
      answered ? h('p', { class: 'reply' }, h('b', {}, 'You replied: '), n.reply, h('span', { class: 'small' }, ' · the agent is using it; it closes the note when it is settled.')) : null,
      h('div', { class: 'note-actions' },
        h('button', { class: 'link', type: 'button', onclick: () => give(n) }, answered ? 'Add more information' : 'I have more information'),
        h('button', { class: 'link', type: 'button', onclick: () => act(() => post(`/api/notes/${n.id}/resolve`, {})) }, 'Resolve')));
  };

  const done = (n) => h('div', { class: 'note resolved', 'data-sev': n.severity },
    h('div', { class: 'area' }, n.area, h('span', { class: 'when' }, ` · resolved ${ago(n.resolved_at)}`)),
    h('p', {}, n.text),
    n.resolution ? h('p', { class: 'reply' }, n.resolution) : null,
    h('div', { class: 'note-actions' }, h('button', { class: 'link', type: 'button', onclick: () => act(() => post(`/api/notes/${n.id}/reopen`)) }, 'Reopen')));

  const render = () => {
    const notes = model.notes;
    if (!notes) return;
    const open = notes.filter((n) => n.status !== 'resolved');
    const resolved = notes.filter((n) => n.status === 'resolved');
    const decisions = open.filter((n) => n.severity === 'decision').length;
    tabCount.textContent = decisions ? String(decisions) : '';
    const keep = root.scrollTop;
    clear(root);
    if (!open.length) root.append(h('p', { class: 'empty-note' }, notes.length
      ? 'Everything here is settled. New notes appear when the agent finds a gap or a disagreement in the data; they are kept out of the report.'
      : 'Nothing needs your review. When the agent finds a gap or a disagreement in the data, it is noted here and kept out of the report.'));
    for (const [sev, title] of GROUPS) {
      const items = open.filter((n) => n.severity === sev);
      if (!items.length) continue;
      root.append(h('div', { class: 'group', 'data-g': sev === 'decision' ? 'needed' : '' }, h('h3', {}, `${title} · ${items.length}`), items.map(card)));
    }
    if (resolved.length) {
      root.append(h('div', { class: 'group resolved-group' },
        h('button', { class: 'fold', type: 'button', 'aria-expanded': String(showResolved), onclick: () => { showResolved = !showResolved; render(); } },
          showResolved ? 'Hide resolved' : `Resolved · ${resolved.length}`),
        showResolved ? resolved.map(done) : null));
    }
    root.scrollTop = keep;
  };
  subscribe((what) => { if (what === 'notes' || what === 'quarter') render(); });
  render();
}
