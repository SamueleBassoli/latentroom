// Latentroom shell: icon sprite, tabs + hash router, top bar (model chip, VRAM meter), screen start-up.
import { $, api, gb, modelName, toast, PHONE } from './ui.js';
import { initDevelop } from './develop.js';
import { initLibrary } from './library.js';
import { initModels } from './models.js';
import { initSettings } from './settings.js';
import { initWizard } from './wizard.js';
import { initProblem } from './problem.js';
import { initPhone, initPair } from './phone.js';

// Shared app state every screen reads. Screens call app.refresh() after changing settings/models.
const app = {
  settings: {},
  models: {}, // Model.id -> Model (+ fit, installed, pair) from /api/catalogue
  model() { return this.models[this.settings.model_id] || null; },
  async refresh() {
    const [settings, cat] = await Promise.all([api('/api/settings'), api('/api/catalogue')]);
    this.settings = settings; this.models = {}; this.files = cat.files || []; // shared parts (upscaler, face detector, ...)
    this.size = cat.size; // largest square size the recommended pair runs at
    this.ready = cat.ready; // the engine and a picture model are on disk
    for (const pair of cat.pairs) for (const side of ['standard', 'uncensored']) if (pair[side]) this.models[pair[side].id] = { ...pair[side], pair };
    chip();
    window.dispatchEvent(new Event('lr-model'));
  },
};

function chip() {
  const m = app.model();
  $('#chip-name').textContent = m ? modelName(m) : 'No model yet';
  $('#chip-q').textContent = m ? m.quant : '';
}

// read again every 2 s only while a job of ours or Enhance runs, then once more and stop
let vramTimer = 0;
async function vram() {
  clearTimeout(vramTimer);
  try {
    const hw = await api('/api/hardware');
    if (!hw.gpu) return;
    const used = hw.gpu.vram_total - hw.gpu.vram_free;
    $('#vram').hidden = false;
    $('#vram-bar').style.width = Math.round(used / hw.gpu.vram_total * 100) + '%';
    $('#vram-txt').textContent = `${gb(used)} / ${gb(hw.gpu.vram_total)} GB`;
  } catch { /* the meter is optional: stay hidden */ }
  clearTimeout(vramTimer); // two readings at once (a job ends while Enhance runs) keep one timer
  if (app.develop?.running() || $('#enh').getAttribute('aria-busy') === 'true') vramTimer = setTimeout(vram, 2000);
}

// ---------- router: #develop | #library | #models | #settings | #wizard (+ problem, pair: no hash) ----------
const SCREENS = { develop: null, library: null, models: null, settings: null, wizard: null, problem: null, pair: null };
const BARE = ['wizard', 'problem', 'pair']; // no top bar
const TABS = ['develop', 'library', 'models'];
let current = '';
function route() {
  let name = location.hash.slice(1).split('/')[0];
  if (!(name in SCREENS) || name === 'problem' || name === 'pair') name = 'develop';
  if (PHONE.matches) { if (name !== 'library') name = 'develop'; } // Models, Settings and the wizard are PC-only
  else if (app.settings.first_run_done === false) name = 'wizard'; // the wizard replaces every screen until it is done
  if (app.hw && app.hw.status !== 'ok') name = 'problem'; // hardware problems: checked before the wizard and Develop
  if (app.unpaired) name = 'pair'; // a phone without the pairing cookie
  $('#app').classList.toggle('bare', BARE.includes(name));
  if (BARE.includes(name) && current === name) return; // a hash change mid-setup must not restart it at step 1
  if (current && current !== name) SCREENS[current]?.hide?.(); // e.g. Settings stops its pairing timer
  current = name;
  for (const s in SCREENS) $('#screen-' + s).hidden = s !== name;
  for (const t of TABS) $('#tab-' + t).setAttribute('aria-selected', String(t === name));
  $('#btn-settings').setAttribute('aria-pressed', String(name === 'settings'));
  document.body.dataset.screen = name;
  if (name === 'models') vram();
  SCREENS[name]?.show?.();
}
window.addEventListener('hashchange', route);
$('#chip').onclick = () => { location.hash = '#models'; };
$('#btn-settings').onclick = () => { location.hash = location.hash === '#settings' ? '#develop' : '#settings'; };
document.addEventListener('keydown', e => {
  const i = ['1', '2', '3'].indexOf(e.key);
  if (BARE.includes(current)) return;
  if (e.ctrlKey && !e.shiftKey && !e.altKey && i >= 0) { e.preventDefault(); location.hash = '#' + TABS[i]; }
});

// ---------- start ----------
async function start() {
  // a picture that cannot load (file moved, deleted) leaves its grey box, not a broken-image icon
  document.addEventListener('error', e => { if (e.target instanceof HTMLImageElement) e.target.style.visibility = 'hidden'; }, true);
  // The icon sprite lives in its own file; <use href="#i-..."> needs it inside the page.
  const sprite = await fetch('/icons.svg').then(r => r.text()).catch(() => '');
  document.body.insertAdjacentHTML('afterbegin', sprite);
  SCREENS.pair = initPair(async () => {
    app.unpaired = false;
    await screens();
    toast('This phone is paired. Latentroom on your PC is ready.');
  });
  // an unpaired phone gets the pairing screen only: the other screens would start calling the API
  try { await api('/api/version'); } catch (e) { if (e.code === 'not_paired') { app.unpaired = true; return route(); } }
  await screens();
}
async function screens() {
  SCREENS.develop = app.develop = initDevelop(app); // Library fills Develop through app.develop
  SCREENS.library = initLibrary(app);
  SCREENS.models = initModels(app);
  SCREENS.settings = initSettings(app);
  SCREENS.wizard = initWizard(app);
  SCREENS.problem = initProblem(app, route);
  initPhone(app);
  try {
    [app.hw] = await Promise.all([api('/api/hardware'), app.refresh()]); // before route(): they pick the screen
    // setup was done, but the engine or every picture model is gone (an uninstall that
    // deleted them, or deleted by hand): the wizard again, as Settings > Run the setup again; it
    // gets only what is missing. Models kept (an update): Develop, as before.
    if (!PHONE.matches && app.settings.first_run_done && app.ready === false) location.hash = '#wizard';
  } catch (e) { toast(e.message); }
  if (app.size && app.size < 1024) app.develop.setSize(app.size, app.size); // the size the wizard proposes
  route();
  vram();
  window.addEventListener('lr-job-end', vram);
  window.addEventListener('lr-busy', vram); // a job or Enhance started
}
start();
