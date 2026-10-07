// The agent's work as a readable timeline: what it did, grouped into chapters, newest at the
// bottom. Built only from server events, so a reloaded tab shows exactly the same history.

import { get } from './api.js';
import { emit, model, subscribe } from './live.js';
import { ICON, ago, clear, clock, h, list, md, plural, svg, tok } from './util.js';

const RUN_TITLES = { build: 'Building the report', update: 'Updating the report', steer: 'Applying your change',
                     resume: 'Continuing', recover: 'Continuing' };

export function mountActivity(root) {
  const sbar = h('div', { class: 'sbar', hidden: true });
  const feed = h('div', { class: 'feed no-anim' });
  const empty = h('p', { class: 'empty-note' }, 'Nothing yet. When the agent starts work, each step appears here.');
  const pill = h('button', { class: 'pill', type: 'button', onclick: () => toBottom() }, svg(ICON.down, { size: 13 }), 'Latest');
  root.append(sbar, feed);
  root.parentElement.append(pill);
  feed.append(empty);

  const runs = new Map();
  const steerState = new Map();
  const subs = new Map();          // subagent id -> its block in the feed
  const kinds = new Map();         // run id -> what kind of run it is (a message to the agent gets no block until it does work)
  let last = null, stick = true, hydrating = true;
  let programmaticUntil = 0;
  const nearBottom = () => root.scrollHeight - root.scrollTop - root.clientHeight < 90;
  root.addEventListener('scroll', () => {
    if (Date.now() < programmaticUntil) return;          // our own scrolling is not the reader's intent
    stick = nearBottom(); pill.classList.toggle('on', !stick);
  }, { passive: true });
  const toBottom = () => { programmaticUntil = Date.now() + 180; root.scrollTop = root.scrollHeight; stick = true; pill.classList.remove('on'); };
  const settle = () => { if (stick || hydrating) requestAnimationFrame(toBottom); else pill.classList.add('on'); };

  const place = (node) => { empty.remove(); feed.append(node); settle(); return node; };

  function ensureRun(id, kind, iso) {
    if (runs.has(id)) return runs.get(id);
    kind = kinds.get(id) || kind;
    for (const r of runs.values()) r.el.classList.add('collapsed');
    const inner = h('div', {});
    const title = h('span', {}, RUN_TITLES[kind] || 'Working');
    const when = h('span', {}, clock(iso));
    const el = h('section', { class: 'run', 'data-id': id },
      h('div', { class: 'run-head' },
        h('button', { type: 'button', 'aria-label': 'Show or hide this run', onclick: () => el.classList.toggle('collapsed') },
          svg(ICON.down, { size: 13, cls: 'chev' }), title), when),
      h('div', { class: 'run-body' }, inner));
    const r = { id, kind, el, inner, title, when, chapter: null, chapterName: null, steps: new Map() };
    runs.set(id, r); last = r;
    place(el);
    return r;
  }

  const line = (r, text, { tone, time, small, cls = 'moment' } = {}) => {
    const node = h('div', { class: cls, 'data-tone': tone }, h('span', { class: 'when' }, time ? clock(time) : ''), h('div', {}, text, small && h('small', {}, small)));
    if (r) { r.inner.append(node); r.chapter = null; settle(); } else place(node);
    return node;
  };

  function addStep(ev) {
    const r = ensureRun(ev.run_id, 'update', ev.created_at);
    if (!r.chapter) {
      const cont = r.chapterName === ev.chapter;
      r.chapter = h('div', { class: 'chapter' }, !cont && ev.chapter ? h('h4', {}, ev.chapter) : null, h('div', { class: 'steps' }));
      r.chapterName = ev.chapter;
      r.inner.append(r.chapter);
    } else if (ev.chapter && ev.chapter !== r.chapterName) {
      r.chapter = null; return addStep(ev);
    }
    const out = h('span', { class: 'out' });
    const node = h('div', { class: 'step', 'data-s': 'running' }, h('div', {}, ev.label, out), h('span', { class: 't' }, clock(ev.created_at)));
    const list = r.chapter.querySelector('.steps');
    list.append(node);
    r.steps.set(ev.detail.call, { node, out });
    fold(r.chapter, list);
    settle();
  }

  // A long stretch of reading is dozens of near-identical lines. Keep the latest few in view and
  // fold the rest behind one line, so the timeline reads as chapters rather than a log.
  const KEEP = 4;
  function fold(chapter, list) {
    const steps = [...list.querySelectorAll('.step')];
    let bar = list.querySelector('.fold');
    if (steps.length <= KEEP + 2) { bar?.remove(); return; }
    const older = steps.slice(0, steps.length - KEEP).filter((n) => n.dataset.s !== 'running');
    const open = chapter.dataset.open === '1';
    steps.forEach((n) => { n.hidden = !open && older.includes(n); });
    if (!bar) {
      bar = h('button', { class: 'fold', type: 'button', onclick: () => { chapter.dataset.open = chapter.dataset.open === '1' ? '0' : '1'; fold(chapter, list); } });
      list.prepend(bar);
    }
    bar.textContent = open ? 'Show fewer' : `${older.length} earlier steps`;
  }

  // ---- subagents: bulky work done in a context of its own, shown nested and collapsed once finished ----------
  function startSub(ev) {
    const r = ensureRun(ev.run_id, 'update', ev.created_at);
    r.chapter = null;
    const d = ev.detail || {};
    const meta = h('span', { class: 'sub-meta' }, 'working…');
    const body = h('div', { class: 'steps' });
    const head = h('button', { class: 'sub-head', type: 'button', 'aria-expanded': 'true', onclick: () => toggleSub(entry) },
      h('span', { class: 'dot', 'data-s': 'working' }), h('span', { class: 'sub-title' }, ev.label), meta, svg(ICON.chev, { size: 12, cls: 'chev' }));
    const el = h('div', { class: 'sub open', 'data-s': 'running' }, head, h('div', { class: 'sub-body' }, h('div', {}, body)));
    const entry = { el, head, body, meta, steps: new Map(), count: 0 };
    subs.set(d.sub, entry);
    r.inner.append(el); settle();
  }
  function toggleSub(entry) {
    const open = entry.el.classList.toggle('open');
    entry.head.setAttribute('aria-expanded', String(open));
  }
  function subStep(ev) {
    const entry = subs.get(ev.detail.sub);
    if (!entry) return;
    const out = h('span', { class: 'out' });
    const node = h('div', { class: 'step', 'data-s': 'running' }, h('div', {}, ev.label, out), h('span', { class: 't' }, clock(ev.created_at)));
    entry.body.append(node);
    entry.steps.set(ev.detail.call, { node, out });
    entry.count += 1;
    entry.meta.textContent = `${entry.count} step${entry.count === 1 ? '' : 's'}…`;
    settle();
  }
  function subStepDone(ev) {
    const s = subs.get(ev.detail.sub)?.steps.get(ev.detail.call);
    if (!s) return;
    s.node.dataset.s = ev.detail.level === 'issue' ? 'issue' : 'ok';
    if (ev.label) s.out.textContent = ev.label;
  }
  function endSub(ev) {
    const d = ev.detail || {};
    const entry = subs.get(d.sub);
    if (!entry) return;
    const u = d.usage || {};
    const tokens = (u.input || 0) + (u.output || 0);
    const bits = [ev.label, tokens ? `${tok(tokens)} tokens` : null, d.seconds ? `${Math.round(d.seconds)} s` : null].filter(Boolean);
    entry.meta.textContent = bits.join(' · ');
    entry.el.dataset.s = d.status === 'done' ? 'done' : 'failed';
    entry.head.querySelector('.dot').dataset.s = d.status === 'done' ? 'ok' : 'bad';
    if (!hydrating) { entry.el.classList.remove('open'); entry.head.setAttribute('aria-expanded', 'false'); }
    else { entry.el.classList.remove('open'); }
  }

  function doneStep(ev) {
    for (const r of runs.values()) {
      const s = r.steps.get(ev.detail.call);
      if (!s) continue;
      s.node.dataset.s = ev.detail.level === 'issue' ? 'issue' : 'ok';
      if (ev.label) s.out.textContent = ev.label;
      settle();
      return;
    }
  }

  const tickers = new Set();
  setInterval(() => tickers.forEach((t) => t()), 1000);

  function apply(ev, live) {
    const d = ev.detail || {};
    const runOf = () => (ev.run_id && runs.get(ev.run_id)) || null;
    switch (ev.kind) {
      case 'run.queued': case 'run.start':
        if (d.kind) kinds.set(ev.run_id, d.kind);
        // A message to the agent shows as a conversation: its work gets a block only once it does some (see addStep).
        if ((kinds.get(ev.run_id) || 'update') !== 'steer') ensureRun(ev.run_id, d.kind || 'update', ev.created_at);
        break;
      case 'run.prepared': { const pr = runs.get(ev.run_id); if (pr) line(pr, ev.label, { cls: 'moment', time: ev.created_at }); break; }
      case 'subagent.start': startSub(ev); break;
      case 'subagent.done': endSub(ev); break;
      case 'step': if (d.sub) subStep(ev); else addStep(ev); break;
      case 'step.done': if (d.sub) subStepDone(ev); else doneStep(ev); break;
      case 'note': {
        const r = runOf();
        const full = d.text || ev.label;                 // the whole reply, never the shortened summary
        const body = md(full);
        const n = h('div', { class: 'agent-note' }, body);
        if (full.length > 700) {
          n.classList.add('clamped');
          n.append(h('button', { class: 'fold', type: 'button', onclick: (e) => { n.classList.toggle('clamped'); e.target.textContent = n.classList.contains('clamped') ? 'Read all' : 'Show less'; } }, 'Read all'));
        }
        if (r) { r.inner.append(n); r.chapter = null; settle(); } else place(n);
        break;
      }
      case 'question': line(runOf(), d.text || ev.label, { time: ev.created_at, small: d.why || undefined }); break;
      case 'question.answered': line(runOf(), 'You answered: ' + (d.text || ev.label).replace(/^SKIP:.*/, 'Decide for me'), { time: ev.created_at, cls: 'moment you' }); break;
      case 'steer': {
        const state = h('small', { class: 'state' }, d.queued ? 'Queued · applies after the current step' : 'Sent');
        steerState.set(d.id, state);
        const atts = (d.attachments || []).length ? h('div', { class: 'chips inline' }, d.attachments.map((a) =>
          h('span', { class: 'chip' }, svg(ICON.doc, { size: 13 }), h('span', { class: 'nm' }, a.name)))) : null;
        const about = d.note ? h('small', { class: 'about' }, `About the review note “${d.note_area || ''}”`) : null;
        line(runOf(), [about, h('div', { class: 'quote' }, d.text || ev.label), atts, state], { time: ev.created_at, cls: 'moment you' });
        break;
      }
      case 'steer.dropped': {
        const st = steerState.get(d.id);
        if (st) { st.textContent = 'Not delivered: the run ended first. Send it again.'; st.dataset.done = '0'; }
        break;
      }
      case 'steer.applied': {
        const st = steerState.get(d.id);
        if (st) { st.textContent = `Applied at ${clock(ev.created_at)}`; st.dataset.done = '1'; }
        break;
      }
      case 'retry': {
        const text = ev.label.replace(/Trying again in \d+ seconds\./, '');
        const target = new Date(ev.created_at).getTime() + (d.wait || 0) * 1000;
        const span = h('span', {});
        const n = line(runOf(), [text, span], { tone: 'warn', time: ev.created_at });
        const tick = () => { const s = Math.max(0, Math.round((target - Date.now()) / 1000)); span.textContent = s ? `Trying again in ${s} s (attempt ${d.attempt} of ${d.of}).` : 'Trying again now.'; if (!s || !n.isConnected) tickers.delete(tick); };
        tickers.add(tick); tick();
        break;
      }
      case 'recovered': case 'provider.switch': case 'issue': line(runOf(), ev.label, { tone: ev.kind === 'issue' ? 'warn' : undefined, time: ev.created_at }); break;
      case 'compacted': line(runOf(), ev.label, { time: ev.created_at }); break;
      case 'nudge': line(runOf(), ev.label, { time: ev.created_at }); break;
      case 'version': {
        const r = runOf();
        const ch = d.changed?.length ? ` · changed ${list(d.changed.filter((k) => k !== 'cover'))}` : '';
        const node = h('div', { class: 'closing' }, h('b', {}, ev.label), ` · ${plural(d.pages, 'page')}${d.version > 1 ? ch : ''}`);
        (r ? r.inner : feed).append(node); settle();
        break;
      }
      case 'run.end': {
        const r = runOf();
        if (!r) {                                          // a conversation turn that did no work: only a failure needs saying
          if (d.status === 'failed' || d.status === 'incomplete') line(null, ev.label || 'The agent could not reply.', { tone: 'warn', time: ev.created_at });
          break;
        }
        r.chapter = null;
        const bad = d.status === 'failed' || d.status === 'incomplete';
        if (d.status === 'done') { if (!r.inner.querySelector('.closing')) r.inner.append(h('div', { class: 'closing' }, h('b', {}, 'Done'))); }
        else r.inner.append(h('div', { class: 'closing', 'data-tone': bad ? 'bad' : '' }, ev.label || (d.status === 'stopped' ? 'Stopped.' : '')));
        r.title.textContent = (RUN_TITLES[r.kind] || r.title.textContent);
        if (d.status && d.status !== 'done') r.when.textContent = `${clock(ev.created_at)} · ${d.status}`;
        if (live) { /* the latest run stays open until the next one starts */ }
        settle();
        break;
      }
      case 'source.sync': {
        const sec = d.sections?.length ? `Affects ${list(d.sections)}` : undefined;
        line(null, ev.label, { time: ev.created_at, small: sec });
        break;
      }
      case 'session.new': line(null, 'New session started. The report and its history carry over.', { time: ev.created_at }); break;
      case 'waiting.sources': case 'period': case 'source.cleared': case 'source.moved': case 'instance.new': case 'instance.open': line(null, ev.label, { time: ev.created_at }); break;
      default: break;
    }
  }

  // Show one session's history (an earlier one read-only, or the current one live).
  async function show(id, info) {
    const data = await get(`/api/history?n=400${id ? `&session=${encodeURIComponent(id)}` : ''}`);
    model.viewingSession = id || null;
    clear(feed); runs.clear(); steerState.clear(); subs.clear(); kinds.clear(); last = null;
    feed.append(empty);
    hydrating = true; feed.classList.add('no-anim'); stick = true;
    data.events.forEach((ev) => apply(ev, false));
    [...runs.values()].slice(0, -1).forEach((r) => r.el.classList.add('collapsed'));
    requestAnimationFrame(() => { hydrating = false; feed.classList.remove('no-anim'); toBottom(); });
    clear(sbar);
    sbar.hidden = !id;
    if (id) sbar.append(h('span', {}, info?.last_at ? `An earlier session · ${ago(info.last_at)}` : 'An earlier session'),
      h('button', { type: 'button', onclick: () => show(null).then(() => emit('session-view')) }, 'Back to the current session'));
    emit('session-view');
  }

  subscribe((what, data) => {
    if (what === 'event' && !model.viewingSession) apply(data.ev, data.live);
    if (what === 'quarter') show(null);                  // a new quarter starts with an empty feed (its own conversation)
    if (what === 'hydrated') {
      hydrating = false; requestAnimationFrame(() => { feed.classList.remove('no-anim'); toBottom(); });
      const open = [...runs.values()];
      open.slice(0, -1).forEach((r) => r.el.classList.add('collapsed'));
    }
  });
  // replay anything ingested before this component mounted
  model.events.forEach((ev) => apply(ev, false));
  return { show };
}
