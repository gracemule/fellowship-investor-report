// Every request goes through here so a lost session shows the login screen, not a broken page.

let onAuthLost = () => {};
export const setAuthHandler = (fn) => { onAuthLost = fn; };

export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function parse(res) {
  if (res.status === 401) { onAuthLost(); throw new ApiError('Please sign in again.', 401); }
  let body = null;
  try { body = await res.json(); } catch { /* not JSON */ }
  if (!res.ok) throw new ApiError((body && (body.detail || body.error || body.reason)) || `Request failed (${res.status})`, res.status);
  return body;
}

export const get = (url) => fetch(url, { credentials: 'same-origin' }).then(parse);
export const post = (url, data = {}) => fetch(url, {
  method: 'POST', credentials: 'same-origin',
  headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data),
}).then(parse);
export const put = (url, body) => fetch(url, { method: 'PUT', credentials: 'same-origin', body }).then(parse);
