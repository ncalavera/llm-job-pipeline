import {escHtml, progressStage} from './helpers.js';
import {TRIAGE_COLUMNS} from './state.js';
const statusLabel = value => TRIAGE_COLUMNS.find(c => c.key === progressStage(value))?.label || ({unseen:'Undecided',passed:'Passed',archived:'Archived'}[value]) || value;

// Keep unfinished edits across dashboard refreshes and vacancy navigation.
const drafts = new Map();

export function applicationNotesHtml() {
  return `<details class="vac-desc-block application-notes"><summary class="vac-section-label">Application steps &amp; history</summary>
    <p>List this employer’s steps and add dated updates. Mark each step Planned, In progress, Done, or Cancelled. Leave unknown dates blank.</p>
    <p>Example: Planned — recruiter interview<br>Done — application sent — 2026-09-08</p>
    <label>Steps and notes<textarea maxlength="50000" rows="12" style="width:100%;box-sizing:border-box" disabled></textarea></label>
    <button type="button" class="vac-btn" disabled>Save notes</button> <span role="status">Loading…</span><div class="application-events"></div></details>`;
}

export async function loadApplicationNotes(host, id) {
  const panel = host.querySelector('.application-notes');
  if (!panel) return;
  const input = panel.querySelector('textarea');
  const button = panel.querySelector('button');
  const message = panel.querySelector('[role="status"]');
  let original;
  try {
    const res = await fetch('/api/application-notes?id=' + encodeURIComponent(id), {cache:'no-store'});
    if (!res.ok) throw new Error('Application notes could not be loaded. Reload to retry.');
    const data = await res.json();
    if (!panel.isConnected) return;
    original = data.notes;
    panel.querySelector(".application-events").innerHTML = (data.events || []).map(e => `<p>${escHtml(e.recorded_at)} — recorded: ${escHtml(statusLabel(e.previous_status))} → ${escHtml(statusLabel(e.status))}</p>`).join("");
    input.value = drafts.get(id) ?? original;
    input.disabled = button.disabled = false;
    message.textContent = drafts.has(id) ? 'Unsaved draft restored' : '';
    input.addEventListener('input', () => {
      drafts.set(id, input.value);
      message.textContent = 'Unsaved changes';
    });
    button.addEventListener('click', async () => {
      const submitted = input.value;
      button.disabled = true;
      message.textContent = 'Saving…';
      try {
        const saved = await fetch('/api/application-notes', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({id, notes:submitted, expected_notes:original}),
        });
        const result = await saved.json();
        if (!saved.ok) throw new Error(result.error || 'Save failed');
        original = result.notes;
        if (drafts.get(id) === submitted) drafts.delete(id);
        message.textContent = input.value === submitted ? 'Saved' : 'Unsaved changes';
      } catch (err) { message.textContent = err.message; }
      finally { button.disabled = false; }
    });
  } catch (err) { message.textContent = err.message; }
}
