'use strict';
const $ = id => document.getElementById(id);
let selectedJob = null, jobs = [], pollTimer, mediaURL, requestPending = false;
const modes = {
  story: { hint: "Introduce a character, a setting and a little possibility. We'll build the story arc.", placeholder: 'A tiny robot discovers a glowing garden on Mars. Tell a warm, cinematic story about curiosity and unexpected friendship.' },
  brand: { hint: "Tell us what you're promoting. Include approved prices, currency and offer dates; unknown facts stay unspecified.", placeholder: 'Introduce a summer collection of canvas sneakers, sunglasses and beach bags. Use a bright, relaxed mood and invite viewers to explore the collection.' },
  knowledge: { hint: 'Name a topic and your audience. Add verified context below; generated explanations still need factual review.', placeholder: 'Explain how solar panels turn sunlight into electricity for a curious beginner, using simple examples and a clear visual progression.' }
};
const examples = [
  { mode: 'story', text: modes.story.placeholder },
  { mode: 'brand', text: modes.brand.placeholder },
  { mode: 'knowledge', text: 'Explain why some ocean waves glow at night. Introduce bioluminescent plankton for a curious beginner, with calm, cinematic ocean imagery.' }
];
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => $('toast').hidden = true, 6000); }
async function api(path, options = {}) {
  const token = sessionStorage.getItem('studio-token');
  const headers = { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(token ? { Authorization: `Bearer ${token}` } : {}), ...options.headers };
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let message = `Request failed (${response.status}).`;
    try { const body = await response.json(); message = typeof body.detail === 'string' ? body.detail : 'Check your prompt, duration and settings, then try again.'; } catch {}
    throw new Error(message);
  }
  return response;
}
function switchView(view) {
  ['create', 'library', 'guide'].forEach(name => $('view-' + name).hidden = name !== view);
  document.querySelectorAll('.nav-item').forEach(button => button.classList.toggle('selected', button.dataset.view === view));
  $('breadcrumb').textContent = { create: 'Create a video', library: 'My library', guide: 'How it works' }[view];
  if (view === 'library') refreshLibrary().catch(error => toast(error.message));
}
function updateMode() { const config = modes[$('mode').value]; $('prompt').placeholder = config.placeholder; $('mode-guidance').textContent = config.hint; }
function duration() { return Number($('duration').value === 'custom' ? $('custom-duration').value : $('duration').value); }
function estimate() { const seconds = duration(); if (!Number.isFinite(seconds)) return; $('estimate').textContent = `Approx. ${Math.max(4, Math.round(seconds / 30 * 2))}–${Math.max(15, Math.round(seconds / 30 * 7))} min to create`; }
function tickPrompt() { $('prompt-count').textContent = `${$('prompt').value.length.toLocaleString()} / 5,000`; }
document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', event => { event.preventDefault(); switchView(button.dataset.view); }));
document.querySelectorAll('[data-example]').forEach(button => button.addEventListener('click', () => { const example = examples[Number(button.dataset.example)]; $('mode').value = example.mode; $('prompt').value = example.text; updateMode(); tickPrompt(); $('prompt').focus(); }));
$('mode').addEventListener('change', updateMode);
$('prompt').addEventListener('input', tickPrompt);
$('duration').addEventListener('change', () => { $('custom-wrap').hidden = $('duration').value !== 'custom'; estimate(); });
$('custom-duration').addEventListener('input', estimate);
document.addEventListener('keydown', event => { if (event.key.toLowerCase() === 'n' && !event.ctrlKey && !event.metaKey && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName) && !$('settings').open) { event.preventDefault(); switchView('create'); $('prompt').focus(); } });
$('open-settings').addEventListener('click', () => { $('access-token').value = sessionStorage.getItem('studio-token') || ''; $('settings').showModal(); });
$('save-settings').addEventListener('click', () => { sessionStorage.setItem('studio-token', $('access-token').value.trim()); refreshLibrary().then(() => toast('Workspace connected.')).catch(error => toast(error.message)); });
$('create-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (requestPending) return;
  const seconds = duration();
  if (!Number.isInteger(seconds) || seconds < 1 || seconds > 300) { $('form-error').textContent = 'Choose a duration between 1 and 300 whole seconds.'; $('form-error').hidden = false; return; }
  requestPending = true; $('submit').disabled = true; $('form-error').hidden = true;
  try {
    const payload = { mode: $('mode').value, prompt: $('prompt').value, duration: seconds, details: $('details').value, voice: $('voice').value, label: $('label').value.trim() };
    const job = await (await api('/api/jobs', { method: 'POST', body: JSON.stringify(payload) })).json();
    selectJob(job); toast('Your brief is in the queue. All generation runs on Modal.');
    refreshLibrary().catch(() => {});
  } catch (error) { $('form-error').textContent = error.message; $('form-error').hidden = false; }
  finally { requestPending = false; $('submit').disabled = false; }
});
function selectJob(job) {
  clearTimeout(pollTimer); selectedJob = job; localStorage.setItem('frameforge-job', job.id); switchView('create');
  renderJob(job); if (['queued', 'running'].includes(job.status)) pollTimer = setTimeout(poll, 4000);
}
async function poll() {
  if (!selectedJob) return;
  try { const job = await (await api(`/api/jobs/${selectedJob.id}`)).json(); selectedJob = job; renderJob(job); if (['queued', 'running'].includes(job.status)) pollTimer = setTimeout(poll, 4000); else refreshLibrary().catch(() => {}); }
  catch (error) { $('progress-note').textContent = `Connection interrupted. Retrying shortly. ${error.message}`; pollTimer = setTimeout(poll, 8000); }
}
function renderJob(job) {
  $('empty-output').hidden = true; $('video-output').hidden = job.status !== 'completed'; $('job-progress').hidden = job.status === 'completed';
  $('output-tag').textContent = { completed: 'READY TO WATCH', running: 'CREATING ON MODAL', queued: 'IN THE QUEUE', failed: 'NEEDS ATTENTION', interrupted: 'INTERRUPTED' }[job.status];
  $('job-id').textContent = 'JOB / ' + job.id.slice(0, 8).toUpperCase(); $('stage-name').textContent = job.stage;
  $('progress-fill').style.width = job.progress + '%'; $('progress-track').setAttribute('aria-valuenow', job.progress);
  $('milestone').textContent = `Pipeline milestone ${job.progress}% · not a time guarantee`;
  $('elapsed').textContent = `${Math.floor(job.elapsed_seconds / 60)}m ${job.elapsed_seconds % 60}s elapsed`;
  $('progress-note').textContent = job.status === 'interrupted' ? 'A previous server session stopped. Check the Modal dashboard before resuming to avoid overlapping remote work.' : `Initial estimate: ${job.estimate.low_minutes}–${job.estimate.high_minutes} min, excluding queue time. You can return to this job from your library.`;
  $('job-error').hidden = !job.error; $('job-error').textContent = job.error || ''; $('retry').hidden = !['failed', 'interrupted'].includes(job.status);
  document.querySelectorAll('.pipeline-stages li').forEach((item, i) => item.classList.toggle('done', job.progress > [32, 55, 81, 96][i]));
  if (job.status === 'completed' && $('player').dataset.job !== job.id) loadVideo(job).catch(error => toast(error.message));
}
async function loadVideo(job) {
  $('player').dataset.job = job.id;
  try {
    const [media, reportResponse] = await Promise.all([api(`/api/jobs/${job.id}/assets/final.mp4`), api(`/api/jobs/${job.id}/assets/report.json`)]);
    const blob = await media.blob(), report = await reportResponse.json();
    if (!selectedJob || selectedJob.id !== job.id) return;
    if (mediaURL) URL.revokeObjectURL(mediaURL); mediaURL = URL.createObjectURL(blob); $('player').src = mediaURL;
    $('result-title').textContent = report.title || 'Your video is ready';
    $('result-meta').textContent = `${report.requested_duration_seconds}s · 720p · ${report.scene_count} scenes · ${report.narration_coverage_percent ?? '—'}% narration coverage`;
    $('review-note').textContent = `${report.review_warnings?.length ? `${report.review_warnings.length} review advisories recorded. ` : ''}Review product consistency, facts and narration before publishing. ${report.timing_warning || ''}`;
  } catch (error) { delete $('player').dataset.job; throw error; }
}
$('retry').addEventListener('click', async () => { if (!selectedJob) return; $('retry').disabled = true; try { selectJob(await (await api(`/api/jobs/${selectedJob.id}/retry`, { method: 'POST' })).json()); } catch (error) { toast(error.message); } finally { $('retry').disabled = false; } });
async function download(name) {
  if (!selectedJob) return;
  try { const blob = await (await api(`/api/jobs/${selectedJob.id}/assets/${name}`)).blob(); const url = URL.createObjectURL(blob); const anchor = document.createElement('a'); anchor.href = url; anchor.download = name === 'final.mp4' ? `frameforge-${selectedJob.id.slice(0, 8)}.mp4` : name; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 10000); }
  catch (error) { toast(error.message); }
}
$('download').addEventListener('click', () => download('final.mp4'));
$('download-report').addEventListener('click', () => download('report.json'));
async function refreshLibrary() {
  jobs = await (await api('/api/jobs')).json(); $('library-count').textContent = jobs.length; $('library-grid').replaceChildren();
  if (!jobs.length) { const empty = document.createElement('div'); empty.className = 'panel library-card'; const title = document.createElement('h3'); title.textContent = 'Your first frame is waiting.'; const copy = document.createElement('p'); copy.textContent = 'Create a video to start your library. No demo results or placeholder jobs are shown.'; empty.append(title, copy); $('library-grid').append(empty); }
  jobs.forEach(job => {
    const card = document.createElement('button'); card.className = 'panel library-card';
    const state = document.createElement('span'); state.className = 'job-state'; state.textContent = job.status.toUpperCase() + ' / ' + job.payload.mode.toUpperCase();
    const title = document.createElement('h3'); title.textContent = job.payload.prompt.length > 100 ? job.payload.prompt.slice(0, 100) + '…' : job.payload.prompt;
    const meta = document.createElement('p'); meta.textContent = `${job.payload.duration}s · ${new Date(job.created * 1000).toLocaleDateString()} · ${job.id.slice(0, 8)} ↗`;
    card.append(state, title, meta); card.addEventListener('click', () => selectJob(job)); $('library-grid').append(card);
  });
}
async function init() {
  try { await refreshLibrary(); const saved = localStorage.getItem('frameforge-job'); if (saved) { const job = jobs.find(item => item.id === saved); if (job) selectJob(job); } }
  catch (error) { toast(error.message); }
}
init();
