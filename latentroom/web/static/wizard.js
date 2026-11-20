// First-run wizard: Your PC, Data folder, Model, Download, Test picture.
// Shown while settings.first_run_done is false, and from Settings > "Run the setup again" (pre-filled).
// Step 3 speeds are the words the server sends (speed, fast_help): plain words, never a time or a factor.
import { $, SEP, esc, icon, api, toast, gb, sizeGB as GB, fitLine, modelName, pickFolder, copyDiagnostics, REPO, extLink, USE_RULE, QWEN_LICENCE } from './ui.js';
import { srow, pathLine, errLine } from './settings.js';
import { PART, SIDE_HELP, byPart, merge, dlLine, rowHelp, and } from './models.js';

const STEPS = ['Your PC', 'Data folder', 'Model', 'Download', 'Test picture'];
const SIDES = ['standard', 'uncensored'];
const PROMPT = 'a red lighthouse on a cliff at sunset, 35 mm film photo'; // SFW, no people (SPEC)
const END = ['done', 'failed', 'cancelled'];

export function initWizard(app) {
  const root = $('#screen-wizard');
  const W = { n: 1, hw: null, inst: null, cat: null, eng: null, files: {}, have: new Set(), pick: '', dataErr: '',
    rows: [], need: 0, dls: {}, unpack: false, dlErr: '', timer: 0, test: null,
    side: 'standard', det: false, more: false }; // step 3: the switch, Details, More models

  // ---------- data ----------
  const models = () => W.cat.pairs.flatMap(p => SIDES.filter(s => p[s]).map(s => ({ ...p[s], pair: p, side: s })));
  const usable = m => fitLine(m.fit)[0] !== 'danger' && (m.side === 'standard' || app.settings.adult_ok);
  const byId = id => models().find(m => m.id === id);
  // Not in catalogue.required, but setup gets them too, so Upscale ×4 and Turbo work without internet
  // afterwards: the upscaler, and the Turbo add-on unless the model is a Turbo model (jobs.py: no sigmas).
  const extras = m => W.cat.files.filter(f => (f.kind === 'upscaler' || (f.kind === 'lora' && !m.sigmas)) && !m.needs.includes(f.id));
  // Everything this model needs that is not on disk yet (= catalogue.required minus installed), plus the extras.
  const parts = m => [...m.needs.map(id => W.files[id]).filter(Boolean), ...extras(m)].sort(byPart);
  const missing = m => [...W.eng.files, ...parts(m), m.file].filter(f => !W.have.has(f.id));
  const bytes = list => list.reduce((n, f) => n + f.size, 0);
  async function loadInst() {
    W.inst = await api('/api/models');
    W.have = new Set(W.inst.installed);
  }
  async function load() {
    [W.hw, W.cat, W.eng] = await Promise.all([api('/api/hardware'), api('/api/catalogue'), api('/api/engine'), loadInst()]);
    const s = W.cat.size; // a small or busy card runs nothing at 1024 x 1024: show the fits at its size
    if (s && s < 1024) W.cat = await api(`/api/catalogue?width=${s}&height=${s}`);
    W.files = Object.fromEntries(W.cat.files.map(f => [f.id, f]));
    const now = byId(app.settings.model_id), rec = W.cat.pairs.find(p => p.id === W.cat.recommended)?.standard;
    W.pick = (now && usable(now) ? now : rec && usable(byId(rec.id)) ? rec : models().find(usable))?.id || '';
    W.side = byId(W.pick)?.side || 'standard'; W.det = W.more = false;
    if (byId(W.pick) && !plan().tiles.some(p => p[W.side]?.id === W.pick)) W.more = true; // the pick stays in sight
  }

  // ---------- frame ----------
  function frame(title, sub, body, foot) {
    const n = W.n;
    const steps = STEPS.map((s, i) => `<li${i + 1 === n ? ' aria-current="step"' : ''} class="${i + 1 < n ? 'done' : ''}"><span class="dot">${i + 1 < n ? icon('check', 'sm') : i + 1}</span><span class="nm">${s}</span></li>`).join('');
    return `<div class="wiz"><header class="whead"><span class="wlogo"><img src="/logo-horizontal.svg" alt="Latentroom" height="20"></span><ol class="wsteps" aria-label="Setup steps">${steps}</ol></header>
      <section class="wbody"><p class="label wnum">Step ${n} of 5</p><h1 class="t-title-page" id="wiz-title" tabindex="-1">${title}</h1><p class="sub">${sub}</p>${body}</section>
      <footer class="wfoot">${n > 1 && n < STEPS.length ? '<button class="btn ghost" type="button" data-w-back>Back</button>' : '<span></span>'}<div class="acts">${foot}</div></footer></div>`;
  }
  const verdict = t => `<div class="verdict"><span class="sdot ok"></span><span class="t-body-strong">${esc(t)}</span></div>`;
  const warn = t => `<div class="warnline">${icon('warn', 'sm')}<span>${esc(t)}</span></div>`; // never blocks Continue
  const next = (label, off, why = '') => `${off && why ? `<span class="help">${why}</span>` : ''}<button class="btn primary md" type="button" data-w-next${off ? ' disabled' : ''}>${label}</button>`;

  // ---------- step 1: Your PC ----------
  function stepPC() {
    const h = W.hw, g = h.gpu;
    const r = (n, v, note) => `<div class="srow"><div class="sl"><div class="n">${n}</div></div><div class="sc wval">${note != null ? '<span class="sdot ok"></span>' : ''}<span>${esc(v)}</span>${note ? `<span class="help">${esc(note)}</span>` : ''}</div></div>`;
    return frame('Welcome to Latentroom', 'Latentroom makes pictures on this PC, with its own graphics card. Nothing you type or make leaves this PC. Setup has 5 short steps and one big download.',
      `<div class="slist">${r('Graphics card', g.name, `${gb(g.vram_total)} GB video memory`)}${r('Driver', h.driver, 'New enough')}${r('Memory', `${gb(h.ram_total)} GB`)}</div>${verdict(h.message)}${h.warning ? warn(h.warning) : ''}`,
      next('Continue'));
  }

  // ---------- step 2: Data folder ----------
  function stepData() {
    const need = models().filter(usable).map(m => bytes(missing(m))), min = need.length ? Math.min(...need) : 0, max = need.length ? Math.max(...need) : 0;
    const free = W.inst.free, drive = W.inst.data_dir.slice(0, 2), low = free < min;
    const fit = low ? errLine(`Only ${GB(free)} free on ${drive}. Setup needs at least ${GB(min)}. Pick a folder on another drive, or free some space.`)
      : `<div class="fit"><span class="sdot ok"></span><span>${gb(free)} GB free on ${esc(drive)}</span><span class="c3 help">${max ? `Setup needs ${min === max ? GB(max) : `${GB(min)} to ${GB(max)}`}, depending on the model you pick next.` : 'Everything setup needs is already on this PC.'}</span></div>`;
    return frame('Where should models go?', 'Models are big files, 2 to 8 GB each. Pick a folder on a drive with room; any drive works.',
      `<div class="slist">${srow('Data folder', 'Models, the engine and the Library list are kept here.', '<button class="btn" type="button" data-w-folder>Change…</button>',
        pathLine(W.inst.data_dir, !app.settings.data_dir, 'data', 'data folder') + (W.dataErr ? errLine(W.dataErr) : fit + W.inst.data_warnings.map(warn).join('')))}
       ${srow('Pictures', 'New pictures are saved in your Pictures folder, under Latentroom. You can change this later in Settings.', '', pathLine(W.inst.output_dir, !app.settings.output_dir, 'output', 'output folder'))}</div>`,
      next('Continue', low, 'Pick a folder with more room to continue.'));
  }

  // ---------- step 3: Model ----------
  // Three tiles (the best levels that run well), the rest under More models, one Standard | Uncensored
  // switch, one Details toggle. Recommended is the server's (catalogue.recommend), as in Models.
  // "Runs well" is the server's too: speed is '' for Full GPU or a small partial slowdown.
  const runs = m => !!m && fitLine(m.fit)[0] !== 'danger';
  const well = m => runs(m) && !m.speed;
  function plan() {
    const sides = app.settings.adult_ok ? SIDES : ['standard'];
    const files = p => sides.map(s => p[s]).filter(Boolean); // every file the user can pick
    const shown = W.cat.pairs.filter(p => files(p).some(runs)); // too big on every side: hidden, counted
    const rec = shown.find(p => p.id === W.cat.recommended);
    const easy = shown.filter(p => !p.fast && files(p).every(well));
    let tiles = easy.slice(0, 3);
    const allWell = tiles.length > 0;
    if (!allWell) tiles = shown.filter(p => !p.fast).slice(0, 3); // small card: nothing runs well
    if (rec && !rec.fast && !tiles.includes(rec)) tiles.splice(Math.min(tiles.length, 2), 1, rec);
    tiles.sort((a, b) => shown.indexOf(a) - shown.indexOf(b));
    return { tiles, more: shown.filter(p => !tiles.includes(p)), rec, allWell, hidden: W.cat.pairs.length - shown.length };
  }
  const det = m => { // Details: the real model behind a quality name
    const ok = fitLine(m.fit)[0] === 'ok';
    return `<span class="det"><span>${esc(modelName(m))}</span><span class="c3">${esc(m.quant)}${SEP}${GB(m.file.size)}</span><span class="fit"><span class="sdot ${ok ? 'ok' : 'warn'}"></span><span>${ok ? 'Full GPU' : 'Partial GPU offload'}</span></span></span>`;
  };
  const pickInput = m => `<input type="radio" name="wpick" value="${esc(m.id)}"${W.pick === m.id ? ' checked' : ''}>`;
  function tile(p, rec) {
    const m = p[W.side], badge = p === rec ? '<span class="badge">Recommended</span>' : '';
    const why = !m ? `No ${W.side} file at this level.` : !runs(m) ? 'Too big for this PC.' : '';
    if (why) return `<div class="wtile off" aria-disabled="true"><span class="top"><span></span>${badge}</span><span class="q">${esc(p.label)}</span><p class="help">${why}</p></div>`;
    return `<label class="wtile">${pickInput(m)}<span class="top"><span class="rdot" aria-hidden="true"></span>${badge}</span><span class="q">${esc(p.label)}</span><p class="help">${rowHelp(p, W.cat)}</p>${W.det ? det(m) : ''}</label>`;
  }
  function row(p) {
    const m = p[W.side], slow = well(m) ? '<span></span>' : `<span class="slow"><span class="sdot warn"></span>${esc(m.speed)}</span>`;
    return `<label class="wrow">${pickInput(m)}<span class="rdot" aria-hidden="true"></span><span class="wrb"><span class="n">${esc(p.label)}</span>${p.fast ? '' : `<p class="help">${rowHelp(p, W.cat)}</p>`}${W.det ? det(m) : ''}</span>${slow}</label>`;
  }
  function moreBlock(pl) {
    const list = pl.more.filter(p => runs(p[W.side])), skipped = pl.more.filter(p => !p[W.side]);
    const names = [...skipped.filter(p => !p.fast).map(p => p.label), ...(skipped.some(p => p.fast) ? ['the Fast levels'] : [])];
    const miss = names.length ? `<p class="help">Uncensored is not available for ${esc(and(names))}.</p>` : '';
    if (!list.length) return miss;
    const std = list.filter(p => !p.fast), fast = list.filter(p => p.fast);
    const exp = fast.some(p => p[W.side].experimental) ? '<span class="badge quiet" title="Still being tested on real cards.">Experimental</span>' : '';
    const head = fast.length ? `<div class="wgh"><span class="label">Fast</span>${exp}<p class="help">${rowHelp(fast[0], W.cat)}</p></div>` : '';
    return `<div class="wmore"><button class="disclosure" type="button" aria-expanded="${W.more}" aria-controls="w-more" data-w-more>More models <span class="sum">${list.length}</span>${icon('chevron')}</button>
      <div id="w-more"${W.more ? '' : ' hidden'}><div class="wlist" role="radiogroup" aria-label="More models">${std.map(row).join('')}${head}${fast.map(row).join('')}</div></div>${W.more ? miss : ''}</div>`;
  }
  function stepModel() {
    const sub = 'This is the model Latentroom downloads now. You can add or swap models later in Models.';
    const pl = plan(), m = byId(W.pick), total = m ? bytes(missing(m)) : 0, sq = W.cat.size, g = W.hw.gpu;
    if (!pl.tiles.length && !pl.more.length) { // no model runs here at any size: say so, and a way on
      return frame('Pick a model', sub,
        errLine('No model fits this PC.', ' Its graphics card has too little video memory, or the PC too little free memory, for the lightest model. You can skip setup and look around; Settings has a diagnostic report to share.'),
        '<button class="btn" type="button" data-w-skip>Skip setup</button>');
    }
    const gate = `<div class="wgate"><div><div class="t-body-strong">Uncensored models <span class="badge quiet">18+</span></div><p class="help">Community models with no content filter. Only for people 18 or older. Latentroom remembers your answer on this PC.</p><p class="help">${USE_RULE}</p></div><button class="btn" type="button" data-w-adult>I am 18 or older, show them</button></div>`;
    const side = s => `<button type="button" role="radio" aria-checked="${W.side === s}"${W.side === s ? '' : ' tabindex="-1"'} data-w-side="${s}">${s === 'standard' ? 'Standard' : 'Uncensored <span class="badge quiet">18+</span>'}</button>`;
    const sw = `<div class="wside"><div class="seg words" role="radiogroup" aria-label="Model type">${SIDES.map(side).join('')}</div><span class="help">${SIDE_HELP[W.side]}</span></div>`;
    const head = `<div class="lab-row wtiles-h"><span class="l"><span class="label" id="w-tiles-l">${pl.allWell ? 'Runs well on this PC' : 'Works on this PC, but slower'}</span><span class="help">${g ? `${esc(g.name)}${SEP}${gb(g.vram_total)} GB video memory` : ''}</span></span><button class="btn ghost sm" type="button" aria-pressed="${W.det}" data-w-det>Details</button></div>`;
    const hidden = pl.hidden ? `<p class="help">${pl.hidden} model${pl.hidden > 1 ? 's are' : ' is'} too big for this PC and ${pl.hidden > 1 ? 'are' : 'is'} not shown.</p>` : '';
    const small = sq && sq < 1024 ? ` The graphics card in this PC has room for pictures up to ${sq} × ${sq}, so Develop starts at that size. You can change it there.` : '';
    return frame('Pick a model', sub + small,
      `${app.settings.adult_ok ? sw : gate}${head}<div class="wtiles" role="radiogroup" aria-labelledby="w-tiles-l">${pl.tiles.map(p => tile(p, pl.rec)).join('')}</div>${moreBlock(pl)}${hidden}<p class="help" id="w-licence">${QWEN_LICENCE}</p>`,
      `<span class="help" id="w-total">${!m ? 'Pick a model to continue.' : total ? `Download: ${GB(total)}` : 'Everything for this model is already on this PC.'}</span><button class="btn primary md" type="button" data-w-next${m ? '' : ' disabled'}>${total || !m ? 'Download' : 'Continue'}</button>`);
  }
  // The switch keeps the picked level when it has a file that runs on the new side, else Recommended's.
  function flip(s) {
    const lvl = byId(W.pick)?.pair, rec = W.cat.pairs.find(p => p.id === W.cat.recommended);
    W.side = s;
    W.pick = [lvl?.[s], rec?.[s]].find(runs)?.id || models().find(m => m.side === s && usable(m))?.id || '';
  }

  // ---------- step 4: Download ----------
  // One row per file group: engine zips, the parts the model needs + the extras, the model.
  function setupRows(m) {
    const part = f => { const [name, help] = PART[f.kind] || [f.name, '']; return { name, help, files: [f] }; };
    return [{ name: 'Picture engine', help: `stable-diffusion.cpp ${W.eng.release}`, files: W.eng.files, engine: true },
      ...parts(m).map(part),
      { name: `${m.pair.label} model`, help: `${modelName(m)} ${m.quant}`, files: [m.file] }];
  }
  const fileDone = f => W.have.has(f.id) || W.dls[f.id]?.status === 'done';
  const STOP = ['failed', 'cancelled'];
  function rowSt(r) {
    if (r.files.every(fileDone)) return `${icon('check', 'sm')}<span>Done</span>`;
    const st = r.files.map(f => W.dls[f.id]).filter(Boolean);
    if (st.some(d => STOP.includes(d.status))) return `<span class="bad">${icon('alert', 'sm')}Stopped</span>`;
    const done = st.reduce((n, d) => n + d.done, 0), p = Math.floor(100 * done / r.files.reduce((n, f) => n + f.size, 0));
    return p > 0 ? `<span>${p}%</span>` : '<span class="help">Waiting</span>';
  }
  function block() { // the one download for the whole setup: the files setup fetches (W.get), not old downloads of files on disk
    const st = [...W.get].map(id => W.dls[id]).filter(Boolean);
    const files = W.rows.every(r => r.files.every(fileDone)), all = files && W.eng.installed;
    const m = st.length ? merge(st) : { done: 0, total: 0, status: undefined };
    const total = m.total || W.need; // the sub says this same number
    const stop = st.some(d => STOP.includes(d.status)), paused = !stop && m.status === 'paused';
    const p = all || files ? 100 : m.total ? Math.floor(100 * m.done / m.total) : 0;
    const got = `${p}%${SEP}${gb(m.done, 2)}/${GB(m.total)}`;
    const line = all ? ['Done', GB(total)].filter(Boolean).join(SEP) : files ? (W.unpack ? 'Unpacking the picture engine…' : 'Getting the picture engine ready…')
      : stop ? `Stopped${SEP}${got}` : dlLine(m);
    const err = W.dlErr || (stop ? m.error || 'The download was cancelled. Press Try again to go on.' : '');
    return { all, files, stop, paused, p, line, err, total };
  }
  function stepDownload() {
    const b = block();
    const ctl = b.all || b.files || b.stop ? '' : `<button class="btn ghost icon sm" type="button" data-w-pause aria-label="${b.paused ? 'Resume' : 'Pause'} download" title="${b.paused ? 'Resume' : 'Pause'} download">${icon(b.paused ? 'play' : 'pause', 'sm')}</button>`;
    const row = (r, i) => `<div class="srow"><div class="sl"><div class="n">${esc(r.name)}</div><p class="help" data-p="h${i}">${r.engine && W.unpack ? 'Unpacking…' : esc(r.help)}</p></div><div class="sc wval"><span class="mono-line">${GB(r.files.reduce((n, f) => n + f.size, 0))}</span><span class="wst" data-p="s${i}">${rowSt(r)}</span></div></div>`;
    return frame(b.all ? 'Download complete' : 'Downloading', b.all ? 'Everything is on this PC. From now on Latentroom works without internet.'
      : `${b.total ? `About ${GB(b.total)}. ` : ''}You can use the PC meanwhile. If you close Latentroom, the download continues from where it stopped next time.`,
      `<div class="wdl"><div class="dl"><div class="bar${b.paused || b.stop ? ' paused' : ''}" role="progressbar" aria-label="Setup download" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${b.p}" data-p="bar"><i style="width:${b.p}%"></i></div><span class="line" data-p="line">${b.line}</span><div class="btns">${ctl}</div></div>
       ${b.err ? `${errLine(b.err)}<div><button class="btn" type="button" data-w-retry>${icon('redo', 'sm')}Try again</button></div>` : ''}</div>
       <div class="slist">${W.rows.map(row).join('')}</div>`,
      next('Continue', !b.all, 'Continue unlocks when everything is downloaded.'));
  }
  // What changes the controls (pause/play, error, done) redraws; the rest is painted in place, so focus stays.
  const shape = () => { const b = block(); return [b.all, b.files, b.stop, b.paused, b.err].join(); };
  function paint() {
    const t = document.createElement('template'); t.innerHTML = stepDownload();
    for (const el of t.content.querySelectorAll('[data-p]')) {
      const cur = root.querySelector(`[data-p="${el.dataset.p}"]`);
      if (cur && cur.outerHTML !== el.outerHTML) cur.replaceWith(el);
    }
  }
  function prepSetup(m) { // what step 4 shows and watches for this model
    const get = missing(m); W.rows = setupRows(m); W.dlErr = '';
    W.need = bytes(get); W.get = new Set(get.map(f => f.id));
    return get;
  }
  async function startSetup() {
    const m = byId(W.pick), old = W.get || new Set();
    prepSetup(m);
    // Back from step 4 and a different pick: pause what only the old pick needed, so it does not
    // download on unseen; the .part stays and Models can resume it. Same pick: the server
    // already skips a running download.
    await Promise.all([...old].filter(id => !W.get.has(id)).map(id => api(`/api/downloads/${encodeURIComponent(id)}/pause`, {}).catch(() => {})));
    const ups = extras(m).map(f => ({ file_id: f.id })); // not in catalogue.required
    try { if (W.need) for (const body of [{ model_id: m.id }, ...ups]) for (const d of (await api('/api/downloads', body)).downloads) W.dls[d.id] = d; } catch (e) { W.dlErr = e.message; }
  }
  async function poll() {
    clearTimeout(W.timer);
    if (W.n !== 4 || root.hidden) return;
    const before = shape();
    try { W.dls = Object.fromEntries((await api('/api/downloads')).downloads.map(d => [d.id, d])); } catch { /* next poll */ }
    let b = block();
    if (b.files && !W.eng.installed && !W.unpack) { // engine zips are on disk: unpack them once
      W.unpack = true; paint();
      try { W.eng = { ...W.eng, ...(await api('/api/engine/install', {})) }; } catch (e) { W.dlErr = e.message; }
      W.unpack = false; b = block();
    }
    if (shape() !== before) draw(b.all, true); else paint(); // done: focus to the title, which now says so
    if (!b.all && !W.dlErr) W.timer = setTimeout(poll, 1000);
  }
  async function pauseAll(b) {
    const act = b.paused ? 'resume' : 'pause', want = b.paused ? ['paused'] : ['queued', 'downloading'];
    const ids = W.rows.flatMap(r => r.files).map(f => W.dls[f.id]).filter(d => d && want.includes(d.status)).map(d => d.id);
    await Promise.all(ids.map(id => api(`/api/downloads/${encodeURIComponent(id)}/${act}`, {})));
  }

  // ---------- step 5: Test picture ----------
  // txt2img, the picked model, 512 x 512, seed 1; 8 steps with Turbo when the add-on is on disk, else 20.
  function testRequest(m) {
    const req = { kind: 'txt2img', model_id: m.id, prompt: PROMPT, width: 512, height: 512, seed: 1 };
    if (m.steps <= 8) return req; // a Fast model is already 8 steps
    const lora = W.cat.files.find(f => /turbo/i.test(f.id) && f.kind !== 'diffusion');
    return Object.assign(req, lora && W.have.has(lora.id) ? { turbo: true, steps: 8, cfg: 1 } : { steps: 20 });
  }
  async function startTest() {
    const m = byId(W.pick) || app.model();
    W.test = { state: 'running', step: 0, total: 0, t0: 0 };
    draw(true);
    try {
      await loadInst(); // the Turbo add-on may have arrived since step 1
      const seq = (await api('/api/events?since=0')).last_seq;
      Object.assign(W.test, { seq, id: (await api('/api/jobs', testRequest(m))).job_id });
    } catch (e) { Object.assign(W.test, { state: 'failed', msg: e.message }); draw(true); return; }
    testPoll();
  }
  async function testPoll() {
    clearTimeout(W.timer);
    const t = W.test; if (W.n !== 5 || root.hidden || t.state !== 'running') return;
    try {
      const r = await api('/api/events?since=' + t.seq);
      if (r.last_seq < t.seq) Object.assign(t, { state: 'failed', msg: 'Latentroom was restarted.' });
      for (const ev of r.events.filter(e => e.job_id === t.id)) {
        if (ev.type === 'preparing') t.t0 = ev.time;
        if (ev.type === 'step') Object.assign(t, { step: ev.step, total: ev.total });
        if (END.includes(ev.type)) Object.assign(t, { state: ev.type === 'done' && ev.entries.length ? 'done' : 'failed', entry: ev.entries[0], secs: Math.max(1, Math.round(ev.time - (t.t0 || ev.time))), msg: ev.message });
      }
      t.seq = r.last_seq;
    } catch { /* next poll */ }
    if (t.state !== 'running') return draw(true, true); // done or failed: the title says so
    const p = t.total ? t.step / t.total : 0;
    $('#w-bar').setAttribute('aria-valuenow', Math.round(100 * p)); $('#w-bar i').style.width = Math.round(100 * p) + '%';
    $('#w-line').innerHTML = t.total ? `Making the test picture${SEP}step ${t.step} of ${t.total}` : 'Getting the engine ready…';
    W.timer = setTimeout(testPoll, 500);
  }
  function stepTest() {
    const t = W.test || { state: 'running' }, m = byId(W.pick);
    const sub = 'One small picture to check that everything works. It is saved in Library like any other.';
    if (t.state === 'failed') {
      return frame('Test picture', sub, `<div class="wtest"><div class="wpic empty">${icon('alert')}</div><div class="wtinfo">${errLine(`The test picture failed. ${t.msg || ''}`, ` Copy the diagnostic report, then open an issue at ${extLink(`${REPO}/issues`)} and paste it there.`)}
        <div class="acts"><button class="btn" type="button" data-w-test>${icon('redo', 'sm')}Try again</button><button class="btn ghost" type="button" data-w-diag>${icon('copy', 'sm')}Copy diagnostic report</button></div></div></div>`,
        '<button class="btn ghost" type="button" data-w-finish>Skip the test</button>');
    }
    const done = t.state === 'done';
    // shortcut: no live preview while it runs (the engine sends none); the frame stays empty until the picture is done.
    const pic = done ? `<img src="/api/history/${encodeURIComponent(t.entry)}/image" alt="The test picture: ${PROMPT}">` : '';
    const prog = done ? `<div class="verdict"><span class="sdot ok"></span><span class="t-body-strong">It works. This picture took ${t.secs} s.</span></div>`
      : `<div class="dl"><div class="bar" id="w-bar" role="progressbar" aria-label="Test picture" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><i style="width:0%"></i></div><span class="line" id="w-line">Getting the engine ready…</span></div>`;
    return frame(done ? 'Latentroom is ready' : 'Test picture', sub,
      `<div class="wtest"><div class="wpic">${pic}</div><div class="wtinfo"><span class="label">Prompt</span><p class="mono-line">${PROMPT}</p><span class="label">Size</span><p class="mono-line">512 × 512${SEP}${esc(m ? `${m.pair.label} model` : '')}</p>${prog}</div></div>`,
      `<button class="btn primary md" type="button" data-w-finish${done ? '' : ' disabled'}>Start using Latentroom</button>`);
  }
  async function finish(skipped, note = skipped ? 'Setup is done. If pictures fail, copy the diagnostic report from Settings.' : 'Setup is done. Write a prompt and press Develop.') {
    await api('/api/settings', { first_run_done: true });
    await app.refresh();
    if (location.hash === '#develop') window.dispatchEvent(new HashChangeEvent('hashchange')); else location.hash = '#develop';
    toast(note);
  }

  // ---------- draw + actions ----------
  const STEP_FN = [stepPC, stepData, stepModel, stepDownload, stepTest];
  function draw(focusTitle, still) { // still: from a poll, so the title gets focus without scrolling
    root.innerHTML = STEP_FN[W.n - 1]();
    if (focusTitle) $('#wiz-title').focus({ preventScroll: !!still });
  }
  function go(n) {
    W.n = n; W.dataErr = ''; clearTimeout(W.timer);
    draw(true); root.scrollTop = 0;
    if (n === 4) poll();
    if (n === 5) { if (!W.test || W.test.state === 'failed') startTest(); else testPoll(); }
  }

  async function forward() {
    if (W.n === 3) { await api('/api/settings', { model_id: W.pick }); await app.refresh(); await startSetup(); }
    if (W.n < STEP_FN.length) go(W.n + 1);
  }
  async function changeFolder() {
    const f = await pickFolder(W.inst.data_dir); if (!f) return;
    // saved at once (not on Continue), so the free space shown is the new drive's
    try { await api('/api/settings', { data_dir: f }); await app.refresh(); await loadInst(); W.dataErr = ''; } catch (e) { W.dataErr = e.message; }
    draw(); $('[data-w-folder]')?.focus();
  }
  root.addEventListener('click', async e => {
    const b = e.target.closest('button'); if (!b || b.disabled) return;
    const d = b.dataset;
    try {
      if (d.wNext != null) await forward();
      else if (d.wBack != null) go(W.n - 1);
      else if (d.wFolder != null) await changeFolder();
      else if (d.wAdult != null) { await api('/api/settings', { adult_ok: true }); await app.refresh(); draw(true); toast('Uncensored models are shown.'); }
      else if (d.wSide) { flip(d.wSide); draw(); $(`[data-w-side="${W.side}"]`).focus(); }
      else if (d.wDet != null) { W.det = !W.det; draw(); $('[data-w-det]').focus(); }
      else if (d.wMore != null) { W.more = !W.more; draw(); $('[data-w-more]').focus(); }
      else if (d.open) await api('/api/open', { what: d.open });
      else if (d.wPause != null) { await pauseAll(block()); await poll(); $('[data-w-pause]')?.focus(); }
      else if (d.wTest != null) await startTest();
      else if (d.wDiag != null) await copyDiagnostics();
      else if (d.wFinish != null) await finish(W.test?.state !== 'done');
      else if (d.wSkip != null) await finish(true, 'Setup was skipped: no model fits this PC.');
      else if (d.wRetry != null) { await startSetup(); await poll(); $('[data-w-pause]')?.focus(); }
    } catch (err) { toast(err.message); }
  });
  root.addEventListener('keydown', e => { // Left/Right flip the Standard | Uncensored switch
    if (!e.target.closest('.wside') || !['ArrowLeft', 'ArrowRight'].includes(e.key)) return;
    e.preventDefault(); root.querySelector('[data-w-side][aria-checked="false"]')?.click();
  });
  root.addEventListener('change', e => {
    if (e.target.name !== 'wpick') return;
    W.pick = e.target.value;
    const m = byId(W.pick), total = bytes(missing(m));
    $('#w-total').textContent = total ? `Download: ${GB(total)}` : 'Everything for this model is already on this PC.';
    const next = root.querySelector('[data-w-next]');
    next.textContent = total ? 'Download' : 'Continue'; next.disabled = false;
  });

  return {
    hide: () => clearTimeout(W.timer),
    async show() {
      W.n = 1; W.test = null; W.get = null;
      try { await load(); } catch (e) { root.innerHTML = ''; toast(e.message); return; }
      // A restart mid-setup: the picked model's download is on its way (or paused, or stopped):
      // back to step 4, without a new POST that would resume a download paused on purpose.
      const m = byId(W.pick), get = m && W.pick === app.settings.model_id ? missing(m).map(f => f.id) : [];
      const dls = get.length ? (await api('/api/downloads').catch(() => ({ downloads: [] }))).downloads : [];
      if (dls.some(d => get.includes(d.id) && d.status !== 'cancelled')) { prepSetup(m); return go(4); }
      draw(true);
    },
  };
}
