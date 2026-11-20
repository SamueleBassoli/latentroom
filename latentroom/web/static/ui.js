// Shared helpers for every screen module (Develop, Library, Models, Settings).
export const $ = (s, root = document) => root.querySelector(s);
export const $$ = (s, root = document) => [...root.querySelectorAll(s)];
// The space before the dot is inside the nowrap span, the one after is not: a line can end with
// the separator but never start with it. Text stays " · ".
export const SEP = '<span class="sep"> ·</span> ';
export const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
// Where people report problems and get new versions.
// The same value as latentroom.REPO in Python: a test keeps them equal.
export const REPO = 'github.com/SamueleBassoli/latentroom';
// Every external address on a page is this link, its text the address itself, still selectable.
// target=_blank: pywebview's EdgeChromium window hands the new-window request to the system browser
// (OPEN_EXTERNAL_LINKS_IN_BROWSER, on by default), never to the app window; on a phone it is a new tab.
// Only constant addresses go in (REPO, nvidia.com), never text from the server.
export const extLink = addr => `<a class="ext" href="https://${esc(addr)}" target="_blank" rel="noopener noreferrer">${esc(addr)}</a>`;
// The rule at every 18+ question, and the Qwen licence in its own words.
// README says the same words: a test keeps them equal.
export const USE_RULE = 'You are responsible for what you make. No real people without their consent. Nobody under 18. Nothing illegal where you live.';
export const QWEN_LICENCE = `Qwen-Image models: research or evaluation use only, no commercial use. Licence: ${extLink('huggingface.co/Qwen/Qwen-Image-2.1/blob/main/LICENSE')}`;
export const icon = (name, cls = '') => `<svg class="i ${cls}"><use href="#i-${name}"/></svg>`;

// A phone reaches Latentroom over the network: "close the window" is wrong advice there.
const fallback = () => PHONE.matches
  ? 'Latentroom on your PC is not answering. Check that the PC is on and Latentroom is open.'
  : 'Latentroom is not answering. Close the window and open Latentroom again.';

let lostCount = 0;
// Calls the local API. Resolves with the JSON answer; throws an Error whose message is the
// plain sentence from the server ({"error": {"message"}}), so callers can toast it as is.
export async function api(path, body, method) { // path = full '/api/...' path
  const opt = { method: method || (body === undefined ? 'GET' : 'POST'), headers: {} };
  if (body instanceof Blob) { opt.body = body; opt.headers['Content-Type'] = body.type; }
  else if (body !== undefined) { opt.body = JSON.stringify(body); opt.headers['Content-Type'] = 'application/json'; }
  let res;
  try { res = await fetch(path, opt); } catch {
    if (++lostCount === 2) window.dispatchEvent(new Event('lr-lost')); // 2 failed calls in a row
    throw new Error(fallback());
  }
  lostCount = 0;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { const e = new Error(data.error?.message || fallback()); e.code = data.error?.code; e.status = res.status; throw e; }
  return data;
}

let toastTimer = 0, toastTick = 0;
export function toast(msg, ms = 4000) { // ms 0: stays until the next toast
  const t = $('#toast');
  t.textContent = msg; t.classList.add('show'); clearToast(t);
  clearInterval(toastTick); toastTick = setInterval(clearToast, 100, t); // a panel may open under it
  clearTimeout(toastTimer); if (ms) toastTimer = setTimeout(() => { t.classList.remove('show'); clearInterval(toastTick); }, ms);
}
// Lost connection, short form: a toast that stays, a try every 2 s, "Connected again." on the
// first answer of any kind.
let lostTimer = 0;
window.addEventListener('lr-lost', function lost() {
  toast(PHONE.matches
    ? 'Lost the connection to Latentroom on your PC. Trying again every 2 seconds. Check that the PC is on.'
    : 'Lost the connection to Latentroom. Trying again every 2 seconds. Pictures already saved are safe.', 0);
  clearTimeout(lostTimer);
  lostTimer = setTimeout(() => fetch('/api/version').then(() => { lostCount = 0; toast('Connected again.'); }, lost), 2000);
});
// The toast never covers a button the user can see (phone Library "Save to phone",
// phone Develop button): from its CSS place it moves up, above every button it would cover.
// It never leaves the window: with no free place on screen (a full Library grid, every
// thumbnail a button) it stays at its CSS place.
function clearToast(t) {
  t.style.bottom = '';
  const seen = $$('button, .btn, [role=tab]').map(b => [b, b.getBoundingClientRect()])
    .filter(([b, r]) => r.width && b.contains(document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)))
    .map(([, r]) => r);
  for (;;) {
    const r = t.getBoundingClientRect(), dy = new DOMMatrix(getComputedStyle(t).transform).m42; // slide-in offset
    const hit = seen.filter(b => b.left < r.right && b.right > r.left && b.top < r.bottom - dy && b.bottom > r.top - dy);
    if (!hit.length) return;
    const top = Math.min(...hit.map(b => b.top));
    if (top - 8 - r.height < 8) { t.style.bottom = ''; return; }
    t.style.bottom = `${innerHeight - top + 8}px`; // moves up every pass
  }
}

