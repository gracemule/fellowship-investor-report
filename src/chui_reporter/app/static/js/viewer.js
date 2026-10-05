// The report itself, open by default on the right: the actual rendered pages, not a preview of
// something else. Pages are the stored PDF of each numbered version, so what is shown is exactly
// what gets downloaded. New versions replace old pages in place; changed pages are marked.

import { get } from './api.js';
import { model, subscribe } from './live.js';
import { $, ICON, ago, clear, h, plural, reduceMotion, svg } from './util.js';

export function mountViewer(root) {
  const load = h('div', { class: 'vload' }, h('i'));
  const meta = h('div', { class: 'vmeta' });
  const page = h('span', { class: 'vpage' });
  const tools = h('div', { class: 'vtools' });
  const top = h('div', { class: 'vtop' }, meta, tools);
  const stack = h('div', { class: 'stack' });
  const scroller = h('div', { class: 'vscroll' }, stack);
  const empty = h('div', { class: 'vempty' });
  root.append(load, top, scroller, empty);

  let shown = null, sheets = [], obs = null, pageObs = null, bannerTimer = null;

  // -- empty state ------------------------------------------------------------------------
  const renderEmpty = () => {
    const S = model.state;
    const ph = S?.status.phase;
    const copy = {
      empty: ['Your report will appear here', 'Choose the folder with this quarter’s files and the agent will take it from there.'],
      needs_sources: ['Waiting for the last files', 'The report is built as soon as everything it needs is in your folder.'],
      ready: ['Ready to build', 'Everything required is in. Start the build and the pages appear here as soon as they are rendered.'],
      working: ['Building your report', 'The first version appears here when it has been rendered and checked.'],
      waiting_user: ['Paused for your answer', 'The agent has a question. Answer it on the left and it carries on.'],
      waiting_data: ['Waiting for files', 'The agent carries on by itself once they are in your folder.'],
      attention: ['Not built yet', 'The last attempt stopped. Your work is saved; continue from the left.'],
    }[ph] || ['Your report will appear here', ''];
    clear(empty).append(h('div', { class: 'inner' },
      h('div', { class: 'ghost', 'aria-hidden': 'true' }, h('i', { class: 't' }), h('i', { class: 'a' }), h('i', { class: 'b' }), h('i', { class: 'c' }), h('i', { class: 'd' }), h('i', { class: 'g' }), h('i', { class: 'b' }), h('i', { class: 'a' })),
      h('h2', { class: 'headline' }, copy[0]), copy[1] && h('p', { class: 'detail' }, copy[1])));
    load.classList.toggle('on', ph === 'working');
    clear(meta).append(h('span', { class: 'muted' }, ph === 'working' ? 'Building…' : 'No report yet'));
    clear(tools);
  };

  // -- header -------------------------------------------------------------------------------
  function renderTop(R) {
    const working = model.state?.status.phase === 'working';
    load.classList.toggle('on', !!working);
    clear(meta).append(h('b', {}, `Version ${R.version}`),
      h('span', { class: 'muted' }, working ? 'Updating…' : `${plural(R.pages, 'page')} · ${ago(R.created_at)}`));
    const menu = h('div', { class: 'menu-pop', hidden: true });
    const toggle = h('button', { class: 'quiet', type: 'button', 'aria-expanded': 'false', onclick: (e) => { e.stopPropagation(); const o = menu.hidden; menu.hidden = !o; toggle.setAttribute('aria-expanded', String(o)); } },
      svg(ICON.list, { size: 15 }), h('span', { class: 'hide-s' }, 'Contents'));
    const sections = R.sections.filter((s) => s.page);
    sections.forEach((s) => menu.append(h('button', { class: 'row', type: 'button', onclick: () => { menu.hidden = true; goto(s.page); } }, h('span', {}, `${s.key}  ${s.title}`), h('span', {}, s.page))));
    clear(tools).append(page,
      sections.length ? toggle : null, sections.length ? menu : null,
      h('a', { class: 'quiet', href: `/api/report/download?fmt=pdf&v=${R.version}`, download: '' }, svg(ICON.download, { size: 15 }), 'PDF'),
      h('a', { class: 'quiet', href: `/api/report/download?fmt=docx&v=${R.version}`, download: '' }, 'Word'));
    updatePage();
  }
  document.addEventListener('click', () => { const m = $('.menu-pop', root); if (m) m.hidden = true; });

  const goto = (n) => sheets[n - 1]?.scrollIntoView({ behavior: reduceMotion() ? 'auto' : 'smooth', block: 'start' });

  // -- pages --------------------------------------------------------------------------------
  const src = (v, n) => `/api/report/page/${n}.png?v=${v}&scale=1.6`;
  function lazyLoad(sheet, v, n) {
    const img = new Image();
    img.decoding = 'async'; img.alt = `Page ${n}`;
    img.onload = () => {
      requestAnimationFrame(() => img.classList.add('in'));
      sheet.classList.add('loaded');
      const old = sheet.querySelector('img');
      if (old && old !== img) old.replaceWith(img); else if (!old) sheet.prepend(img);
    };
    img.src = src(v, n);
    sheet.dataset.v = v;
  }

  async function show(R) {
    const sizes = R.sizes?.length ? R.sizes : (await get(`/api/report/sizes?v=${R.version}`)).sizes;
    empty.hidden = true; scroller.hidden = false;
    const same = shown && sheets.length === sizes.length;
    const changed = new Set(R.version > 1 ? R.changed_pages : []);
    if (!same) {
      const ratio = scroller.scrollTop / Math.max(1, scroller.scrollHeight);
      clear(stack); sheets = [];
      sizes.forEach(([w, hgt], i) => {
        const sheet = h('div', { class: 'sheet', 'data-n': i + 1, style: { '--ar': `${w / hgt}` } });
        stack.append(sheet); sheets.push(sheet);
      });
      shown = R; observe();
      scroller.scrollTop = ratio * scroller.scrollHeight;
    } else {
      sizes.forEach(([w, hgt], i) => sheets[i].style.setProperty('--ar', `${w / hgt}`));
    }
    const prev = shown; shown = R;
    sheets.forEach((s, i) => {
      s.classList.remove('changed'); s.querySelector('.flag')?.remove();
      if (changed.has(i + 1)) { s.classList.add('changed'); s.append(h('span', { class: 'flag' }, 'Updated')); }
      if (String(s.dataset.v || '') !== String(R.version)) s.dataset.stale = '1';
      if (s.dataset.visible) { delete s.dataset.stale; lazyLoad(s, R.version, +s.dataset.n); }   // near the screen: swap now
    });
    renderTop(R);
    if (same && prev && changed.size) banner(R, [...changed].sort((a, b) => a - b));
  }

  function observe() {
    obs?.disconnect(); pageObs?.disconnect();
    obs = new IntersectionObserver((entries) => {
      for (const e of entries) {
        const s = e.target;
        if (e.isIntersecting) {
          s.dataset.visible = '1';
          if (!s.querySelector('img') || s.dataset.stale) { delete s.dataset.stale; lazyLoad(s, shown.version, +s.dataset.n); }
        } else delete s.dataset.visible;
      }
    }, { root: scroller, rootMargin: '1400px 0px' });
    pageObs = new IntersectionObserver(() => updatePage(), { root: scroller, threshold: [0, .25, .5, .75, 1] });
    sheets.forEach((s) => { obs.observe(s); pageObs.observe(s); });
  }

  function updatePage() {
    if (!sheets.length) return;
    const mid = scroller.getBoundingClientRect().top + scroller.clientHeight * .35;
    let best = 1;
    sheets.forEach((s, i) => { if (s.getBoundingClientRect().top <= mid) best = i + 1; });
    page.textContent = `${best} / ${sheets.length}`;
  }
  scroller.addEventListener('scroll', () => requestAnimationFrame(updatePage), { passive: true });

  function banner(R, pages) {
    $('.banner', root)?.remove(); clearTimeout(bannerTimer);
    const b = h('div', { class: 'banner', role: 'status' },
      h('span', {}, `Version ${R.version} is ready · ${plural(pages.length, 'page')} changed`),
      h('button', { type: 'button', onclick: () => { goto(pages[0]); b.remove(); } }, 'Show'),
      h('button', { class: 'x', type: 'button', 'aria-label': 'Dismiss', onclick: () => b.remove() }, '✕'));
    root.append(b);
    bannerTimer = setTimeout(() => b.remove(), 14000);
  }

  // -- wiring -------------------------------------------------------------------------------
  const refresh = async () => {
    const R = model.report;
    if (!R || !R.version) { shown = null; sheets = []; clear(stack); scroller.hidden = true; empty.hidden = false; renderEmpty(); return; }
    if (!shown || shown.version !== R.version) { try { await show(R); } catch { /* retried on the next event */ } }
    else renderTop(R);
  };
  subscribe((what) => {
    if (what === 'report') refresh();
    if (what === 'state') { const R = model.report; if (R?.version) renderTop(R); else renderEmpty(); }
  });
  scroller.hidden = true; empty.hidden = false; renderEmpty();
  setInterval(() => { if (model.report?.version) renderTop(model.report); }, 30000);
}
