// Develop screen: settings column, developing tray driven by the real
// /api/events step events, frame strip, Recent.
import { $, $$, SEP, esc, icon, api, toast, radio, arrowKeys, ago, modelName, fitLine, copyDiagnostics, saveLink, PHONE } from './ui.js';
import { initDevelop2, KINDS, ENTRY_TYPE } from './develop2.js';
import { initEnhance } from './prompt-enhance.js';
import { loader } from './loader-mark.js';

const SIZES = [['1:1', 1024, 1024, 'Square'], ['4:3', 1152, 864, 'Landscape'], ['3:2', 1248, 832, 'Photo'], ['16:9', 1344, 768, 'Wide (close to 16:9)'], ['9:16', 768, 1344, 'Tall']];
const END = new Set(['done', 'failed', 'cancelled']);
const IMG_TYPES = ['image/png', 'image/jpeg', 'image/webp'];
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
const nth = n => n + ((n % 100 > 10 && n % 100 < 14) ? 'th' : ['th', 'st', 'nd', 'rd'][n % 10] || 'th'); // 2nd, 3rd, 11th, 21st

export function initDevelop(app) {
  const ui = { w: 1024, h: 1024, frames: 1, turbo: false, refs: [], loras: [], fit: null, aspect: 1, loraFiles: [] };
  const jobs = new Map(); // job_id -> job this window submitted
  let view = null; // job (or Recent group) shown in the tray
  let seq = 0, pollTimer = 0, fitTimer = 0, d2 = null, enh = null; // d2 = Develop v2 hooks (develop2.js), enh = Enhance prompt sync
  // queue hand-off: a finished job stays in the tray 3 s; a click in that time keeps it there
  let hold = 0, holdTimer = 0, pinned = false, runJob = null;
  const keep = () => { if (Date.now() < hold) pinned = true; };
  const field = new window.DevelopField($('#field'), { aspect: 1 });
  const active = j => j && j.id && !END.has(j.state);
  const anyActive = () => [...jobs.values()].some(active);

  // ---------- settings column ----------
  $('#sizes').innerHTML = SIZES.map(([r, w, h, t]) => { const k = 14 / Math.max(w, h); return `<button type="button" role="radio" aria-checked="false" title="${t} · ${w} × ${h}" data-w="${w}" data-h="${h}"><span class="shape" style="width:${Math.round(w * k)}px;height:${Math.round(h * k)}px"></span>${r}</button>`; }).join('');
  ['#sizes', '#frames'].forEach(arrowKeys);
  function setSize(w, h) {
    ui.w = w; ui.h = h; $('#w').value = w; $('#h').value = h;
    $$('#sizes button').forEach(b => b.setAttribute('aria-checked', String(+b.dataset.w === w && +b.dataset.h === h)));
    if (!anyActive()) setAspect(w / h);
    summary(); checkFit();
  }
  $('#sizes').addEventListener('click', e => { const b = e.target.closest('button'); if (b) setSize(+b.dataset.w, +b.dataset.h); });
  ['#w', '#h'].forEach(id => $(id).addEventListener('change', () => {
    const snap = v => Math.min(2048, Math.max(256, Math.round((+v || 1024) / 32) * 32));
    setSize(snap($('#w').value), snap($('#h').value)); $('#size-help').hidden = false;
  }));
  function bindRange(id, fmt) {
    const r = $(id), v = $(id + '-v');
    const up = () => { r.style.setProperty('--p', ((r.value - r.min) / (r.max - r.min) * 100) + '%'); v.textContent = fmt(+r.value); };
    r.addEventListener('input', up); up(); return up;
  }
  const upSteps = bindRange('#steps', v => v);
  const upCfg = bindRange('#cfg', v => { $('#g-neg').hidden = ui.turbo || (v <= 1 && !$('#neg').value); negGuard(); return v.toFixed(1); });
  function negGuard() { const off = +$('#cfg').value <= 1; $('#neg').disabled = off; $('#neg-help').textContent = off ? 'Guidance 1 ignores the negative prompt.' : 'Things to keep out of the picture.'; }
  $('#neg-open').onclick = () => { $('#neg-box').hidden = false; $('#neg-open').hidden = true; $('#neg').focus(); };
  $('#neg-close').onclick = () => { $('#neg-box').hidden = true; $('#neg-open').hidden = false; $('#neg-open').focus(); };
  $('#frames').addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return; radio('#frames', b); ui.frames = +b.dataset.n; summary(); });
  function lock(on) {
    const b = $('#lock'); b.setAttribute('aria-pressed', String(on)); b.innerHTML = icon(on ? 'lock' : 'unlock');
    b.title = on ? 'Seed kept. Click to use a new random seed each time.' : 'Keep this seed';
  }
  $('#dice').onclick = () => { $('#seed').value = Math.floor(Math.random() * 4294967295); lock(true); };
  $('#lock').onclick = () => lock($('#lock').getAttribute('aria-pressed') !== 'true');
  $('#seed').addEventListener('input', () => { $('#seed').value = $('#seed').value.replace(/\D/g, '').slice(0, 10); lock(!!$('#seed').value); });
  $('#adv-btn').onclick = () => { const b = $('#adv-btn'), o = b.getAttribute('aria-expanded') !== 'true'; b.setAttribute('aria-expanded', String(o)); $('#adv').hidden = !o; };
  ['#sampler', '#scheduler'].forEach(id => $(id).addEventListener('change', () => {
    $('#adv-btn .sum').textContent = `${$('#sampler').value} · ${$('#scheduler').value || 'default'}`;
  }));

  // Turbo: a standard model + the Turbo add-on uses 8 steps and guidance 1.
  function setTurbo(on, locked = false, help = '') {
    const m = app.model(), s = $('#steps'), c = $('#cfg');
    ui.turbo = on && !locked;
    $('#turbo').setAttribute('aria-checked', String(on)); $('#turbo').disabled = locked || !m;
    s.value = on && !locked ? 8 : m ? m.steps : 40; c.value = on && !locked ? 1 : m ? m.cfg : 2.5;
    s.disabled = c.disabled = on; upSteps(); upCfg();
    $('#steps-help').textContent = on ? 'Turbo uses 8 steps.' : 'More steps: more detail, slower.';
    $('#cfg-help').textContent = on ? 'Turbo uses guidance 1.' : 'How strictly the picture follows the prompt.';
    $('#turbo-help').textContent = help || 'Faster, a bit less detail.';
    $('#g-neg').hidden = on || +c.value <= 1;
    checkFit(); // the Turbo add-on counts in the fit
  }
  $('#turbo').onclick = () => setTurbo(!ui.turbo);
  function onModel() { // model picked or changed: its defaults, Turbo rules, fit
    const m = app.model();
    if (m && m.family.startsWith('turbo')) setTurbo(true, true, 'This model is already a Turbo model.');
    else setTurbo(false);
    checkFit();
  }
  window.addEventListener('lr-model', onModel);

  // ---------- LoRA picker ----------
  async function loadLoras() {
    try { ui.loraFiles = (await api('/api/loras')).loras; } catch (e) { toast(e.message); }
    renderLoraList();
  }
  function renderLoraList() {
    const q = $('#lora-find').value.trim().toLowerCase(), names = ui.loraFiles.filter(n => n.toLowerCase().includes(q));
    $('#lora-list').innerHTML = ui.loraFiles.length
      ? names.map(n => `<label class="row"><input type="checkbox" value="${esc(n)}" ${ui.loras.some(l => l.name === n) ? 'checked' : ''}><span title="${esc(n)}">${esc(n)}</span></label>`).join('')
      : `<div class="empty" style="padding:16px 8px">${icon('layers', 'lg')}<span class="t-body-strong">No LoRA files yet</span><p class="help">Put .safetensors files in the LoRA folder, then press Refresh.</p><button class="btn sm" type="button" data-open-loras>${icon('folder', 'sm')}Open LoRA folder</button></div>`;
  }
  $('#lora-find').addEventListener('input', renderLoraList);
  $('#lora-refresh').onclick = loadLoras;
  $('#lora-add').onclick = () => { const p = $('#lora-pop'); p.hidden = !p.hidden; if (!p.hidden) { loadLoras(); $('#lora-find').focus(); } };
  $('#lora-list').addEventListener('change', e => {
    const n = e.target.value;
    ui.loras = e.target.checked ? [...ui.loras, { name: n, w: 1 }] : ui.loras.filter(l => l.name !== n);
    renderLoras();
  });
  $('#lora-done').onclick = () => { $('#lora-pop').hidden = true; $('#lora-add').focus(); };
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('#lora-pop').hidden) { $('#lora-pop').hidden = true; $('#lora-add').focus(); } });
  document.addEventListener('mousedown', e => { if (!$('#lora-pop').hidden && !e.target.closest('.pop-anchor')) $('#lora-pop').hidden = true; });
  function renderLoras() {
    $('#loras').innerHTML = ui.loras.map((l, i) => `<div class="lora-row"><span class="name" title="${esc(l.name)}">${esc(l.name)}</span><input class="range" type="range" min="0" max="2" step="0.05" value="${l.w}" data-i="${i}" aria-label="${esc(l.name)} weight" style="--p:${l.w / 2 * 100}%"><span class="val">${l.w.toFixed(2)}</span><button class="btn ghost icon sm" type="button" data-rm="${i}" aria-label="Remove ${esc(l.name)}" title="Remove">${icon('x', 'sm')}</button></div>`).join('');
    $('#loras').style.marginBottom = ui.loras.length ? '0' : '-8px';
    checkFit(); // the LoRA files count in the fit
  }
  $('#loras').addEventListener('input', e => { const r = e.target; if (r.dataset.i == null) return; const l = ui.loras[+r.dataset.i]; l.w = +r.value; r.style.setProperty('--p', l.w / 2 * 100 + '%'); r.nextElementSibling.textContent = l.w.toFixed(2); });
  $('#loras').addEventListener('click', e => { const b = e.target.closest('[data-rm]'); if (b) { ui.loras.splice(+b.dataset.rm, 1); renderLoras(); } });
  document.addEventListener('click', async e => {
    if (!e.target.closest('[data-open-loras]')) return;
    try { await api('/api/open', { what: 'loras' }); } catch (err) { toast(err.message); }
  });

  // ---------- references (makes the job an Edit) ----------
  function renderRefs() {
    const n = ui.refs.length;
    $('#drop').hidden = n > 0; $('#refs').hidden = n === 0;
    $('#refs').innerHTML = ui.refs.map((r, i) => `<div class="ref" title="${esc(r.name)}"><img src="${esc(r.url)}" alt=""><button type="button" class="tag" data-tag="${i}" title="Insert &lt;image${i + 1}&gt; in the instruction" aria-label="Insert &lt;image${i + 1}&gt; in the instruction">${i + 1}</button><button type="button" class="x" data-x="${i}" aria-label="Remove picture ${i + 1}">${icon('x', 'sm')}</button></div>`).join('')
      + (n ? `<button type="button" class="ref add" aria-label="Add a picture" title="Add a picture">${icon('plus')}</button>` : '');
    $('#refs-help').textContent = n >= 2 ? 'Point at a picture with <image1>, <image2>… in the instruction.' /* SPEC 5fe8905: the names Qwen-Image 2.1 reads */ : 'Optional. Add pictures to edit or combine them.';
    v1Prompt(); checkFit();
  }
  function v1Prompt() {
    const n = ui.refs.length;
    $('#prompt-label').textContent = n ? 'Instruction' : 'Prompt';
    $('#prompt').placeholder = n ? 'Say what to change, e.g. make the sky stormy' : "Describe the picture, e.g. a fisherman's hut on a frozen lake at first light";
    enh?.();
  }
  async function upload(files) {
    for (const f of files) {
      if (!IMG_TYPES.includes(f.type)) { toast(`${f.name} is not a PNG, JPG or WebP picture.`); continue; }
      try {
        const r = await api('/api/uploads', f);
        ui.refs.push({ path: r.path, name: f.name, url: '/api/uploads/' + r.name });
      } catch (e) { toast(e.message); }
    }
    renderRefs();
  }
  $('#drop').onclick = () => $('#ref-file').click();
  $('#ref-file').addEventListener('change', e => { upload([...e.target.files]); e.target.value = ''; });
  $('#refs').addEventListener('click', e => {
    if (e.target.closest('.add')) return $('#ref-file').click();
    const x = e.target.closest('[data-x]'); if (x) { ui.refs.splice(+x.dataset.x, 1); return renderRefs(); }
    const t = e.target.closest('[data-tag]');
    if (t) { const p = $('#prompt'), at = p.selectionStart ?? p.value.length; p.value = p.value.slice(0, at) + `<image${+t.dataset.tag + 1}> ` + p.value.slice(at); p.focus(); }
  });
  const dz = $('#g-refs');
  dz.addEventListener('dragover', e => { e.preventDefault(); $('#drop').classList.add('over'); });
  dz.addEventListener('dragleave', () => $('#drop').classList.remove('over'));
  dz.addEventListener('drop', e => { e.preventDefault(); $('#drop').classList.remove('over'); upload([...e.dataTransfer.files]); });

  // ---------- footer: fit check, Develop button, summary ----------
  function checkFit() { clearTimeout(fitTimer); fitTimer = setTimeout(fit, 300); }
  async function fit() {
    const m = app.model();
    if (!m || !m.installed) return setFit('warn', 'No picture model yet', 'Get one in the Models tab.', null);
    try {
      const f = await api(`/api/fit?model_id=${encodeURIComponent(m.id)}&${fitQuery()}`);
      setFit(...fitLine(f), f, f.reason);
    } catch (e) { setFit('warn', 'Could not check the size', e.message, null); clearTimeout(fitTimer); fitTimer = setTimeout(fit, 3000); } // tried again, or Develop stays off
  }
  function setFit(c, a, b, f, why = '') {
    ui.fit = f && f.label !== 'too_big' ? f : null;
    $('#fit').innerHTML = `<span class="sdot ${c}"></span><span>${esc(a)}</span><span class="c3">${esc(b)}</span>`;
    $('#fit').title = why;
    $('#chip-dot').className = 'sdot ' + c;
    $('#go').disabled = !ui.fit;
  }
  function summary() { enh?.(); if (d2?.summary()) return; $('#summary').innerHTML = `${plural(ui.frames, 'frame')}${SEP}${ui.w} × ${ui.h}${SEP}runs locally`; }

  function request() {
    const cfg = +$('#cfg').value, m = app.model();
    return {
      kind: ui.refs.length ? 'edit' : 'txt2img', model_id: m.id, prompt: $('#prompt').value.trim(),
      negative: cfg > 1 && !ui.turbo ? $('#neg').value.trim() : '', width: ui.w, height: ui.h,
      seed: $('#lock').getAttribute('aria-pressed') === 'true' && $('#seed').value ? +$('#seed').value : -1,
      steps: +$('#steps').value, cfg, sampler: $('#sampler').value, scheduler: $('#scheduler').value,
      batch: ui.frames, turbo: ui.turbo, loras: ui.loras.map(l => ({ name: l.name, weight: l.w })), refs: ui.refs.map(r => r.path),
    };
  }
  async function develop() {
    const v2 = d2.check(); // true: a From a picture job, false: stopped with a message, null: v1 rules
    if (v2 === false) return;
    if (v2 === null && !$('#prompt').value.trim()) { $('#prompt-err').hidden = false; $('#prompt').focus(); return; }
    if ($('#go').disabled) return;
    const req = request(), info = { frames: req.batch, w: req.width, h: req.height, model: app.model() };
    if (v2 && !(await d2.request(req, info))) return;
    submit(req, info);
  }
  async function submit(req, info) {
    const wasBusy = anyActive();
    try {
      const { job_id } = await api('/api/jobs', req);
      const j = { id: job_id, req, ...info, state: 'queued', ahead: 0 };
      jobs.set(job_id, j); window.dispatchEvent(new Event('lr-busy')); // the VRAM meter reads live
      if (wasBusy) toast('Added to the queue. It starts when this one is done.');
      if (!active(view)) show(j); else drawRecent(); // a job added behind another shows in Recent at once
      busy(); poll();
      return true;
    } catch (e) { toast(e.message); return false; }
  }
  // history Entry -> upscale x4 job / Develop reference (tray buttons and Library)
  const upscale = e => submit({ kind: 'upscale', init_image: e.image }, { frames: 1, w: e.width, h: e.height, model: app.models[e.model_id] });
  function addRef(e) { ui.refs.push({ path: e.image, name: '#' + e.id, url: `/api/history/${e.id}/thumb` }); renderRefs(); return ui.refs.length; }

  // Library "Re-develop": put a stored JobRequest back into the settings column, seed locked.
  async function fill(r, seed) {
    if (r.model_id && r.model_id !== app.settings.model_id && app.models[r.model_id]) {
      try { await api('/api/settings', { model_id: r.model_id }); await app.refresh(); } catch (e) { toast(e.message); }
    }
    $('#prompt').value = r.prompt; $('#prompt-err').hidden = true;
    $('#neg').value = r.negative; $('#neg-box').hidden = !r.negative; $('#neg-open').hidden = !!r.negative;
    const m = app.model();
    if (!m?.family.startsWith('turbo')) setTurbo(r.turbo);
    if (!ui.turbo && m) { $('#steps').value = r.steps ?? m.steps; $('#cfg').value = r.cfg ?? m.cfg; upSteps(); upCfg(); }
    setSize(r.width, r.height);
    $('#seed').value = seed; lock(true);
    const fb = $(`#frames [data-n="${r.batch}"]`) || $('#frames [data-n="1"]'); radio('#frames', fb); ui.frames = +fb.dataset.n;
    $('#sampler').value = r.sampler; $('#scheduler').value = r.scheduler; $('#sampler').dispatchEvent(new Event('change'));
    ui.loras = r.loras.map(l => ({ name: l.name, w: l.weight })); renderLoras();
    // shortcut: a reference from the output folder has no thumbnail URL here, it shows its file name only.
    // phone: References are hidden, so none come back that the phone could not see or remove
    ui.refs = PHONE.matches ? [] : r.refs.map(p => { const name = p.split(/[\\/]/).pop(); return { path: p, name, url: /^[0-9a-f]{32}\.(png|jpg|webp)$/.test(name) ? '/api/uploads/' + name : '' }; });
    renderRefs(); summary();
    await d2.fill(r);
  }
  $('#prompt').addEventListener('input', () => { $('#prompt-err').hidden = true; });
  $('#go').onclick = develop;
  document.addEventListener('keydown', e => {
    if (e.ctrlKey && e.key === 'Enter' && !$('#screen-develop').hidden) { e.preventDefault(); develop(); }
  });

  // ---------- tray ----------
  function setAspect(a) { ui.aspect = a; field.setAspect(a); layoutTray(); }
  function layoutTray() { // tray = largest W/H box that fits the stage; strip, warn line and meta take the tray width
    const st = $('.stage'); if (!st.clientWidth) return;
    const cs = getComputedStyle(st), a = ui.aspect, vis = el => !el.hidden;
    $('#size-tag').textContent = view ? `${view.w} × ${view.h}` : `${ui.w} × ${ui.h}`;
    const W = st.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
    let base = st.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom) - 44 - 24;
    if (vis($('#warnline'))) base -= $('#warnline').offsetHeight + 12;
    base -= d2?.extraH() || 0; // mask toolbar (Repaint a part)
    const stripOn = vis($('#strip')), lab = ($('#strip .cell > span') || {}).offsetHeight || 16;
    let w = W, h = W / a;
    // phone: the page scrolls, so the tray is the full width, at most layout.phone-tray-max-h tall
    const phoneMax = PHONE.matches && parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--layout-phone-tray-max-h'));
    for (let i = 0; i < 4; i++) { // strip height depends on its width, which is the tray width
      const sh = stripOn ? (w - 3 * 12) / 4 / a + 5 + lab + 12 : 0, H = phoneMax || base - sh;
      if (W / a > H) { h = Math.max(120, H); w = h * a; } else { w = W; h = W / a; }
    }
    $('#tray-wrap').style.height = Math.floor(h) + 'px';
    $('#tray').style.width = Math.floor(w) + 'px'; $('#tray').style.height = Math.floor(h) + 'px';
    ['#strip', '#warnline', '#meta'].forEach(id => ($(id).style.width = Math.floor(w) + 'px'));
  }
  new ResizeObserver(layoutTray).observe($('.stage'));

  const header = (left, right = '') => {
    $('#th-left').innerHTML = left; $('#th-right').innerHTML = right;
    // Screen readers hear the state once per change (Preparing, Developing frame 2 of 4, Done),
    // not every step or 0.5 s poll: only the title goes to the live span
    const say = $('#th-left .t-title')?.textContent || '';
    if ($('#th-say').textContent !== say) $('#th-say').textContent = say;
  };
  const cancelBtn = () => `<button class="btn sm" type="button" data-cancel>${icon('x', 'sm')}Cancel</button>`;
  function counter(mode, k, t) {
    const c = $('#count'); c.hidden = !mode; c.classList.toggle('prep', mode === 'prep' || mode === 'up');
    if (mode === 'prep') { $('#count-n').textContent = 'Preparing…'; $('#count-l').textContent = 'loading the model'; }
    if (mode === 'dev') { $('#count-n').innerHTML = `${k}<small> / ${t}</small>`; $('#count-l').textContent = 'Developing'; }
    if (mode === 'up') { $('#count-n').textContent = 'Upscaling…'; $('#count-l').textContent = '×4'; }
  }
  const pline = (on, frac = 0) => { $('#pline').hidden = !on; $('#pline-i').style.transform = `scaleX(${frac})`; };
  const overlay = html => { const o = $('#overlay'); o.hidden = !html; o.innerHTML = html || ''; };
  function strip(j, cur) { // while running: still dots per frame; done: the real pictures
    const s = $('#strip'); s.hidden = j.frames < 2 && !(j.entries?.length > 1);
    if (s.hidden) { s.innerHTML = ''; return layoutTray(); }
    if (j.entries) {
      s.innerHTML = j.entries.map((e, i) => `<button class="cell" type="button" data-e="${i}"><div class="th ${i === j.sel ? 'sel' : ''}" style="aspect-ratio:${j.w} / ${j.h}"><img src="/api/history/${e.id}/thumb" alt=""></div><span>${String(i + 1).padStart(2, '0')} · #${String(e.id).padStart(4, '0')}</span></button>`).join('');
      return layoutTray();
    }
    s.innerHTML = Array.from({ length: j.frames }, (_, i) => {
      const n = i + 1, id = String(n).padStart(2, '0'), ar = `aspect-ratio:${j.w} / ${j.h}`;
      if (n < cur) return `<div class="cell"><div class="th" style="${ar}"><canvas data-still="${n}"></canvas></div><span>${id} · done</span></div>`;
      if (n === cur) return `<div class="cell"><div class="th cur" style="${ar}"><canvas data-still="${n}"></canvas></div><span class="cur">${loader()}${id} · <b id="cur-pct" style="font-weight:400">0%</b></span></div>`;
      return `<div class="cell"><div class="th q" style="${ar}"><canvas data-still="${n + 20}" style="opacity:.6"></canvas></div><span>${id} · queued</span></div>`;
    }).join('');
    layoutTray();
    requestAnimationFrame(() => $$('#strip [data-still]').forEach(c => window.DevelopField.still(c, +c.dataset.still * 7, j.w / j.h)));
  }
  $('#strip').addEventListener('click', e => {
    const c = e.target.closest('.cell[data-e]'); if (!c || !view?.entries) return;
    keep();
    view.sel = +c.dataset.e; $$('#strip .th').forEach((t, i) => t.classList.toggle('sel', i === view.sel));
    finishWith(view.entries[view.sel]); meta(view);
    const sv = $('#th-right [data-save]'); if (sv) sv.outerHTML = saveLink(view.entries[view.sel]); // Save to phone follows the frame
  });
  async function finishWith(entry) {
    const img = new Image(); img.src = `/api/history/${entry.id}/image`;
    try { await img.decode(); field.finish(img); } catch { field.reset(); toast('This picture is no longer in its folder.'); }
  }
  function meta(j) {
    const e = j.entries?.[j.sel], r = e?.request || j.req || {}, m = app.models[e?.model_id] || j.model;
    $('#meta').innerHTML = e ? [`seed ${e.seed}`, r.steps && `${r.steps} steps`, r.cfg && `guidance ${(+r.cfg).toFixed(1)}`, KINDS[r.kind] && `strength ${(+r.strength).toFixed(2)}`, m && `${modelName(m)} ${m.quant}`].filter(Boolean).map(esc).join(SEP) : '';
  }
  function duration(j) {
    const s = Math.max(1, Math.round((j.end || 0) - (j.t0 || j.end || 0)));
    return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
  }

  // Draw the tray for job (or Recent group) j in its current state.
  function render(j) {
    $('#tray').classList.remove('done'); $('#field').style.opacity = ''; overlay(''); counter(null); pline(false); d2?.reset();
    $('#warnline').hidden = true; $('#meta').innerHTML = ''; $('#go-t').textContent = anyActive() ? 'Add to queue' : 'Develop';
    if (!j) {
      field.reset(); strip({ frames: 1 }, 0);
      if (d2?.idle()) return; // From a picture: the starting picture or its drop hint
      setAspect(ui.w / ui.h);
      header('<span class="t-title c2">Ready</span>');
      return overlay(`<div class="empty">${icon('image', 'lg')}<span class="t-body-strong">Your picture develops here</span><p class="help">Write a prompt, then press Develop (Ctrl + Enter).</p></div>`);
    }
    if (ui.aspect !== j.w / j.h) setAspect(j.w / j.h);
    const kd = j.req?.kind, up = kd === 'upscale', K = KINDS[kd];
    const solo = kd === 'enhance' ? 'Adding detail' : kd === 'facefix' ? 'Fixing faces' : '';
    if (j.state === 'queued') {
      field.reset(); $('#field').style.opacity = '.6'; strip(j, 0);
      header(`<span class="sdot hollow"></span><span class="t-title">Waiting</span><span class="t-mono-sm">${j.ahead ? plural(j.ahead, 'job') + ' ahead' : 'starting'}</span>`, cancelBtn());
    } else if (j.state === 'preparing' || (up && j.state === 'step' && !j.total)) {
      field.prepare(); counter(up ? 'up' : 'prep'); pline(true, 0); strip(j, 1);
      header(up ? `${loader()}<span class="t-title">Upscaling ×4…</span><span class="t-mono-sm">${j.w * 4} × ${j.h * 4}</span>`
        : `${loader()}<span class="t-title">${solo ? solo + '…' : 'Preparing…'}</span><span class="t-mono-sm">${kd === 'enhance' ? 'upscaling, then adding detail' : 'loading the model'}</span>`, cancelBtn());
    } else if (j.state === 'step') {
      const n = Math.max(1, Math.round(j.total / j.frames)), f = Math.min(j.frames, Math.ceil(j.step / n)), k = j.step - (f - 1) * n;
      if (f !== j.frame) { j.frame = f; field.reset(); strip(j, f); }
      field.step(k, n); counter('dev', k, n); pline(true, k / n);
      const pc = $('#cur-pct'); if (pc) pc.textContent = Math.round(k / n * 100) + '%';
      header(up ? `${loader()}<span class="t-title">Upscaling ×4…</span><span class="t-mono-sm">${j.w * 4} × ${j.h * 4}</span>`
        : `${loader()}<span class="t-title">${solo || `Developing frame ${f} of ${j.frames}`}</span><span class="t-mono-sm">step ${k} / ${n}</span>`, cancelBtn());
    } else if (j.state === 'done') {
      if (!j.entries) return; // pictures still loading (see onEvent)
      j.sel ??= 0;
      $('#tray').classList.add('done'); finishWith(j.entries[j.sel]); strip(j, 0); meta(j);
      const short = (j.loras || []).filter(l => l.applied < l.total || !l.total)[0];
      if (short) {
        $('#warnline').innerHTML = `${icon('warn', 'sm')}<span>${esc(short.total ? `${short.name}: only ${short.applied} of ${short.total} parts were applied. It may not match this model.` : `${short.name} was not found in the LoRA folder.`)}</span>`;
        $('#warnline').hidden = false; layoutTray();
      } else if (kd === 'edit' && j.req.refs?.length > 1) { // some seeds ignore a reference (SPEC 5fe8905)
        $('#warnline').innerHTML = '<p class="help">Not what you wanted? Try again: each run uses a new seed.</p>';
        $('#warnline').hidden = false; layoutTray();
      }
      const what = kd === 'enhance' ? [K.name, `${j.w} × ${j.h}`] : kd === 'facefix' ? [K.name] : [K && (K.short || K.name), plural(j.entries.length, 'frame')];
      header(`${icon('check')}<span class="t-title">Done</span><span class="t-mono-sm">${[...what, j.end && duration(j)].filter(Boolean).join(' · ')}</span>`,
        d2.doneRight(j) + `<button class="btn sm desk-only" type="button" data-act="folder">${icon('folder', 'sm')}Show in folder</button>` + saveLink(j.entries[j.sel]));
    } else if (j.state === 'failed') {
      field.reset(); $('#field').style.opacity = '.4'; strip({ frames: 1 }, 0);
      header(`<svg class="i err"><use href="#i-alert"/></svg><span class="t-title">Stopped</span>`);
      overlay(`<div class="err-panel" role="alert">${icon('alert', 'lg')}<span class="t-body-strong">This picture could not be developed</span><p>${esc(j.message || 'Something went wrong. Try again.')}</p><div class="acts"><button class="btn" type="button" data-again>${icon('redo', 'sm')}Try again</button><button class="btn ghost desk-only" type="button" data-copy>${icon('copy', 'sm')}Copy diagnostic report</button></div></div>`);
    } else if (j.state === 'cancelled') {
      field.reset(); strip({ frames: 1 }, 0);
      header('<span class="t-title c2">Cancelled</span><span class="t-mono-sm">nothing was saved</span>');
    }
  }
  function show(j) { view = j; pinned = false; if (j) j.frame = 0; render(j); drawRecent(); }
  function handOff(j) { // after j's hold: follow the running job, or the newest one if it finished meanwhile
    if (pinned || view !== j) return; // a click kept j, or the tray already shows something else
    const all = [...jobs.values()], n = all.find(active) || all[all.length - 1];
    if (n !== j) show(n);
  }

  $('#th-right').addEventListener('click', async e => {
    const b = e.target.closest('button'); if (!b || !view) return;
    keep();
    if (b.matches('[data-cancel]')) return cancel(view);
    const e0 = view.entries?.[view.sel ?? 0]; if (!e0) return; // Compare and Touch up: develop2.js
    if (b.dataset.act === 'folder') { try { await api('/api/open', { what: 'file', entry_id: e0.id }); } catch (err) { toast(err.message); } }
  });
  async function cancel(j) { // the tray's Cancel, and a queued row's in Recent
    try { if ((await api(`/api/jobs/${j.id}/cancel`, {})).cancelled) { j.state = 'cancelled'; if (j === view) render(j); drawRecent(); busy(); } } catch (err) { toast(err.message); }
  }
  $('#overlay').addEventListener('click', e => {
    if (e.target.closest('[data-copy]')) copyDiagnostics();
    if (e.target.closest('[data-again]') && view?.req) submit(view.req, { frames: view.frames, w: view.w, h: view.h, model: view.model });
  });

  // ---------- events: GET /api/events every 0.5 s while a job is queued or running, else 3 s ----------
  async function onEvent(ev) {
    const j = jobs.get(ev.job_id); if (!j || END.has(j.state)) return;
    j.state = ev.type;
    if (ev.type === 'preparing') j.t0 = ev.time;
    if (ev.type === 'step') { j.step = ev.step; j.total = ev.total; }
    if (END.has(ev.type)) { j.end = ev.time; j.message = ev.message; j.loras = ev.loras; window.dispatchEvent(new Event('lr-job-end')); }
    if (ev.type === 'done') {
      try { j.entries = await Promise.all(ev.entries.map(id => api(`/api/history/${id}`))); } catch { j.entries = []; }
      if (!j.entries.length) { j.state = 'failed'; j.message = 'The picture was not saved. Try again.'; }
    }
    if (END.has(ev.type)) recent();
    if (view !== j && (active(view) || pinned || Date.now() < hold)) return drawRecent(); // j lives in the Recent top row
    view = j;
    if (END.has(ev.type)) { hold = Date.now() + 3000; clearTimeout(holdTimer); holdTimer = setTimeout(handOff, 3000, j); }
    render(j); drawRecent();
  }
  async function poll() {
    clearTimeout(pollTimer);
    try {
      const r = await api('/api/events?since=' + seq);
      if (r.last_seq < seq) { // Latentroom restarted: the jobs it had are gone
        for (const j of jobs.values()) if (active(j)) Object.assign(j, { state: 'failed', message: 'Latentroom was restarted. Press Try again.' });
        seq = 0; if (view) render(view);
      } else {
        for (const ev of r.events) await onEvent(ev);
        seq = r.last_seq;
      }
      const q = [...jobs.values()].filter(j => j.state === 'queued');
      if (q.length) {
        const list = (await api('/api/jobs')).jobs, ids = list.map(x => x.id), was = q.map(j => j.pos).join();
        q.forEach(j => { const i = ids.indexOf(j.id); j.ahead = Math.max(0, i); j.pos = list[i]?.position; }); // pos 1 = runs next
        if (q.includes(view)) render(view);
        if (q.map(j => j.pos).join() !== was) drawRecent();
      }
    } catch { /* server busy or restarting: keep polling */ }
    busy();
    pollTimer = setTimeout(poll, anyActive() ? 500 : 3000);
  }
  function busy() { // Develop tab dot + button label while anything of ours runs
    const on = anyActive();
    $('#tab-dot').hidden = !on || document.body.dataset.screen === 'develop';
    $('#go-t').textContent = on ? 'Add to queue' : 'Develop';
    enh?.();
  }
  window.addEventListener('hashchange', busy);

  // ---------- Recent: history entries grouped back into jobs ----------
  let groups = [];
  const gone = new Set(); // history ids whose picture file is missing
  async function recent() {
    let entries = [];
    try { entries = (await api('/api/history?page=0&per_page=60')).entries; } catch { return; }
    groups = [];
    for (const e of entries) { // a batch is saved as one entry per picture with seed, seed+1, …: entries come newest first
      const g = groups[groups.length - 1], p = g?.entries[g.entries.length - 1];
      if (p && p.prompt === e.prompt && p.kind === e.kind && p.model_id === e.model_id && p.width === e.width && p.height === e.height && p.seed === e.seed + 1) g.entries.push(e);
      else groups.push({ entries: [e] });
    }
    groups = groups.slice(0, 12).map(g => {
      g.entries.reverse(); // newest first from the API; frames read 01, 02, ...
      const e = g.entries[0];
      return { ...g, state: 'done', req: e.request, frames: g.entries.length, w: e.width, h: e.height, model: app.models[e.model_id], loras: [] };
    });
    drawRecent();
  }
  function drawRecent() {
    const run = runJob = active(view) ? view : [...jobs.values()].find(active) || null;
    const rows = groups.map((g, i) => {
      const e = g.entries[0], t = e.prompt || 'Upscaled picture', m = `${plural(g.frames, 'frame')} · ${ago(e.created)}`, miss = gone.has(String(e.id));
      const th = miss ? `<span class="miss" title="Picture file not found" aria-hidden="true">${icon('warn')}</span>` : `<img src="/api/history/${e.id}/thumb" alt="">`;
      return `<button class="rrow" type="button" data-g="${i}" draggable="${!miss}" data-id="${e.id}"${miss ? ` aria-label="${esc(`${t}, ${m}, picture file not found`)}"` : ''}>${th}<span style="min-width:0"><div class="t" title="${esc(e.prompt)}">${esc(t)}</div><div class="m">${m}</div></span></button>`;
    });
    const title = j => j.req.prompt || 'Upscaling ×4';
    // separate jobs waiting behind the running one, in queue order; frames of one job wait in the strip
    const waiting = run ? [...jobs.values()].filter(j => j !== run && j.state === 'queued').sort((a, b) => (a.pos ?? 1e9) - (b.pos ?? 1e9)).map(j => `<div class="rrow q"><canvas data-dev style="opacity:.6"></canvas><span style="min-width:0"><div class="t" title="${esc(j.req.prompt)}">${esc(title(j))}</div><div class="m"><span class="sdot hollow"></span>queued${j.pos ? ' · ' + (j.pos > 1 ? nth(j.pos) : 'next') : ''}</div></span><button class="btn ghost icon sm" type="button" data-q="${esc(j.id)}" aria-label="Cancel: ${esc(title(j))}" title="Cancel">${icon('x', 'sm')}</button></div>`) : [];
    if (run) rows.unshift(`<button class="rrow" type="button" data-run><canvas data-dev></canvas><span style="min-width:0"><div class="t" title="${esc(run.req.prompt)}">${esc(title(run))}</div><div class="m">${run.state === 'queued' ? '<span class="sdot hollow"></span>' : loader()}${run.state === 'queued' ? 'waiting' : run.state === 'preparing' ? 'preparing' : `developing · ${run.frame || 1} of ${run.frames}`}</div></span></button>`, ...waiting);
    $('#recent').innerHTML = rows.join('') || `<div class="empty" style="padding:48px 8px;align-self:center">${icon('image', 'lg')}<span class="t-body-strong">Nothing yet</span><p class="help">Your pictures will show up here.</p></div>`;
    $$('#recent [data-dev]').forEach(c => requestAnimationFrame(() => window.DevelopField.still(c, 3, 1.4)));
  }
  $('#recent').addEventListener('click', e => {
    const q = e.target.closest('[data-q]'); if (q) return cancel(jobs.get(q.dataset.q));
    const r = e.target.closest('.rrow'); if (!r) return;
    if (r.dataset.g != null && !active(view)) { show(groups[+r.dataset.g]); keep(); }
    if (r.dataset.run != null && runJob && runJob !== view) show(runJob);
    recentOpen(false);
  });
  // a thumb whose file was moved or deleted (/thumb 404) becomes the missing-picture thumb
  // shortcut: a file put back still shows as missing until the page reloads, fine for a rare case
  $('#recent').addEventListener('error', e => { const r = e.target.closest?.('[data-id]'); if (r) { gone.add(r.dataset.id); drawRecent(); } }, true);
  $('#recent').addEventListener('dragstart', e => { const r = e.target.closest('[data-id]'); if (r) e.dataTransfer.setData(ENTRY_TYPE, r.dataset.id); }); // into the Picture slot
  function srcOf(p) { // stored picture path -> URL: a history picture or an upload
    for (const g of [...groups, ...jobs.values()]) for (const e of g.entries || []) if (e.image === p) return `/api/history/${e.id}/image`;
    const n = (p || '').split(/[\\/]/).pop();
    return /^[0-9a-f]{32}\.(png|jpg|webp)$/.test(n) ? '/api/uploads/' + n : '';
  }
  // narrow window: Recent opens as a popover from the tray header
  const dev = $('#screen-develop'), rb = $('#recent-btn');
  function recentOpen(open) { dev.classList.toggle('recent-open', open); rb.setAttribute('aria-expanded', String(open)); }
  rb.onclick = () => recentOpen(!dev.classList.contains('recent-open'));
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && dev.classList.contains('recent-open')) { recentOpen(false); rb.focus(); } });
  document.addEventListener('mousedown', e => { if (dev.classList.contains('recent-open') && !e.target.closest('.col-right, #recent-btn')) recentOpen(false); });

  // ---------- start ----------
  const ref = e => { d2.toNew(); return addRef(e); }; // Use as reference always lands in New
  d2 = initDevelop2({ app, ui, show, running: () => active(view), header, overlay, setAspect, layoutTray, summary, checkFit, v1Prompt, setSize, submit, upscale, addRef: ref, current: () => view?.entries?.[view.sel ?? 0], srcOf });
  enh = initEnhance({ app, running: anyActive, edit: () => ui.refs.length > 0 || d2.from() });
  renderLoras(); setSize(1024, 1024); summary();
  // launch with history opens the newest Recent group, as a click on that row does
  recent().then(() => view || (groups[0] ? show(groups[0]) : render(null)));
  api('/api/events?since=0').then(async r => { // old events are not ours; jobs still queued are (after a crash or a reload)
    seq = r.last_seq;
    for (const x of (await api('/api/jobs')).jobs) {
      const q = x.request;
      jobs.set(x.id, { id: x.id, req: q, frames: q.batch, w: q.width, h: q.height, model: app.models[q.model_id], state: x.status === 'running' ? 'preparing' : 'queued', ahead: 0 });
    }
    const run = [...jobs.values()].find(active);
    if (run) { window.dispatchEvent(new Event('lr-busy')); show(run); }
  }).catch(() => {}).finally(poll);
  function size() { return d2?.fitSize() || { w: ui.w, h: ui.h, refs: ui.refs.length }; } // fit is checked for this (footer, Models)
  // The fit question (footer, Models): the size, and the LoRAs + Turbo add-on on, which the queue
  // counts too. LoRA names joined by '/', which no file name holds.
  function fitQuery() { const s = size(); return `width=${s.w}&height=${s.h}&refs=${s.refs}&loras=${encodeURIComponent(ui.loras.map(l => l.name).join('/'))}&turbo=${+ui.turbo}`; }
  return { show: () => { layoutTray(); recent(); }, fill, addRef: ref, upscale, size, setSize, fitQuery, running: anyActive };
}
