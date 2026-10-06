// What the agent has to work with: each kind of source document, whether it is in, and what it
// feeds. This answers "does the agent have what it needs?" without a file listing.

import { post } from './api.js';
import * as folder from './folder.js';
import { emit, model, subscribe } from './live.js';
import { ICON, ago, clear, h, list, plural, svg, toast } from './util.js';

const open = new Set();
let confirmClear = false;
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

    const srv = S.workspace, label = S.period.label;
    const none = !srv.files;
    root.append(
      h('div', { class: 'src-head' },
        h('div', { class: 'big' }, none ? (srv.last_sync_at ? `${srv.folder ? `“${srv.folder}”` : 'The folder'} has no ${label} files` : `No folder for ${label} yet`)
          : needed.length ? `${needed.length} required ${needed.length === 1 ? 'source is' : 'sources are'} missing` : 'Everything required is in'),
        h('div', { class: 'small' }, `${ready.length} of ${cov.length}`)),
      h('div', { class: 'watching' },
        h('span', { class: 'dot', 'data-s': f.name && !f.needsPermission ? 'ok' : '' }),
        h('span', { id: 'watch-text' }, watchText()),
        h('span', { class: 'sp' }),
        h('button', { class: 'link', type: 'button', onclick: () => folder.pick() }, f.name || srv.folder ? 'Change folder' : 'Choose folder')));

    const group = (key, title, items) => items.length && h('div', { class: 'group', 'data-g': key }, h('h3', {}, title), items.map(slot));
    const slot = (c) => {
      const el = h('div', { class: 'slot' + (open.has(c.id) ? ' open' : ''), 'data-state': c.state },
        h('button', { type: 'button', 'aria-expanded': String(open.has(c.id)), onclick: () => { open.has(c.id) ? open.delete(c.id) : open.add(c.id); el.classList.toggle('open'); el.firstChild.setAttribute('aria-expanded', String(open.has(c.id))); } },
          h('span', { class: 'name' }, c.label),
          h('span', { class: 'state' }, c.state === 'ready' ? (c.durable ? 'Kept for every quarter' : c.note && !c.count ? 'From the report' : c.count > 1 ? `${c.count} files` : '1 file') : c.state === 'missing' ? 'Needed' : 'Not provided'),
          svg(ICON.chev, { size: 14, cls: 'chev' })),
        h('div', { class: 'more' }, h('div', {}, h('div', { class: 'inner' },
          h('p', {}, c.hint, ' ', h('span', { class: 'small' }, `Goes in “${c.folder}”.`)),
          c.unlocks.length ? h('p', { class: 'small' }, `Feeds ${list(c.unlocks)}.`) : null,
          c.durable ? h('p', { class: 'small' }, 'Added once. It stays for every quarter until you change it, so you never upload it again.') : null,
          c.note ? h('p', { class: 'small' }, c.note + '. Nothing to upload.') : null,
          c.files.length ? h('ul', { class: 'files' }, c.files.map((p) => h('li', {}, c.modified.includes(p) ? h('span', { class: 'tag' }, 'new') : null, p.split('/').slice(1).join('/')))) : null))));
      return el;
    };
    const manage = (S) => {
      const srv = S.workspace, here = S.period.code;
      const move = async (to, toLabel) => {
        try {
          const r = await post('/api/sources/move', { to, period: here });
          await folder.moveSaved(here, to);
          model.state = r.state; emit('state');
          toast(`Moved ${plural(r.moved, 'file')} to ${toLabel}. Open that quarter to see them.`);
        } catch (e) { if (e.status !== 401) toast(e.message, 'bad'); }
      };
      const clearAll = async () => {
        try {
          const r = await post('/api/sources/clear', { period: here });
          await folder.forget();
          confirmClear = false; model.state = r.state; emit('state');
          toast(`Removed ${plural(r.removed, 'file')}. Choose this quarter’s folder when you are ready.`);
        } catch (e) { if (e.status !== 401) toast(e.message, 'bad'); }
      };
      return h('div', { class: 'group manage' }, h('h3', {}, `${S.period.label} files`),
        h('p', { class: 'small', style: { margin: '6px 0 10px' } },
          `${plural(srv.files, 'file')}${srv.folder ? ` from “${srv.folder}”` : ''}${srv.last_sync_at ? `, synced ${ago(srv.last_sync_at)}` : ''}. Synced the wrong quarter? Move them, or remove them and start again. The brand kit is never touched.`),
        h('div', { class: 'manage-actions' },
          h('button', { class: 'link', type: 'button', onclick: () => move(S.period.prev_code, S.period.prev) }, `Move to ${S.period.prev}`),
          h('button', { class: 'link', type: 'button', onclick: () => move(S.period.next_code, S.period.next) }, `Move to ${S.period.next}`),
          confirmClear
            ? h('span', { class: 'confirm' }, `Remove ${plural(srv.files, 'file')}? `,
                h('button', { class: 'link danger', type: 'button', onclick: clearAll }, 'Yes, remove'),
                h('button', { class: 'link', type: 'button', onclick: () => { confirmClear = false; render(); } }, 'Cancel'))
            : h('button', { class: 'link danger', type: 'button', onclick: () => { confirmClear = true; render(); } }, 'Remove synced files')));
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
      srv.files ? manage(S) : '',
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
    const f = model.folder, S = model.state, srv = S?.workspace;
    if (f.name && !f.needsPermission) {
      if (f.phase === 'error') return `Could not read “${f.name}”: ${f.error}`;
      const bare = srv && !srv.files ? ' · it holds no files for this quarter yet' : '';
      return `Connected to “${f.name}”${bare}${f.checkedAt ? ' · checked ' + fmtAge(f.checkedAt) : ''}`;
    }
    if (f.needsPermission) return `“${f.name}” needs your permission to be read again.`;
    if (srv?.folder && srv.last_sync_at) return `Synced from “${srv.folder}” ${ago(srv.last_sync_at)}. Choose it again here to keep watching for changes.`;
    return `Choose the folder where you keep ${S ? S.period.label + '’s' : 'this quarter’s'} files.`;
  };
  subscribe((what) => {
    if (what === 'state' || what === 'quarter' || (what === 'folder' && ['none', 'needs_permission', 'error', 'watching', 'manual'].includes(model.folder.phase))) render();
    if (what === 'folder') { const t = root.querySelector('#watch-text'); if (t) t.textContent = watchText(); }
  });
  setInterval(() => { const t = root.querySelector('#watch-text'); if (t) t.textContent = watchText(); }, 10000);
  render();
}
