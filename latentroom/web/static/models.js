// Models screen: hardware cards, picture models with fit + downloads, shared parts, LoRAs.
// shortcut: quality labels come from the catalogue as they are ("Best quality", "Fast, balanced"), the design has
// shorter ones; help lines are keyed by quant level.
import { $, $$, SEP, esc, icon, api, toast, gb, GiB, sizeGB as size, fitLine, openDialog, closeDialog, dlgActs, confirmDialog, pickFolder, modelLabel as label, modelName, USE_RULE, QWEN_LICENCE } from './ui.js';

const SEARCH = ['embedding', 'embedding_vision', 'llama']; // Smart search: its 3 files are ONE row, all or nothing
export const PART = { // file kind -> name, help; this order is the display order
  llm: ['Text encoder', 'Reads your prompt. Qwen3-VL 8B Q4_K_M.'],
  llm_vision: ['Vision encoder', 'Reads reference pictures when you edit.'],
  vae: ['Image decoder (VAE)', 'Turns the result into pixels.'],
  upscaler: ['Upscaler', 'Makes pictures 4x bigger. Real-ESRGAN x4plus.'],
  detector: ['Face detector', 'Finds the faces for Fix faces in Develop.'],
  lora: ['Turbo LoRA', 'Used when Turbo is on in Develop.'],
  ...Object.fromEntries(SEARCH.map(k => [k, ['Smart search', 'Find pictures by what is in them. Runs on the processor. About 0.8 GB.']])),
};
const order = f => (Object.keys(PART).indexOf(f.kind) + 99) % 99;
export const byPart = (a, b) => order(a) - order(b); // sort files in PART order, unknown kinds last (Models, wizard)
export const HELP = { q2: 'Runs on small cards. Visibly less detail.', q3: 'For smaller cards. Less detail than Balanced.', q4: 'Good detail. The usual pick.', q5: 'A little sharper than Balanced.', q6: 'Sharper fine detail, bigger file.', q8: 'Closest to the original model.' };
// The model family by name, the same words in Models and wizard step 3: the column headers
// say Qwen-Image 2.1, a Fast row says Qwen-Image 2.1 Turbo + catalogue.fast_help.
const QWEN = modelName({ family: 'standard' });
export const SIDE_HELP = { standard: `${QWEN}, the official model`, uncensored: `${QWEN}, community model` };
export const rowHelp = (p, cat) => (p.fast ? esc(`${modelName(p.standard)}. ${cat.fast_help}`) : HELP[p.id] || '');
const SIDES = ['standard', 'uncensored'];
const ACTIVE = ['queued', 'downloading', 'paused', 'verifying', 'failed'];
const MOVING = ['queued', 'downloading', 'verifying']; // poll every 1 s while one of these is on
const RANK = ['failed', 'paused', 'downloading', 'verifying', 'queued']; // status of a block of several files
const pct = d => (d.total ? Math.floor(100 * d.done / d.total) : 0);
export const and = a => (a.length < 2 ? a.join('') : a.slice(0, -1).join(', ') + ' and ' + a.at(-1)); // "A, B and C" (Models, wizard)

// One download made of several files (the model + parts it needs + engine), shown as one.
export function merge(list) {
  const done = list.reduce((n, d) => n + d.done, 0), total = list.reduce((n, d) => n + d.total, 0);
  const speed = list.reduce((n, d) => n + (d.status === 'downloading' ? d.speed : 0), 0);
  return {
    ids: list.map(d => d.id), status: RANK.find(s => list.some(d => d.status === s)), done, total, speed,
    eta: speed ? (total - done) / speed : null, error: list.find(d => d.status === 'failed')?.error || '',
  };
}

export function dlLine(d) {
  const got = `${gb(d.done, 2)}/${size(d.total)}`;
  if (d.status === 'queued') return `Waiting${SEP}${size(d.total)}`;
  if (d.status === 'paused') return ['Paused', pct(d) + '%', got].join(SEP);
  if (d.status === 'verifying') return 'Checking the file…';
  const left = d.eta == null ? [] : [d.eta < 60 ? 'under 1 min left' : Math.ceil(d.eta / 60) + ' min left'];
  return [pct(d) + '%', got, Math.round(d.speed / (GiB / 1024)) + ' MB/s', ...left].join(SEP);
}

