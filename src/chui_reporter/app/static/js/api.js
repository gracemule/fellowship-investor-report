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
// `timeout` (ms) is for calls that must not hang silently: a server that is overloaded may not answer at all.
export const post = (url, data = {}, { timeout } = {}) => {
  const ctl = timeout ? new AbortController() : null;
  const timer = ctl && setTimeout(() => ctl.abort(), timeout);
  return fetch(url, {
    method: 'POST', credentials: 'same-origin', signal: ctl?.signal,
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data),
  }).then(parse, (e) => {
    if (e.name === 'AbortError') throw new ApiError('The server did not answer. It may be overloaded: wait a minute and try again.', 0);
    throw e;
  }).finally(() => clearTimeout(timer));
};
export const put = (url, body) => fetch(url, { method: 'PUT', credentials: 'same-origin', body }).then(parse);
