// What the agent has to work with: each kind of source document, whether it is in, and what it
// feeds. This answers "does the agent have what it needs?" without a file listing.

import * as folder from './folder.js';
import { model, subscribe } from './live.js';
import { ICON, ago, clear, h, list, svg } from './util.js';

const open = new Set();
const fmtAge = (ms) => (ms ? ago(new Date(ms).toISOString()) : '');

export function mountSources(root, tabCount) {
  const render = () => {
    const S = model.state;
    if (!S) return;
    const cov = S.coverage || [];
    const needed = cov.filter((c) => c.state === 'missing');
    const ready = cov.filter((c) => c.state === 'ready');
    const optional = cov.filter((c) => c.state === 'absent');
    const f = model.folder;
    tabCount.textContent = needed.length ? String(needed.length) : '';
    const keep = root.scrollTop;
    clear(root);

    root.append(
      h('div', { class: 'src-head' },
        h('div', { class: 'big' }, !S.workspace.files ? 'No folder connected' : needed.length ? `${needed.length} required ${needed.length === 1 ? 'source is' : 'sources are'} missing` : 'Everything required is in'),
        h('div', { class: 'small' }, `${ready.length} of ${cov.length}`)),
      h('div', { class: 'watching' },
        h('span', { class: 'dot', 'data-s': f.name && !f.needsPermission ? 'ok' : '' }),
        h('span', { id: 'watch-text' }, watchText()),
        h('span', { class: 'sp' }),
        f.name ? h('button', { class: 'link', type: 'button', onclick: () => folder.pick() }, 'Change folder') : h('button', { class: 'link', type: 'button', onclick: () => folder.pick() }, 'Choose folder')));

    const group = (key, title, items) => items.length && h('div', { class: 'group', 'data-g': key }, h('h3', {}, title), items.map(slot));
    const slot = (c) => {
      const el = h('div', { class: 'slot' + (open.has(c.id) ? ' open' : ''), 'data-state': c.state },
        h('button', { type: 'button', 'aria-expanded': String(open.has(c.id)), onclick: () => { open.has(c.id) ? open.delete(c.id) : open.add(c.id); el.classList.toggle('open'); el.firstChild.setAttribute('aria-expanded', String(open.has(c.id))); } },
          h('span', { class: 'name' }, c.label),
          h('span', { class: 'state' }, c.state === 'ready' ? (c.count > 1 ? `${c.count} files` : '1 file') : c.state === 'missing' ? 'Needed' : 'Not provided'),
          svg(ICON.chev, { size: 14, cls: 'chev' })),
        h('div', { class: 'more' }, h('div', {}, h('div', { class: 'inner' },
          h('p', {}, c.hint, ' ', h('span', { class: 'small' }, `Goes in “${c.folder}”.`)),
          c.unlocks.length ? h('p', { class: 'small' }, `Feeds ${list(c.unlocks)}.`) : null,
          c.files.length ? h('ul', { class: 'files' }, c.files.map((p) => h('li', {}, c.modified.includes(p) ? h('span', { class: 'tag' }, 'new') : null, p.split('/').slice(1).join('/')))) : null))));
      return el;
    };
    const svc = S.services;
    const providerLine = (x, i) => h('div', { class: 'svc-row', 'data-state': x.state },
      h('span', {}, x.name === 'tavily' ? 'Tavily' : 'Brave', i === 0 && x.state !== 'unset' ? h('em', {}, 'default') : null),
      h('span', {}, x.state === 'unset' ? 'No key set' : x.state === 'ok' ? `Ready · ${x.used} used this month`
        : x.state === 'auth' ? 'Key rejected' : 'Credit used up · retrying automatically'));
    const conv = svc?.converter;
    root.append(
      group('needed', 'Needed', needed) || '',
      group('ready', 'Provided', ready) || '',
      group('optional', 'Optional', optional) || '',
      svc ? h('div', { class: 'group' }, h('h3', {}, 'Services'),
        h('div', { class: 'svc' }, h('div', { class: 'svc-title' }, 'Web search (for macro data)'), ...svc.search.map(providerLine)),
        h('div', { class: 'svc' }, h('div', { class: 'svc-title' }, 'PDF conversion'),
          h('div', { class: 'svc-row', 'data-state': conv.ready ? 'ok' : 'unset' },
            h('span', {}, conv.name === 'iloveapi' ? 'iLoveAPI' : conv.name === 'remote' ? 'LibreOffice service' : 'LibreOffice'),
            h('span', {}, !conv.ready ? (conv.name === 'iloveapi' ? 'No key set' : conv.name === 'remote' ? 'Not configured' : 'Not installed')
              : conv.name === 'iloveapi' ? `${conv.used} files this month${conv.detail ? ' · ' + conv.detail : ''}` : conv.name === 'remote' ? 'Separate service' : 'Runs on the server')))) : '',
      S.attachments?.length ? h('div', { class: 'group' }, h('h3', {}, 'Attached by you'),
        h('ul', { class: 'slot files', style: { padding: '6px 0 12px' } }, S.attachments.map((n) => h('li', {}, n)))) : '',
      S.unplaced?.length ? h('div', { class: 'group' }, h('h3', {}, 'Not recognised'),
        h('p', { class: 'small', style: { margin: '6px 0 8px' } }, 'These files are in your folder, but no part of the report uses them.'),
        h('ul', { class: 'slot files', style: { padding: '0 0 12px' } }, S.unplaced.slice(0, 20).map((p) => h('li', {}, p)))) : '');
    root.scrollTop = keep;
  };

  const watchText = () => {
    const f = model.folder;
    if (!f.name) return 'Choose the folder where you keep this quarter’s files.';
    if (f.needsPermission) return `“${f.name}” needs your permission to be read again.`;
    if (f.phase === 'error') return `Could not read “${f.name}”: ${f.error}`;
    if (!f.supported && f.phase === 'manual') return `“${f.name}” · choose it again to look for changes`;
    return `Watching “${f.name}”${f.checkedAt ? ' · checked ' + fmtAge(f.checkedAt) : ''}`;
  };
  subscribe((what) => {
    if (what === 'state' || (what === 'folder' && ['none', 'needs_permission', 'error'].includes(model.folder.phase))) render();
    if (what === 'folder') { const t = root.querySelector('#watch-text'); if (t) t.textContent = watchText(); }
  });
  setInterval(() => { const t = root.querySelector('#watch-text'); if (t) t.textContent = watchText(); }, 10000);
  render();
}
