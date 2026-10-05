// The report itself, open by default on the right: the actual rendered pages, not a preview of
// something else. Pages are the stored PDF of each numbered version, so what is shown is exactly
// what gets downloaded.
//
// Every render with different content is a numbered version. Any of them can be opened, the pages
// that changed from the one before are marked, and "Changes" shows the wording that changed.

import { get } from './api.js';
import { model, subscribe } from './live.js';
import { $, ICON, ago, clear, clock, h, plural, reduceMotion, svg, toast } from './util.js';

export function mountViewer(root) {
  const load = h('div', { class: 'vload' }, h('i'));
  const meta = h('div', { class: 'vmeta' });
  const page = h('span', { class: 'vpage' });
  const tools = h('div', { class: 'vtools' });
  const top = h('div', { class: 'vtop' }, meta, tools);
  const old = h('div', { class: 'vold', hidden: true });
  const stack = h('div', { class: 'stack' });
  const scroller = h('div', { class: 'vscroll' }, stack);
  const empty = h('div', { class: 'vempty' });
  const pop = h('div', { class: 'menu-pop', hidden: true });
  const panel = h('aside', { class: 'changes', 'aria-label': 'Changes in this version', hidden: true });
  root.append(load, top, old, scroller, empty, pop, panel);

  let shown = null, pinned = false, latestSeen = 0, sheets = [], obs = null, pageObs = null, bannerTimer = null;

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
    clear(tools); old.hidden = true;
  };

  // -- popovers -------------------------------------------------------------------------------
  const closePop = () => { pop.hidden = true; $$all('[data-pop]').forEach((b) => b.setAttribute('aria-expanded', 'false')); };
  const $$all = (sel) => [...root.querySelectorAll(sel)];
  function openPop(kind, anchor, build) {
    if (!pop.hidden && pop.dataset.kind === kind) { closePop(); return; }
    clear(pop); build(pop); pop.dataset.kind = kind; pop.hidden = false;
    pop.classList.toggle('left', kind === 'versions');
    $$all('[data-pop]').forEach((b) => b.setAttribute('aria-expanded', String(b === anchor)));
  }
  document.addEventListener('click', (e) => { if (!pop.contains(e.target) && !e.target.closest('[data-pop]')) closePop(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { closePop(); closeChanges(); } });

  const summary = (v) => {
    if (v.first) return 'First version';
    const names = v.changed.map((c) => c.title.replace(/^\d+(\.\d+)?\s*/, ''));
    if (!names.length) return 'No wording changes';
    return `Changed ${names.slice(0, 2).join(', ')}${names.length > 2 ? ` and ${names.length - 2} more` : ''}`;
  };

  // -- header -------------------------------------------------------------------------------
  function renderTop(R) {
    const working = model.state?.status.phase === 'working';
    load.classList.toggle('on', !!working);
    const latest = R.latest ?? R.version;
    const vbtn = h('button', { class: 'quiet vbtn', type: 'button', 'data-pop': '1', 'aria-expanded': 'false', 'aria-haspopup': 'menu',
      onclick: (e) => { e.stopPropagation(); openPop('versions', vbtn, (m) => (R.versions || []).forEach((v) => m.append(
        h('button', { class: 'row vrow' + (v.version === R.version ? ' on' : ''), type: 'button', onclick: () => { closePop(); viewVersion(v.version); } },
          h('span', {}, h('span', { class: 'l1' }, h('b', {}, `Version ${v.version}`), v.version === latest ? h('i', {}, 'Latest') : null), h('small', {}, summary(v))),
          h('span', {}, h('small', {}, ago(v.created_at)), h('small', {}, plural(v.pages, 'page'))))))); } },
      `Version ${R.version}`, svg(ICON.down, { size: 13 }));
    clear(meta).append(vbtn, h('span', { class: 'muted' }, working && !pinned ? 'Updating…' : `${plural(R.pages, 'page')} · ${ago(R.created_at)}`));

    const sections = (R.sections || []).filter((s) => s.page);
    const ctoc = h('button', { class: 'quiet', type: 'button', 'data-pop': '1', 'aria-expanded': 'false',
      onclick: (e) => { e.stopPropagation(); openPop('contents', ctoc, (m) => sections.forEach((s) => m.append(
        h('button', { class: 'row', type: 'button', onclick: () => { closePop(); goto(s.page); } }, h('span', {}, `${s.key}  ${s.title}`), h('span', {}, s.page))))); } },
      svg(ICON.list, { size: 15 }), h('span', { class: 'hide-s' }, 'Contents'));
    const cchg = R.version > 1 && (R.versions || []).find((v) => v.version === R.version && !v.first)
      ? h('button', { class: 'quiet', type: 'button', onclick: () => openChanges(R) }, svg(ICON.refresh, { size: 14 }), h('span', { class: 'hide-s' }, 'Changes')) : null;
    clear(tools).append(...[page, sections.length ? ctoc : null, cchg,
      h('a', { class: 'quiet', href: `/api/report/download?fmt=pdf&v=${R.version}`, download: '' }, svg(ICON.download, { size: 15 }), 'PDF'),
      h('a', { class: 'quiet', href: `/api/report/download?fmt=docx&v=${R.version}`, download: '' }, 'Word')].filter(Boolean));

    old.hidden = !pinned;
    if (pinned) clear(old).append(h('span', {}, `You are viewing version ${R.version}, not the latest.`),
      h('button', { type: 'button', onclick: () => viewVersion(latest) }, `Back to version ${latest}`));
    updatePage();
  }

  const goto = (n) => sheets[n - 1]?.scrollIntoView({ behavior: reduceMotion() ? 'auto' : 'smooth', block: 'start' });

  // -- the changes panel -----------------------------------------------------------------------
  const closeChanges = () => { panel.classList.remove('open'); setTimeout(() => { if (!panel.classList.contains('open')) panel.hidden = true; }, 320); };
  async function openChanges(R) {
    closePop();
    clear(panel).append(h('div', { class: 'ch-head' }, h('div', {}, h('h3', {}, `Changes in version ${R.version}`), h('p', { class: 'small' }, 'Loading…')),
      h('button', { class: 'quiet', type: 'button', 'aria-label': 'Close', onclick: closeChanges }, '✕')));
    panel.hidden = false; requestAnimationFrame(() => panel.classList.add('open'));
    let d;
    try { d = await get(`/api/report/diff?v=${R.version}`); } catch (e) { toast(e.message, 'bad'); return; }
    const body = h('div', { class: 'ch-body' });
    if (!d.sections.length) body.append(h('p', { class: 'small' }, 'Nothing changed that can be shown.'));
    for (const s of d.sections) {
      const sec = h('section', { class: 'ch-sec' },
        h('div', { class: 'ch-title' }, h('h4', {}, `${s.key !== 'cover' ? s.key + '  ' : ''}${s.title}`),
          s.page ? h('button', { class: 'link', type: 'button', onclick: () => { goto(s.page); closeChanges(); } }, `Page ${s.page}`) : null));
      if (s.text === null || s.text === undefined) sec.append(h('p', { class: 'small' }, 'The wording of this change was not kept for this version. The changed pages are marked in the report.'));
      else if (!s.text.length) sec.append(h('p', { class: 'small' }, 'The figures or tables in this section changed; the wording did not.'));
      else sec.append(h('p', { class: 'diff' }, s.text.flatMap(([op, t], i) => {
        const node = op === 'eq' ? document.createTextNode(t) : h(op === 'ins' ? 'ins' : 'del', {}, t);
        return [node, document.createTextNode(' ')];
      })));
      body.append(sec);
    }
    clear(panel).append(h('div', { class: 'ch-head' }, h('div', {}, h('h3', {}, `Changes in version ${R.version}`),
      h('p', { class: 'small' }, d.first ? 'This is the first version.' : `Compared with version ${d.against}`)),
      h('button', { class: 'quiet', type: 'button', 'aria-label': 'Close', onclick: closeChanges }, '✕')), body);
  }

  // -- pages --------------------------------------------------------------------------------
  const src = (v, n) => `/api/report/page/${n}.png?v=${v}&scale=1.6`;
  function lazyLoad(sheet, v, n) {
    const img = new Image();
    img.decoding = 'async'; img.alt = `Page ${n}`;
    img.onload = () => {
      requestAnimationFrame(() => img.classList.add('in'));
      sheet.classList.add('loaded');
      const prev = sheet.querySelector('img');
      if (prev && prev !== img) prev.replaceWith(img); else if (!prev) sheet.prepend(img);
    };
    img.src = src(v, n);
    sheet.dataset.v = v;
  }

  async function show(R, { announce = false } = {}) {
    const sizes = R.sizes?.length ? R.sizes : (await get(`/api/report/sizes?v=${R.version}`)).sizes;
    empty.hidden = true; scroller.hidden = false;
    const same = shown && sheets.length === sizes.length;
    const changed = new Set(R.changed_pages || []);
    if (!same) {
      const ratio = scroller.scrollTop / Math.max(1, scroller.scrollHeight);
      clear(stack); sheets = [];
      sizes.forEach(([w, hgt], i) => {
        const sheet = h('div', { class: 'sheet', 'data-n': i + 1, style: { '--ar': `${w / hgt}` } });
        stack.append(sheet); sheets.push(sheet);
      });
      shown = R; observe();
      scroller.scrollTop = ratio * scroller.scrollHeight;
    } else sizes.forEach(([w, hgt], i) => sheets[i].style.setProperty('--ar', `${w / hgt}`));
    const prev = shown; shown = R;
    sheets.forEach((s, i) => {
      s.classList.remove('changed'); s.querySelector('.flag')?.remove();
      if (changed.has(i + 1)) { s.classList.add('changed'); s.append(h('span', { class: 'flag' }, 'Updated')); }
      if (String(s.dataset.v || '') !== String(R.version)) s.dataset.stale = '1';
      if (s.dataset.visible) { delete s.dataset.stale; lazyLoad(s, R.version, +s.dataset.n); }
    });
    renderTop(R);
    if (announce && same && prev && changed.size) banner(`Version ${R.version} is ready · ${plural(changed.size, 'page')} changed`, [...changed].sort((a, b) => a - b)[0]);
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

  function banner(text, firstPage, action) {
    $('.banner', root)?.remove(); clearTimeout(bannerTimer);
    const b = h('div', { class: 'banner', role: 'status' }, h('span', {}, text),
      h('button', { type: 'button', onclick: () => { b.remove(); if (action) action(); else if (firstPage) goto(firstPage); } }, action ? 'View' : 'Show'),
      h('button', { class: 'x', type: 'button', 'aria-label': 'Dismiss', onclick: () => b.remove() }, '✕'));
    root.append(b);
    bannerTimer = setTimeout(() => b.remove(), 14000);
  }

  // -- choosing a version ----------------------------------------------------------------------
  async function viewVersion(v) {
    try {
      const R = await get(`/api/report?v=${v}`);
      pinned = R.version !== R.latest;
      closeChanges();
      await show(R);
      scroller.scrollTo({ top: 0 });
    } catch (e) { toast(e.message, 'bad'); }
  }

  // -- wiring -------------------------------------------------------------------------------
  const refresh = async () => {
    const L = model.report;
    if (!L || !L.version) { shown = null; sheets = []; clear(stack); scroller.hidden = true; empty.hidden = false; renderEmpty(); return; }
    const isNew = L.version > latestSeen;
    latestSeen = Math.max(latestSeen, L.version);
    try {
      if (!shown) await show(L);
      else if (!pinned) { if (shown.version !== L.version) await show(L, { announce: true }); else renderTop({ ...shown, versions: L.versions, latest: L.version }); }
      else {
        shown = { ...shown, versions: L.versions, latest: L.version };
        renderTop(shown);
        if (isNew) banner(`Version ${L.version} is ready`, null, () => viewVersion(L.version));
      }
    } catch { /* retried on the next event */ }
  };
  subscribe((what) => {
    if (what === 'report') refresh();
    if (what === 'state') { const R = shown || model.report; if (R?.version) renderTop(R); else renderEmpty(); }
  });
  scroller.hidden = true; empty.hidden = false; renderEmpty();
  setInterval(() => { if (shown) renderTop(shown); }, 30000);
}