const xBtn = k => `<button class="btn ghost icon sm" type="button" data-dl="cancel" data-k="${k}" aria-label="Cancel download" title="Cancel download">${icon('x', 'sm')}</button>`;
function dlHTML(k, d) {
  if (d.status === 'failed') return `<div class="errline" data-blk="${k}" data-st="failed">${icon('alert', 'sm')}<span>${esc(d.error)}</span></div><div class="act"><button class="btn sm" type="button" data-dl="resume" data-k="${k}">${icon('redo', 'sm')}Try again</button>${xBtn(k)}</div>`;
  const p = pct(d), btn = d.status === 'downloading' ? `<button class="btn ghost icon sm" type="button" data-dl="pause" data-k="${k}" aria-label="Pause download" title="Pause download">${icon('pause', 'sm')}</button>`
    : d.status === 'paused' ? `<button class="btn ghost icon sm" type="button" data-dl="resume" data-k="${k}" aria-label="Resume download" title="Resume download">${icon('play', 'sm')}</button>` : '';
  return `<div class="dl" role="group" aria-label="Download ${p}%" data-blk="${k}" data-st="${d.status}"><div class="bar${d.status === 'paused' ? ' paused' : ''}" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${p}"><i style="width:${d.status === 'verifying' ? 100 : p}%"></i></div><span class="line">${dlLine(d)}</span>
      <div class="btns">${btn}${d.status === 'verifying' ? '' : xBtn(k)}</div></div>`;
}

