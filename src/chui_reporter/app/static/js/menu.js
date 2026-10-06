// Quarter, automatic updating, folder and sign-out: everything that is a setting, kept behind one
// quiet control so it never competes with the work.

import { post } from './api.js';
import * as folder from './folder.js';
import { emit, model, subscribe } from './live.js';
import { setQuarter } from './quarter.js';
import { ICON, clear, h, svg, toast } from './util.js';

export function mountMenu({ button, menu, onSignOut }) {
  const setPeriod = async (code) => { close(); await setQuarter(code); };
  const close = () => { menu.hidden = true; button.setAttribute('aria-expanded', 'false'); };

  function render() {
    const S = model.state;
    if (!S) return;
    button.replaceChildren(h('span', {}, S.period.label), svg(ICON.down, { size: 13 }));
    const auto = S.workspace.auto;
    clear(menu).append(
      h('div', { class: 'row' }, h('span', {}, 'Quarter'),
        h('span', { class: 'stepper' },
          h('button', { type: 'button', 'aria-label': 'Previous quarter', onclick: () => setPeriod(S.period.prev) }, svg(ICON.chev, { size: 14, cls: 'flip' })),
          h('b', {}, S.period.label),
          h('button', { type: 'button', 'aria-label': 'Next quarter', onclick: () => setPeriod(S.period.next) }, svg(ICON.chev, { size: 14 })))),
      h('button', { class: 'row newq', type: 'button', role: 'menuitem', onclick: () => setPeriod(S.period.next_code) },
        h('span', {}, h('span', { class: 'l1' }, h('b', {}, `Start ${S.period.next}`)),
          h('small', {}, 'A clean slate: its own folder, report and notes. Only the brand kit carries over.')), svg(ICON.plus, { size: 15 })),
      h('hr'),
      h('button', { class: 'row', type: 'button', role: 'menuitemcheckbox', 'aria-checked': String(auto),
        onclick: async () => { model.state = await post('/api/settings', { auto: !auto }); emit('state'); } },
        h('span', {}, 'Update automatically'), h('span', { class: 'switch-ctl', role: 'switch', 'aria-checked': String(auto) })),
      h('hr'),
      h('button', { class: 'row', type: 'button', role: 'menuitem', onclick: () => { close(); folder.pick(); } }, h('span', {}, model.folder.name ? 'Change folder' : 'Choose folder')),
      h('button', { class: 'row', type: 'button', role: 'menuitem', onclick: () => { close(); onSignOut(); } }, h('span', {}, 'Sign out')));
  }
  button.addEventListener('click', (e) => { e.stopPropagation(); const open = menu.hidden; menu.hidden = !open; button.setAttribute('aria-expanded', String(open)); });
  document.addEventListener('click', (e) => { if (!menu.contains(e.target)) close(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
  subscribe((what) => { if (what === 'state' || (what === 'folder' && ['none', 'watching'].includes(model.folder.phase))) render(); });
  render();
}
