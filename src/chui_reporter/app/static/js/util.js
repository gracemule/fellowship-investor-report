// Small DOM helpers. Text is always inserted as text nodes, never as HTML.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  append(el, kids);
  return el;
}

export function append(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.append(k.nodeType ? k : document.createTextNode(String(k)));
  }
  return el;
}

export const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };

export function svg(path, { size = 16, stroke = 1.6, cls = '' } = {}) {
  const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('width', size); s.setAttribute('height', size);
  s.setAttribute('fill', 'none'); s.setAttribute('stroke', 'currentColor');
  s.setAttribute('stroke-width', stroke); s.setAttribute('stroke-linecap', 'round');
  s.setAttribute('stroke-linejoin', 'round'); s.setAttribute('aria-hidden', 'true');
  if (cls) s.setAttribute('class', cls);
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  p.setAttribute('d', path); s.append(p);
  return s;
}
export const ICON = {
  check: 'M5 12.5l4.5 4.5L19 7.5',
  chev: 'M9 6l6 6-6 6',
  down: 'M6 9l6 6 6-6',
  arrow: 'M5 12h14M13 6l6 6-6 6',
  up: 'M12 19V5M6 11l6-6 6 6',
  folder: 'M3.5 7.5a2 2 0 0 1 2-2h4l2 2h7a2 2 0 0 1 2 2v7.5a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z',
  doc: 'M7 3.5h7l4 4V20a.5.5 0 0 1-.5.5h-10.5a.5.5 0 0 1-.5-.5V4a.5.5 0 0 1 .5-.5zM14 3.5V8h4',
  download: 'M12 4v11M7 11l5 5 5-5M5 20h14',
  list: 'M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01',
  stop: 'M7 7h10v10H7z',
  refresh: 'M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7',
  plus: 'M12 5v14M5 12h14',
  clock: 'M12 7v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z',
};

const pad = (n) => String(n).padStart(2, '0');
export const clock = (iso) => { const d = new Date(iso); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };

export function ago(iso, now = Date.now()) {
  if (!iso) return '';
  const s = Math.max(0, (now - new Date(iso).getTime()) / 1000);
  if (s < 45) return 'just now';
  if (s < 90) return 'a minute ago';
  if (s < 3300) return `${Math.round(s / 60)} min ago`;
  if (s < 5400) return 'an hour ago';
  if (s < 86400) return `${Math.round(s / 3600)} hours ago`;
  const d = new Date(iso);
  return s < 172800 ? `yesterday, ${clock(iso)}` : d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
export const plural = (n, one, many = one + 's') => `${n} ${n === 1 ? one : many}`;
export const reduceMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;

export function list(items, conj = 'and') {
  if (items.length <= 1) return items.join('');
  return items.slice(0, -1).join(', ') + ` ${conj} ` + items.at(-1);
}

let toastTimer;
export function toast(msg, kind = 'info') {
  let t = $('#toast');
  if (!t) { t = h('div', { id: 'toast', role: 'status' }); document.body.append(t); }
  t.textContent = msg; t.dataset.kind = kind; t.classList.add('on');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('on'), 5200);
}

// A small, safe markdown subset (paragraphs, bullets, **bold**, `code`) built from DOM nodes, never HTML.
function inline(text) {
  const out = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    out.push(tok.startsWith('**') ? h('strong', {}, tok.slice(2, -2)) : h('code', {}, tok.slice(1, -1)));
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}
export function md(text) {
  const root = h('div', { class: 'md' });
  let list = null;
  for (const raw of String(text).split('\n')) {
    const line = raw.trim();
    if (!line) { list = null; continue; }
    const quote = line.match(/^>+\s*(.*)$/);
    const li = line.match(/^(?:[-*•]|\d+[.)])\s+(.*)$/);
    if (quote) {
      list = null;
      if (quote[1]) root.append(h('blockquote', {}, inline(quote[1])));
    } else if (li) {
      if (!list) { list = h('ul', {}); root.append(list); }
      list.append(h('li', {}, inline(li[1])));
    } else { list = null; root.append(h('p', {}, inline(line))); }
  }
  return root;
}

// 49,345 -> "49k", 1,048,576 -> "1.05M"
export function tok(n) {
  if (n == null) return '';
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 2).replace(/\.?0+$/, '')}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}k`;
  return String(n);
}
