import { get, post, setAuthHandler } from './api.js';
import { mountActivity } from './activity.js';
import * as folder from './folder.js';
import { mountComposer } from './composer.js';
import { mountHero } from './hero.js';
import { start } from './live.js';
import { mountMenu } from './menu.js';
import { mountSessions } from './sessions.js';
import { mountNotes } from './notes.js';
import { mountSources } from './sources.js';
import { mountViewer } from './viewer.js';
import { $, $$, toast } from './util.js';

let started = false;

// ---- sign-in ----------------------------------------------------------------------------------
const login = $('#login');
setAuthHandler(() => { login.hidden = false; document.body.classList.remove('booting'); $('#password').focus(); });
$('#login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const err = $('#login-error'); err.textContent = '';
  try {
    await post('/api/login', { password: $('#password').value });
    $('#password').value = ''; login.hidden = true;
    await boot();
  } catch (ex) { err.textContent = ex.message; }
});

// ---- tabs --------------------------------------------------------------------------------------
const tabs = $('#tabs');
function moveInk() {
  const t = $('[role=tab][aria-selected=true]', tabs), ink = $('.ink', tabs);
  if (!t) return;
  ink.style.setProperty('--x', `${t.offsetLeft}px`); ink.style.width = `${t.offsetWidth}px`;
}
function selectTab(name, { animate = true } = {}) {
  $$('[role=tab]', tabs).forEach((t) => t.setAttribute('aria-selected', String(t.dataset.tab === name)));
  for (const p of ['activity', 'sources', 'notes']) {
    const el = $(`#panel-${p}`);
    const on = p === name;
    el.hidden = !on;
    if (on && animate) { el.classList.remove('enter'); void el.offsetWidth; el.classList.add('enter'); }
  }
  moveInk();
}
tabs.addEventListener('click', (e) => { const b = e.target.closest('[role=tab]'); if (b) selectTab(b.dataset.tab); });
tabs.addEventListener('keydown', (e) => {
  const list = $$('[role=tab]', tabs), i = list.indexOf(document.activeElement);
  if (i < 0 || !['ArrowRight', 'ArrowLeft'].includes(e.key)) return;
  const n = list[(i + (e.key === 'ArrowRight' ? 1 : -1) + list.length) % list.length];
  n.focus(); selectTab(n.dataset.tab);
});
addEventListener('resize', moveInk);

// ---- mobile pane switch --------------------------------------------------------------------------
$('#switch').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  document.body.dataset.pane = b.dataset.pane;
  $$('#switch button').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
});

// ---- boot ----------------------------------------------------------------------------------------
async function boot() {
  if (started) return;
  started = true;
  try {
    const activity = mountActivity($('#panel-activity'));
    mountHero($('#hero'));
    mountSources($('#panel-sources'), $('#count-sources'));
    mountNotes($('#panel-notes'), $('#count-notes'));
    mountViewer($('#viewer'));
    mountComposer();
    mountSessions({ button: $('#sessions'), menu: $('#smenu'), activity });
    mountMenu({ button: $('#period'), menu: $('#menu'), onSignOut: async () => { await post('/api/logout'); location.reload(); } });
    await start();
    await folder.init();
  } catch (e) {
    started = false;
    if (e.status !== 401) toast(e.message || 'Could not reach the server.', 'bad');
    return;
  }
  document.body.classList.remove('booting');
  requestAnimationFrame(() => { selectTab('activity', { animate: false }); moveInk(); });
  document.fonts?.ready.then(moveInk);
}

(async () => {
  try { await get('/api/session'); await boot(); }
  catch (e) { if (e.status !== 401) { document.body.classList.remove('booting'); toast('Could not reach the server.', 'bad'); } }
})();