// ---------- modal dialog (shared by Library, Models, Settings) ----------
// The first button (or [data-first]) gets focus, Tab stays inside, Esc / scrim click = cancel,
// focus returns to the opener. onClick gets every click inside the box.
let opener = null;
export function openDialog(html, onClick) {
  opener = document.activeElement;
  const d = $('#dlg'); d.innerHTML = html; d.onclick = onClick || null;
  $('#dialog').hidden = false; (d.querySelector('[data-first]') || d.querySelector('button'))?.focus();
}
export function closeDialog(refocus = true) {
  if ($('#dialog').hidden) return;
  $('#dialog').hidden = true; $('#dlg').innerHTML = '';
  if (refocus && opener?.isConnected) opener.focus();
}
export const dlgActs = (cancel, ok) => `<div class="acts"><button class="btn ghost" type="button" data-close data-first>${cancel}</button>${ok}</div>`;
// Yes/no dialog: resolves true when the [data-yes] button is pressed.
export function confirmDialog(title, text, yes, no = 'Cancel') {
  return new Promise(done => openDialog(`<h2 id="dlg-title">${title}</h2><p>${text}</p>${dlgActs(no, yes)}`, e => {
    if (e.target.closest('[data-close]')) { closeDialog(); done(false); }
    if (e.target.closest('[data-yes]')) { closeDialog(false); done(true); }
  }));
}
const cancelDialog = () => { const c = $('#dlg [data-close]'); if (c) c.click(); else closeDialog(); };
$('#dialog').addEventListener('mousedown', e => { if (e.target.id === 'dialog') cancelDialog(); });
document.addEventListener('keydown', e => {
  if ($('#dialog').hidden) return;
  if (e.key === 'Escape') { e.preventDefault(); cancelDialog(); }
  if (e.key === 'Tab') {
    const f = $$('#dlg :is(button, input, select, a[href]):not(:disabled)'); if (!f.length) return;
    const i = f.indexOf(document.activeElement);
    if (e.shiftKey && i <= 0) { e.preventDefault(); f.at(-1).focus(); } else if (!e.shiftKey && i === f.length - 1) { e.preventDefault(); f[0].focus(); }
  }
});

export function radio(group, btn) { $$(group + ' [role=radio]').forEach(b => b.setAttribute('aria-checked', String(b === btn))); }

// Left/Right arrows move the choice inside a segmented control.
export function arrowKeys(group) {
  $(group).addEventListener('keydown', e => {
    if (!['ArrowLeft', 'ArrowRight'].includes(e.key)) return;
    const bs = $$(group + ' [role=radio]'), i = bs.findIndex(b => b.getAttribute('aria-checked') === 'true');
    const n = bs[(i + (e.key === 'ArrowRight' ? 1 : bs.length - 1)) % bs.length];
    n.click(); n.focus(); e.preventDefault();
  });
}

// "2m", "1h", "yesterday", "4d" for history rows.
export function ago(unix) {
  const s = Math.max(0, Date.now() / 1000 - unix);
  if (s < 60) return 'now';
  if (s < 3600) return Math.floor(s / 60) + 'm';
  if (s < 86400) return Math.floor(s / 3600) + 'h';
  return s < 172800 ? 'yesterday' : Math.floor(s / 86400) + 'd';
}

// The ONE size formatter (SPEC "Sizes"): 1 GB = 1024^3 bytes, as Windows Explorer and Task Manager show, like hardware.gb.
export const GiB = 2 ** 30;
export const gb = (bytes, digits = 1) => (bytes / GiB).toFixed(digits).replace(/\.0$/, ''); // number only: "32", "4.2"; digits=2 keeps "1.00"
export const sizeGB = b => b > 0 ? gb(Math.max(b, GiB / 100), 2) + ' GB' : ''; // file sizes; 0 or unknown shows nothing, never "0.00 GB"

// Fit line words: [dot class, line 1, line 2]. Develop, Models, wizard.
export function fitLine(f) {
  if (f.label === 'full_gpu') return ['ok', 'Full GPU at this size', 'Fastest.'];
  if (f.label === 'too_big') return ['danger', 'Too big for this PC at this size', 'Pick a smaller size or a lighter model.'];
  return ['warn', 'Partial GPU offload at this size', 'Works, but slower.'];
}

// Model chip / meta line name, e.g. "Qwen-Image 2.1 Turbo" + quant "Q4_K".
export function modelName(m) {
  const fam = { standard: '', uncensored: ' Uncensored', turbo: ' Turbo', turbo_uncensored: ' Turbo Uncensored' };
  return 'Qwen-Image 2.1' + (fam[m.family] ?? '');
}
// "Balanced model", "Balanced uncensored model": Models toasts and the phone model line (no quant code, 11.2)
export const modelLabel = m => `${m.pair.label}${m.family === 'uncensored' ? ' uncensored' : ''} model`;

// Phone layout: the same width as phone.css (layout.bp-phone-max)
export const PHONE = matchMedia('(max-width: 599px)');

// Save to phone: a plain download link, file name as on the PC; the phone browser says the rest.
export const saveLink = (e, cls = 'sm') => `<a class="btn ${cls} only-phone" data-save href="/api/history/${e.id}/image" download="${esc(e.image.split(/[\\/]/).pop())}">${icon('download', 'sm')}Save to phone</a>`;

// Copies the diagnostic report (no prompts, no pictures) to the clipboard.
export async function copyDiagnostics() {
  let text;
  try { ({ text } = await api('/api/diagnostics')); } catch (e) { return toast(e.message); }
  try {
    await navigator.clipboard.writeText(text);
    toast(`Diagnostic report copied. Open an issue at ${REPO}/issues and paste it there.`); // a toast is not clickable: plain text
  } catch { toast('Could not copy the report. Click in the Latentroom window and try again.'); } // browser text is not plain English
}

// Native folder picker (pywebview, app.py Api.pick_folder). Resolves with the folder, or '' when closed.
export async function pickFolder(start = '') {
  if (!window.pywebview) { toast('Picking a folder works only in the Latentroom window.'); return ''; }
  return (await window.pywebview.api.pick_folder(start)) || '';
}
