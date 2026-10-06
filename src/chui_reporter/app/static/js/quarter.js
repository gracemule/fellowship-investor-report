// Moving between quarters. Each quarter is a clean slate: its own folder, files, report, notes and conversation. Only the brand
// kit stands from quarter to quarter. The server does the switching; this makes the page follow it completely, so nothing of
// the previous quarter (its report, its notes, its feed, its connected folder) is left on screen.

import { post } from './api.js';
import * as folder from './folder.js';
import { emit, model, refreshNotes, refreshReport } from './live.js';
import { toast } from './util.js';

export async function setQuarter(code) {
  try { model.state = await post('/api/period', { period: code }); }
  catch (e) { if (e.status !== 401) toast(e.message, 'bad'); return false; }
  model.events = []; model.report = null; model.notes = null; model.viewingSession = null; model.replyTo = null;
  emit('state');
  await folder.switchQuarter();
  emit('quarter', model.state.period);
  await Promise.allSettled([refreshReport(), refreshNotes()]);
  return true;
}