export function initModels(app) {
  const root = $('#screen-models');
  $('#lic-note').innerHTML = QWEN_LICENCE; // fixed words: set once, before the screen is first shown
  const M = { cat: { pairs: [], files: [] }, hw: null, engine: null, inst: null, loras: [], dls: {}, showAll: false, timer: 0, at: null };
  let models = {}, parts = {}; // id -> Model (+ pair) / FileItem
  const live = id => M.dls[id] && ACTIVE.includes(M.dls[id].status);

  // ---------- data ----------
  async function load() {
    const s = app.develop.size();
    M.at = s;
    try {
      const [hw, engine, inst, cat, dl] = await Promise.all([api('/api/hardware'), api('/api/engine'), api('/api/models'),
        api(`/api/catalogue?${app.develop.fitQuery()}`), api('/api/downloads')]);
      Object.assign(M, { hw, engine, inst, cat });
      models = {}; parts = {};
      for (const p of cat.pairs) for (const sd of SIDES) if (p[sd]) models[p[sd].id] = { ...p[sd], pair: p };
      for (const f of cat.files.toSorted(byPart)) parts[f.id] = f;
      setDls(dl.downloads);
    } catch (e) { toast(e.message); }
    M.loras = await api('/api/loras').then(r => r.loras, () => []);
    redraw(); poll();
  }
  function setDls(list) { const before = M.dls; M.dls = Object.fromEntries(list.map(d => [d.id, d])); return before; }
  // Files that are neither a picture model nor a listed part (the engine zips) join every model's block.
  const others = () => Object.values(M.dls).filter(d => ACTIVE.includes(d.status) && !models[d.id] && !parts[d.id]);
  function block(m) {
    if (!M.dls[m.id] || m.installed) return null;
    const g = [m.id, ...m.needs].filter(live).map(i => M.dls[i]).concat(others());
    return g.length ? merge(g) : null;
  }
  const adult = () => !!app.settings.adult_ok;
  function rows() { // a row whose models are all too big is hidden unless "show" is on, or one is in use / on disk / coming
    const keep = m => m && (m.fit.label !== 'too_big' || m.installed || M.dls[m.id] || m.id === app.settings.model_id);
    const all = M.cat.pairs, shown = M.showAll ? all : all.filter(p => SIDES.some(sd => keep(p[sd])));
    return { shown, hidden: all.filter(p => !shown.includes(p)).reduce((n, p) => n + SIDES.filter(sd => p[sd]).length, 0) };
  }
  // Rows of the parts list: one per file, except the Smart search files, which share one row.
  function partRows() {
    const out = {};
    for (const f of Object.values(parts)) {
      const k = SEARCH.includes(f.kind) ? 'search' : f.id, r = out[k] ||= { k, f, ids: [], size: 0 };
      r.ids.push(f.id); r.size += f.size;
    }
    return Object.values(out);
  }
  function blocks() { // what draw() puts on screen right now: key -> merged download
    const out = {};
    for (const p of rows().shown) for (const sd of SIDES) {
      const m = p[sd] && models[p[sd].id]; // the model in use shows "In use", never a download (cell)
      if (m && m.id !== app.settings.model_id && (sd === 'standard' || adult())) { const b = block(m); if (b) out[m.id] = b; }
    }
    for (const r of partRows()) { const g = r.ids.filter(live); if (g.length) out[r.k] = merge(g.map(i => M.dls[i])); }
    return out;
  }

  // ---------- drawing ----------
  function cards() {
    const { hw, inst, engine } = M, g = hw?.gpu, bad = hw && hw.status !== 'ok';
    const gpu = g ? g.name.replace(/^NVIDIA GeForce /, '') : '';
    const used = g ? g.vram_total - g.vram_free : 0, rused = hw ? hw.ram_total - hw.ram_free : 0;
    const store = inst ? Object.values(inst.disk_use).reduce((a, b) => a + b, 0) : 0;
    const bar = (a, b) => `<div class="bar"><i style="width:${b ? Math.round(100 * a / b) : 0}%"></i></div>`;
    const eng = engine?.installed ? `<span class="st"><span class="sdot ok"></span>Ready</span>` : `<span class="st"><span class="sdot warn"></span>Not installed</span>`;
    return `
      <div class="card"><div class="top"><span class="label">Graphics card</span>${bad ? '<span class="st"><span class="sdot warn"></span>Check</span>' : ''}</div>${g ? `<div class="big">${gb(used)} <small>/ ${gb(g.vram_total)} GB</small></div>` : ''}${bad ? `<div class="foot">${esc(hw.message)}</div>` : g ? `${bar(used, g.vram_total)}<div class="foot">video memory in use<br>${esc(gpu)}${SEP}driver&nbsp;${esc(hw.driver)}</div>` : ''}</div>
      <div class="card"><div class="top"><span class="label">System memory</span></div>${hw ? `<div class="big">${gb(rused)} <small>/ ${gb(hw.ram_total)} GB</small></div>${bar(rused, hw.ram_total)}` : ''}<div class="foot">in use by all apps</div></div>
      <div class="card"><div class="top"><span class="label">Model storage</span><button class="btn ghost icon sm" type="button" data-open="data" aria-label="Show the data folder" title="Show the data folder">${icon('folder', 'sm')}</button></div><div class="big">${gb(store)} <small>GB</small></div>${inst ? `<div class="foot">${gb(inst.free, 0)} GB free on this drive</div><div class="foot path" title="${esc(inst.data_dir)}">${esc(inst.data_dir)}</div>` : ''}</div>
      <div class="card"><div class="top"><span class="label">Engine</span>${engine ? eng : ''}</div><div class="big ver">${esc(engine?.release)}</div><div class="foot">stable-diffusion.cpp, pinned release</div>${engine && !engine.installed ? '<div><button class="btn sm" type="button" data-engine>Install</button></div>' : ''}</div>`;
  }
  function fitHTML(m) {
    const [c, a, b] = fitLine(m.fit);
    return `<div class="fit" title="${esc(m.fit.reason)}"><span class="sdot ${c}"></span><span>${a}</span>${c !== 'ok' ? `<span class="c3 help">${b}</span>` : ''}</div>`;
  }
  function cell(p, sd) {
    if (!p[sd]) return `<div class="mc none" role="cell"><span class="help">No ${sd} file at this level.</span></div>`;
    const m = models[p[sd].id], b = block(m);
    let act = '';
    if (m.id === app.settings.model_id) act = `<div class="act"><span class="st" tabindex="-1" data-inuse="${m.id}">${icon('check', 'sm')}In use</span></div>`;
    else if (b) act = dlHTML(m.id, b);
    else if (m.installed) act = `<div class="act"><button class="btn primary sm" type="button" data-use="${m.id}">Use this</button><button class="btn ghost icon sm" type="button" data-rm="${m.id}" aria-label="Delete the ${esc(label(m))}" title="Delete">${icon('trash', 'sm')}</button></div>`;
    else if (m.fit.label !== 'too_big') act = `<div class="act"><button class="btn sm" type="button" data-get="${m.id}">${icon('download', 'sm')}Download</button></div>`;
    return `<div class="mc" role="cell"><span class="name">${size(m.file.size)}<span class="sz">${esc(m.quant)}</span></span>${fitHTML(m)}${act}</div>`;
  }
  function draw() {
    $('#hw').innerHTML = cards();
    const { shown, hidden } = rows(), g = M.hw?.gpu, s = M.at;
    let html = `<div class="colh" role="columnheader">Quality</div><div class="colh m" role="columnheader">Standard<span class="help">${SIDE_HELP.standard}</span></div><div class="colh m" role="columnheader">Uncensored <span class="badge quiet">18+</span><span class="help">${SIDE_HELP.uncensored}</span></div>`;
    shown.forEach((p, i) => {
      const exp = p.standard.experimental ? '<span class="badge quiet" title="Still being tested on real cards.">Experimental</span>' : '';
      const help = rowHelp(p, M.cat);
      html += `<div class="qc" role="rowheader"><span class="n">${esc(p.label)}${p.id === M.cat.recommended ? '<span class="badge">Recommended</span>' : ''}${exp}</span>${help ? `<span class="help">${help}</span>` : ''}</div>`;
      html += cell(p, 'standard');
      if (adult()) html += cell(p, 'uncensored');
      else if (i === 0) html += `<div class="adult" style="grid-row: 2 / span ${shown.length}"><span class="t-body-strong">Uncensored models are hidden</span><p class="help">Community models with no content filter. Only for people 18 or older. Latentroom remembers your answer on this PC.</p><p class="help">${USE_RULE}</p><div class="acts"><button class="btn" type="button" data-adult>I am 18 or older, show them</button></div></div>`;
    });
    $('#pairs').innerHTML = html;
    const sa = $('#showall'); sa.hidden = !hidden && !M.showAll;
    sa.textContent = M.showAll ? 'Hide models too big for this PC' : `Show ${hidden} models too big for this PC`;
    // references cost video memory too: name them, or a Partial label has no visible reason
    const refs = s.refs ? ` with ${s.refs} reference picture${s.refs > 1 ? 's' : ''}, as set in Develop` : ', the size set in Develop';
    $('#fit-note').textContent = `Fit is checked for ${s.w} × ${s.h}${refs}` + (g ? `, on this PC (${g.name.replace(/^NVIDIA GeForce /, '')}, ${gb(g.vram_total)} GB).` : '.');
    const have = new Set(M.inst?.installed || []);
    $('#parts').innerHTML = partRows().map(r => {
      const [n, help] = PART[r.f.kind] || [r.f.name, ''], g = r.ids.filter(live);
      const del = r.k === 'search' ? `<button class="btn ghost icon sm" type="button" data-rm="${r.k}" aria-label="Delete ${n}" title="Delete">${icon('trash', 'sm')}</button>` : '';
      const st = g.length ? dlHTML(r.k, merge(g.map(i => M.dls[i]))) : r.ids.every(i => have.has(i)) ? `<span class="st">${icon('check', 'sm')}Installed</span>${del}`
        : `<button class="btn sm" type="button" data-part="${r.k}">${icon('download', 'sm')}Download</button>`;
      return `<div class="prow"><div><div class="n">${n}</div><p class="help">${help}</p></div><span class="sz">${size(r.size)}</span><div class="s">${st}</div></div>`;
    }).join('');
    $('#loras').innerHTML = M.loras.length ? M.loras.map(n => `<div class="lorarow"><span>${esc(n)}</span><span class="help">.safetensors</span></div>`).join('')
      : `<div class="empty">${icon('layers', 'lg')}<span class="t-body-strong">No LoRAs yet</span><p class="help">Put LoRA files in the LoRA folder, then press the refresh button.</p></div>`;
  }
  // Re-draw, then put focus back on the same control (or what replaced it for the same model / part).
  // Polls re-draw too: focus comes back without scrolling, so the page stays where the user put it.
  function redraw(next) {
    const a = document.activeElement, mine = a && root.contains(a) && a !== root;
    const k = mine ? a.dataset.k || a.dataset.get || a.dataset.use || a.dataset.rm || a.dataset.part || a.dataset.inuse || '' : '';
    const same = mine && (a.id ? '#' + a.id : k && a.dataset.dl ? `[data-k="${k}"][data-dl="${a.dataset.dl}"]` : '');
    draw();
    const q = s => s && root.querySelector(s);
    const t = q(next) || q(same) || (k && (q(`[data-k="${k}"][data-dl="pause"]`) || q(`[data-k="${k}"]`) || q(`[data-get="${k}"]`) || q(`[data-part="${k}"]`) || q(`[data-use="${k}"]`) || q(`[data-inuse="${k}"]`)));
    const keep = { preventScroll: true };
    if (t) t.focus(keep); else if (mine && !a.isConnected) root.querySelector('button')?.focus(keep);
  }
  // Poll result: progress repaints in place; a status change re-draws (and keeps focus).
  function paint() {
    const now = blocks(), shown = $$('[data-blk]', root);
    if (shown.length !== Object.keys(now).length || shown.some(el => now[el.dataset.blk]?.status !== el.dataset.st)) return redraw();
    for (const el of shown) {
      const d = now[el.dataset.blk], bar = el.querySelector('.bar');
      if (!bar) continue;
      bar.setAttribute('aria-valuenow', pct(d)); bar.firstElementChild.style.width = (d.status === 'verifying' ? 100 : pct(d)) + '%';
      el.querySelector('.line').innerHTML = dlLine(d); el.setAttribute('aria-label', `Download ${pct(d)}%`);
    }
  }
  function poll() {
    clearTimeout(M.timer);
    if (!root.hidden && Object.values(M.dls).some(d => MOVING.includes(d.status))) M.timer = setTimeout(tick, 1000);
  }
  async function tick() {
    try {
      const before = setDls((await api('/api/downloads')).downloads);
      const ended = Object.keys(M.dls).filter(id => M.dls[id].status === 'done' && before[id] && before[id].status !== 'done');
      if (!ended.length) { paint(); return poll(); }
      const have = new Set(M.inst?.installed || []), said = new Set();
      for (const id of ended) {
        if (models[id]) toast(`The ${label(models[id])} is ready to use.`);
        else if (parts[id]) { // a row of several files (Smart search) is installed once all of them are
          const r = partRows().find(r => r.ids.includes(id));
          if (!said.has(r.k) && r.ids.every(i => have.has(i) || M.dls[i]?.status === 'done')) { said.add(r.k); toast(`${(PART[r.f.kind] || [r.f.name])[0]} is installed.`); }
        }
      }
      await app.refresh(); // Develop and the chip learn the model is on disk
      await load();
    } catch { poll(); } // a missed poll is retried; the server keeps downloading either way
  }
  async function refreshDls(next) {
    setDls((await api('/api/downloads')).downloads);
    redraw(next); poll();
  }

  // ---------- actions ----------
  async function startDl(bodies, k) {
    for (const body of bodies) for (const d of (await api('/api/downloads', body)).downloads) M.dls[d.id] = d;
    await refreshDls(`[data-k="${k}"]`);
  }
  async function dlAct(act, k) {
    const b = blocks()[k]; if (!b) return;
    const want = { pause: ['queued', 'downloading'], resume: ['paused', 'failed'], cancel: ACTIVE }[act];
    if (act === 'cancel') {
      const yes = await confirmDialog('Cancel this download?', `The ${gb(b.done, 2)} GB downloaded so far is deleted. Pause keeps it instead.`,
        '<button class="btn" type="button" data-yes>Cancel download</button>', 'Keep downloading');
      if (!yes) return;
    }
    for (const id of b.ids) if (want.includes(M.dls[id].status)) await api(`/api/downloads/${encodeURIComponent(id)}/${act}`, {});
    if (act === 'cancel') toast('Download cancelled. The partial file is deleted.');
    await refreshDls(act === 'cancel' ? `[data-get="${k}"], [data-part="${k}"]` : '');
  }
  async function use(id) {
    await api('/api/settings', { model_id: id });
    await app.refresh();
    toast(`Develop now uses the ${label(models[id])}.`);
    redraw(`[data-inuse="${id}"]`);
  }
  async function remove(id) {
    const m = models[id];
    const yes = await confirmDialog(`Delete the ${esc(label(m))}?`, `This frees ${size(m.file.size)}. Pictures you made with it stay in Library, and you can download it again any time.`,
      `<button class="btn" type="button" data-yes>${icon('trash', 'sm')}Delete</button>`);
    if (!yes) return;
    await api('/api/models/' + encodeURIComponent(id), undefined, 'DELETE');
    toast(`The ${label(m)} is deleted.`);
    await app.refresh(); await load();
    root.querySelector(`[data-get="${id}"]`)?.focus();
  }
  async function removePart(k) { // Smart search: all its files together
    const r = partRows().find(r => r.k === k), n = PART[r.f.kind][0];
    const yes = await confirmDialog(`Delete ${n}?`, `This frees ${size(r.size)}. Your pictures stay in Library, and you can download it again any time.`,
      `<button class="btn" type="button" data-yes>${icon('trash', 'sm')}Delete</button>`);
    if (!yes) return;
    // The program first: Windows refuses while it runs, and then nothing is deleted (409, toast).
    const ids = r.ids.toSorted((a, b) => (parts[b].kind === 'llama') - (parts[a].kind === 'llama'));
    try {
      for (const id of ids) await api('/api/models/' + encodeURIComponent(id), undefined, 'DELETE');
      toast(`${n} is deleted.`);
    } finally { await app.refresh(); await load(); }
    root.querySelector(`[data-part="${k}"]`)?.focus();
  }
  function askImport() {
    openDialog(`<h2 id="dlg-title">Import models from a folder</h2><p>Latentroom copies the files that match its list of models (same size and checksum). Your folder is not changed.</p>
      <div class="field"><span class="label">Folder</span><div style="display:flex;gap:8px"><input class="input" id="imp-path" readonly placeholder="No folder chosen" aria-label="Folder"><button class="btn" type="button" data-pick data-first>Choose…</button></div></div>
      ${dlgActs('Cancel', '<button class="btn primary md" type="button" data-yes disabled>Import</button>')}`, async ev => {
      if (ev.target.closest('[data-close]')) return closeDialog();
      if (ev.target.closest('[data-pick]')) {
        const f = await pickFolder($('#imp-path').value);
        if (f) { $('#imp-path').value = f; $('#dlg [data-yes]').disabled = false; }
        return;
      }
      const y = ev.target.closest('[data-yes]'); if (!y || y.disabled) return;
      y.disabled = true; y.textContent = 'Importing…';
      try {
        const { imported } = await api('/api/models/import', { folder: $('#imp-path').value });
        closeDialog();
        const names = [...new Set(imported.map(id => models[id] ? label(models[id]).replace(/ model$/, '') : (PART[parts[id]?.kind] || [id])[0]))]; // Smart search: 3 files, 1 name
        toast(names.length ? `Imported ${names.length} ${names.length === 1 ? 'model' : 'models'}: ${and(names)}.` : 'No matching model files in that folder.');
        await app.refresh(); await load();
      } catch (e) { toast(e.message); y.disabled = false; y.textContent = 'Import'; }
    });
  }
  async function installEngine(b) {
    b.disabled = true; b.textContent = 'Installing…';
    try { await api('/api/engine/install', {}); } catch (e) { toast(e.message); }
    await load();
  }
  root.addEventListener('click', async e => {
    const b = e.target.closest('button'); if (!b || b.disabled) return;
    const d = b.dataset;
    try {
      if (d.get) await startDl([{ model_id: d.get }], d.get);
      else if (d.part) await startDl(partRows().find(r => r.k === d.part).ids.map(file_id => ({ file_id })), d.part);
      else if (d.dl) await dlAct(d.dl, d.k);
      else if (d.use) await use(d.use);
      else if (d.rm) await (models[d.rm] ? remove(d.rm) : removePart(d.rm));
      else if (d.adult != null) {
        await api('/api/settings', { adult_ok: true }); await app.refresh();
        toast('Uncensored models are shown. You can hide them again in Settings.'); redraw();
        root.querySelector('#pairs [data-use], #pairs [data-get], #pairs [data-inuse]')?.focus();
      } else if (b.id === 'showall') { M.showAll = !M.showAll; redraw('#showall'); }
      else if (d.import != null) askImport();
      else if (d.open) await api('/api/open', { what: d.open }); // [data-open-loras]: develop.js opens it, page-wide
      else if (d.loraRefresh != null) {
        M.loras = (await api('/api/loras')).loras; redraw();
        toast(M.loras.length ? `${M.loras.length} ${M.loras.length === 1 ? 'LoRA' : 'LoRAs'} found.` : 'No LoRAs found.');
      } else if (d.engine != null) await installEngine(b);
    } catch (err) { toast(err.message); }
  });
  window.addEventListener('lr-model', () => { if (!root.hidden && M.inst) redraw(); });

  return { show: load };
}
