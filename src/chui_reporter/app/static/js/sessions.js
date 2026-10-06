// Sessions: a session is one conversation with the agent. The report, its figures and its versions belong
// to the quarter and carry across sessions; a new session only gives the agent a clean working memory and
// a clean activity feed. Earlier sessions stay readable here.

import { get, post } from './api.js';
import { emit, model, subscribe } from './live.js';
import { $, ICON, ago, clear, h, svg, toast } from './util.js';

export function mountSessions({ button, menu, activity }) {
  const close = () => { menu.hidden = true; button.setAttribute('aria-expanded', 'false'); };

  button.append(svg(ICON.clock, { size: 15 }), h('span', { class: 'hide-s' }, 'Sessions'));

  async function open() {
    clear(menu).append(h('div', { class: 'row small' }, 'Loading…'));
    menu.hidden = false; button.setAttribute('aria-expanded', 'true');
    let data;
    try { data = await get('/api/sessions'); } catch (e) { toast(e.message, 'bad'); close(); return; }
    clear(menu).append(
      h('button', { class: 'row newrow', type: 'button', onclick: newSession }, h('span', {}, 'New session'), svg(ICON.plus, { size: 15 })),
      h('hr'),
      ...data.sessions.map((s) => h('button', { class: 'row srow' + (s.id === (model.viewingSession || data.current) ? ' on' : ''), type: 'button',
        onclick: () => { close(); activity.show(s.current ? null : s.id, s); } },
        h('span', {}, h('span', { class: 'l1' }, h('b', {}, s.title), s.current ? h('i', {}, 'Current') : null),
          h('small', {}, `${ago(s.last_at)} · ${s.runs} ${s.runs === 1 ? 'run' : 'runs'}`)))));
  }

  async function newSession() {
    close();
    try {
      await post('/api/sessions');
      await activity.show(null);
      emit('session-view');
      toast('New session. The report and its history carry over.');
    } catch (e) { toast(e.message, 'bad'); }
  }

  button.addEventListener('click', (e) => { e.stopPropagation(); menu.hidden ? open() : close(); });
  document.addEventListener('click', (e) => { if (!menu.contains(e.target) && !button.contains(e.target)) close(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
}
