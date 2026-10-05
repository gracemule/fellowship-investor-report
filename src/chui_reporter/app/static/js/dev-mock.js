// Development only (?mock=1, and the server must run with CHUI_ENV=dev).
// A virtual folder served from the dev machine's data directory, so the real sync code path
// (walk -> hash -> plan -> upload -> commit) can be exercised without a native folder picker.

export async function mockSource() {
  const res = await fetch('/api/dev/tree', { credentials: 'same-origin' });
  if (!res.ok) throw new Error('The development folder is not available (is the server in dev mode?)');
  const tree = await res.json();
  return {
    name: tree.name, watch: true,
    async *walk() {
      for (const f of tree.files) {
        yield { path: f.path, size: f.size, mtime: f.mtime,
          file: async () => new File([await (await fetch(`/api/dev/file?path=${encodeURIComponent(f.path)}`)).blob()],
                                      f.path.split('/').pop(), { lastModified: f.mtime }) };
      }
    },
  };
}
