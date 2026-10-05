// Review notes: what the agent found about the data that a person should decide or check.
// They live here, never inside the report itself.

import { model, subscribe } from './live.js';
import { clear, h } from './util.js';

const GROUPS = [['decision', 'For your decision'], ['warning', 'To check'], ['info', 'For information']];

export function mountNotes(root, tabCount) {
  const render = () => {
    const notes = model.notes;
    if (!notes) return;
    const decisions = notes.filter((n) => n.severity === 'decision').length;
    tabCount.textContent = decisions ? String(decisions) : '';
    clear(root);
    if (!notes.length) { root.append(h('p', { class: 'empty-note' }, 'Nothing needs your review. When the agent finds a gap or a disagreement in the data, it is noted here and kept out of the report.')); return; }
    for (const [sev, title] of GROUPS) {
      const items = notes.filter((n) => n.severity === sev);
      if (!items.length) continue;
      root.append(h('div', { class: 'group', 'data-g': sev === 'decision' ? 'needed' : '' }, h('h3', {}, `${title} · ${items.length}`),
        items.map((n) => h('div', { class: 'note', 'data-sev': sev }, h('div', { class: 'area' }, n.area), h('p', {}, n.text)))));
    }
  };
  subscribe((what) => { if (what === 'notes') render(); });
  render();
}
