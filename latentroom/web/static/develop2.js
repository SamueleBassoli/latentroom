// Develop v2: Start "From a picture" with Rework (img2img), Repaint a part
// (inpaint + mask painter), Add detail (enhance), Fix faces (facefix); done header Compare + Touch up.
// Plugs into develop.js through the hooks it returns.
import { $, $$, SEP, esc, icon, api, toast, PHONE } from './ui.js';

// Rework 0.70-0.95, default 0.85: sd.cpp strength = share of the steps run, and the
// Qwen-Image flow schedule leaves the picture almost as it was up to 0.75 (live: mean change
// ~10/255 at 0.6 and 0.75, a full restyle at 0.9); at 1 it ignores the picture (no steps cut).
export const KINDS = { // id = JobRequest.kind
  img2img: { name: 'Rework', icon: 'refresh', help: 'Redraws the whole picture. Keeps its layout and colours.', label: 'Prompt', ph: 'Describe the new picture, e.g. the same hut in a snowstorm', need: 'Write what you want to see first.', min: 0.7, max: 0.95, def: 0.85, shelp: 'Low keeps more of the picture. High follows the prompt more.', f: 1, frames: true },
  inpaint: { name: 'Repaint a part', short: 'Repaint', icon: 'brush', help: 'Paint over a part. Only that part changes.', label: 'What to paint there', ph: 'e.g. a small red rowing boat on the ice', need: 'Say what to paint there first.', min: 0.05, max: 1, def: 0.75, shelp: 'Low keeps the painted part close to what is there now.', f: 1, frames: true },
  enhance: { name: 'Add detail', icon: 'sparkle', help: 'Makes it up to 2x bigger and adds fine detail.', label: 'Prompt (optional)', ph: 'Describe the picture for better detail', need: '', min: 0.05, max: 0.35, def: 0.35, shelp: 'Higher adds more new detail. 0.35 at most.', f: 2, frames: false },
  facefix: { name: 'Fix faces', icon: 'face', help: 'Finds the faces and redraws them sharper.', label: 'Prompt for the faces (optional)', ph: 'a detailed natural face, sharp focus', need: '', min: 0.05, max: 0.4, def: 0.4, shelp: 'Higher changes the faces more. 0.40 at most.', f: 1, frames: false },
};
const IMG_TYPES = ['image/png', 'image/jpeg', 'image/webp'];
export const ENTRY_TYPE = 'application/x-latentroom-entry'; // a Recent row dragged into the Picture slot
const snap = x => Math.max(256, Math.min(2048, Math.floor(x / 32) * 32)); // engine jobs._snap
export function outSize(kind, p) { // engine jobs.job_size
  if (kind === 'facefix') return [p.w, p.h];
  const s = Math.max(Math.min(KINDS[kind] ? KINDS[kind].f : 1, 2048 / Math.max(p.w, p.h)), 256 / Math.min(p.w, p.h));
  return [snap(Math.round(p.w * s)), snap(Math.round(p.h * s))];
}
const tooBig = p => Math.max(p.w, p.h) > 2048;
const fromEntry = e => ({ path: e.image, url: `/api/history/${e.id}/image`, name: e.image.split(/[\\/]/).pop(), w: e.width, h: e.height, prompt: e.prompt });

// X = develop.js internals: app, ui, show, running, header, overlay, setAspect, layoutTray, summary,
// checkFit, v1Prompt, setSize, submit, upscale, addRef, current, entryByImage
export function initDevelop2(X) {
  const { ui } = X;
  // detector + upscaler count as missing until /api/models says otherwise: nothing is offered unchecked
  const v = { mode: 'new', kind: 'img2img', pic: null, str: {}, detector: 'missing', upscaler: false, tool: 'brush', brush: 64, strokes: [], cover: 0, painting: null, sizePrev: null };
  const mk = document.createElement('canvas'); // the mask at picture resolution: opaque = repaint

  // ---------- DOM: left column ----------
  const el = (id, html) => { const d = document.createElement('div'); d.className = 'field'; d.id = id; d.hidden = true; d.innerHTML = html; return d; };
  const refsG = $('#g-refs'), sizeG = $('#sizes').closest('.field'), framesG = $('#frames').closest('.field');
  const startG = el('g-start', `<span class="label" id="start-label">Start</span><div class="seg words" role="radiogroup" aria-labelledby="start-label" id="start"><button type="button" role="radio" aria-checked="true" data-m="new">${icon('plus', 'sm')}New</button><button type="button" role="radio" aria-checked="false" data-m="from">${icon('image', 'sm')}From a picture</button></div>`);
  startG.hidden = false;
  const picG = el('g-pic', `<div class="lab-row"><span class="label">Picture</span></div>
    <button class="drop tall" type="button" id="pic-drop">${icon('image')}<span><span class="t-body">Drop a picture or <u>browse</u></span><br><span class="help">PNG, JPG or WebP. Or drag one from Recent.</span></span></button>
    <input type="file" id="pic-file" accept="image/png,image/jpeg,image/webp" hidden>
    <div class="picslot" id="pic-slot" hidden></div>
    <div class="err-line" id="pic-err" hidden>${icon('alert', 'sm')}Add a picture first.</div>`);
  const doG = el('g-do', `<span class="label" id="do-label">What to do</span><div class="dolist" role="radiogroup" aria-labelledby="do-label" id="dolist"></div>`);
  const strG = el('g-str', `<div class="lab-row"><label class="label" for="str">Strength</label><span class="val" id="str-v">${KINDS.img2img.def}</span></div><input class="range" type="range" id="str" min="${KINDS.img2img.min}" max="${KINDS.img2img.max}" step="0.05" value="${KINDS.img2img.def}"><div class="ends" aria-hidden="true"><span>keep</span><span>change</span></div><p class="help" id="str-help"></p>`);
  const outG = el('g-out', `<div class="outrow"><span class="label">Output size</span><span class="val" id="out-v"></span></div><p class="help" id="out-help"></p>`);
  refsG.before(startG); refsG.after(picG); picG.after(doG); sizeG.before(strG); sizeG.before(outG);

  // ---------- DOM: tray layers + mask toolbar ----------
  $('#field').insertAdjacentHTML('afterend', `<img id="pic" hidden alt=""><canvas id="mask" hidden role="img" aria-label="Mask: nothing painted yet"></canvas><div class="ring" id="ring" hidden></div><span class="tag before" id="before-tag" hidden>Before</span>`);
  $('#tray-wrap').insertAdjacentHTML('afterend', `<div class="mbar-wrap" id="mbar-wrap" hidden><div class="mbar" role="toolbar" aria-label="Mask tools">
    <div class="seg words" role="radiogroup" aria-label="Tool" id="mtool"><button type="button" role="radio" aria-checked="true" data-t="brush" title="Brush (B)">${icon('brush', 'sm')}Brush</button><button type="button" role="radio" aria-checked="false" data-t="eraser" title="Eraser (E)">${icon('eraser', 'sm')}Eraser</button></div>
    <span class="vsep"></span><label class="label" for="bsize">Size</label><input class="range" type="range" id="bsize" min="8" max="256" step="4" value="64" title="Brush size in picture pixels ([ smaller, ] bigger)"><span class="val" id="bsize-v">64</span>
    <span class="vsep"></span><button class="btn ghost icon sm" type="button" id="m-undo" aria-label="Undo" title="Undo (Ctrl + Z)">${icon('undo', 'sm')}</button><button class="btn ghost sm" type="button" id="m-clear">Clear</button></div>
    <div class="err-line" id="mask-err" hidden>${icon('alert', 'sm')}Paint over the part you want to change first.</div></div>`);

  // ---------- helpers ----------
  const from = () => v.mode === 'from';
  function check(group, sel) { $$(group + ' [role=radio]').forEach(b => b.setAttribute('aria-checked', String(b.matches(sel)))); }
  function arrows(group, vertical) {
    $(group).addEventListener('keydown', e => {
      const keys = vertical ? ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'] : ['ArrowLeft', 'ArrowRight']; if (!keys.includes(e.key)) return;
      const bs = $$(group + ' [role=radio]:not([aria-disabled=true])'), i = bs.findIndex(b => b.getAttribute('aria-checked') === 'true');
      const n = bs[(i + (/Right|Down/.test(e.key) ? 1 : bs.length - 1)) % bs.length]; n.click(); n.focus(); e.preventDefault();
    });
  }
  function restoreSize() { if (v.sizePrev) { const [w, h] = v.sizePrev; v.sizePrev = null; X.setSize(w, h); } }
  function useSize(p) { if (!v.sizePrev) v.sizePrev = [ui.w, ui.h]; ui.w = p.w; ui.h = p.h; }
  function setPic(p) { v.pic = p; v.strokes = []; v.cover = 0; if (p) { mk.width = p.w; mk.height = p.h; useSize(p); } else restoreSize(); }
  const toForm = () => { if (!X.running()) X.show(null); }; // a left-column change brings the tray back to the form view
  async function parts() { // upscaler / face detector on disk? (if not, their rows are disabled)
    try {
      const { installed } = await api('/api/models'), files = X.app.files || [];
      const has = k => { const f = files.filter(x => x.kind === k); return !f.length ? 'absent' : f.some(x => installed.includes(x.id)) ? 'ok' : 'missing'; };
      v.detector = has('detector'); v.upscaler = has('upscaler') === 'ok';
    } catch { /* keep the last known state: missing until one call has answered */ }
    render();
  }

  // ---------- left column ----------
  function render() {
    const k = KINDS[v.kind], f = from();
    check('#start', `[data-m="${v.mode}"]`);
    refsG.hidden = f; picG.hidden = doG.hidden = strG.hidden = !f; outG.hidden = !f || !v.pic; sizeG.hidden = f;
    framesG.hidden = f && !k.frames;
    if (!f) { X.v1Prompt(); $('#prompt-err').lastChild.textContent = 'Write what you want to see first.'; return X.summary(); }
    $('#pic-drop').hidden = !!v.pic; $('#pic-slot').hidden = !v.pic;
    if (v.pic) {
      const tw = Math.round(64 * Math.min(1.5, v.pic.w / v.pic.h));
      $('#pic-slot').innerHTML = `<button class="pthumb" type="button" id="pic-show" style="width:${tw}px" title="Show it in the tray" aria-label="Show the starting picture in the tray"><img src="${esc(v.pic.url)}" alt=""></button><div class="ptxt"><span class="pname" title="${esc(v.pic.name)}">${esc(v.pic.name)}</span><span class="psize">${v.pic.w} × ${v.pic.h}</span><button class="btn ghost sm" type="button" id="pic-replace">Replace</button></div><button class="btn ghost icon sm" type="button" id="pic-rm" aria-label="Remove the starting picture" title="Remove">${icon('x', 'sm')}</button>`;
    }
    const off = id => id === 'facefix' && v.detector === 'missing' ? 'Needs the face detector (Models tab).' : id === 'enhance' && !v.upscaler ? 'Needs the upscaler (Models tab).' : id === 'enhance' && v.pic && tooBig(v.pic) ? 'Too large: max 2048 pixels per side.' : '';
    if (off(v.kind) || (v.kind === 'facefix' && v.detector === 'absent')) { v.kind = 'img2img'; return render(); } // 15.1: a disabled selection falls back to Rework
    $('#dolist').innerHTML = Object.entries(KINDS).filter(([id]) => id !== 'facefix' || v.detector !== 'absent').map(([id, d]) => {
      const why = off(id);
      return `<button type="button" class="dorow" role="radio" aria-checked="${id === v.kind}" ${why ? 'aria-disabled="true"' : ''} data-k="${id}">${icon(d.icon)}<span class="t-body-strong">${d.name}</span><span class="help">${why || d.help}</span></button>`;
    }).join('');
    $('#prompt-label').textContent = k.label; $('#prompt').placeholder = k.ph;
    $('#prompt-err').lastChild.textContent = k.need || 'Write what you want to see first.';
    const r = $('#str'), s = v.str[v.kind] ?? k.def; // an older picture's value stays reachable
    r.min = Math.min(k.min, s); r.max = Math.max(k.max, s); r.value = s; upStr(); $('#str-help').textContent = k.shelp;
    if (v.pic) { const o = outSize(v.kind, v.pic); $('#out-v').textContent = `${o[0]} × ${o[1]}`; $('#out-help').textContent = v.kind === 'enhance' ? 'Up to 2x the picture, at most 2048 pixels per side.' : 'Same as the picture.'; }
    X.summary(); X.checkFit();
  }
  function upStr() { const r = $('#str'); r.style.setProperty('--p', ((r.value - r.min) / (r.max - r.min) * 100) + '%'); $('#str-v').textContent = (+r.value).toFixed(2); v.str[v.kind] = +r.value; }
  $('#str').addEventListener('input', upStr);

  // ---------- picture slot: drop, browse, Recent row, Replace, Remove ----------
  async function take(src) { // File or history entry id -> the starting picture
    try {
      if (typeof src === 'number') setPic(fromEntry(await api(`/api/history/${src}`)));
      else {
        if (!IMG_TYPES.includes(src.type)) return toast(`${src.name} is not a PNG, JPG or WebP picture.`);
        const r = await api('/api/uploads', src);
        setPic({ path: r.path, url: '/api/uploads/' + r.name, name: src.name, w: r.width, h: r.height, prompt: '' });
      }
    } catch (e) { return toast(e.message); }
    $('#pic-err').hidden = true; render(); toForm();
  }
  $('#pic-drop').onclick = () => $('#pic-file').click();
  $('#pic-file').addEventListener('change', e => { if (e.target.files[0]) take(e.target.files[0]); e.target.value = ''; });
  picG.addEventListener('dragover', e => { e.preventDefault(); $('#pic-drop').classList.add('over'); });
  picG.addEventListener('dragleave', () => $('#pic-drop').classList.remove('over'));
  picG.addEventListener('drop', e => {
    e.preventDefault(); $('#pic-drop').classList.remove('over');
    const id = e.dataTransfer.getData(ENTRY_TYPE), f = e.dataTransfer.files[0];
    if (id) take(+id); else if (f) take(f);
  });
  $('#pic-slot').addEventListener('click', e => {
    if (e.target.closest('#pic-rm')) { setPic(null); render(); toForm(); return $('#pic-drop').focus(); }
    if (e.target.closest('#pic-replace')) return $('#pic-file').click();
    if (e.target.closest('#pic-show')) toForm();
  });
  $('#start').addEventListener('click', e => {
    const b = e.target.closest('[data-m]'); if (!b || b.dataset.m === v.mode) return;
    v.mode = b.dataset.m; if (!from()) restoreSize(); else if (v.pic) useSize(v.pic);
    render(); toForm();
    if (from()) parts();
  });
  $('#dolist').addEventListener('click', e => { const b = e.target.closest('[data-k]'); if (!b || b.getAttribute('aria-disabled') === 'true') return; v.kind = b.dataset.k; render(); toForm(); $(`#dolist [data-k="${v.kind}"]`).focus(); });
  ['#start', '#mtool'].forEach(g => arrows(g, false)); arrows('#dolist', true);
  window.addEventListener('lr-model', parts); // the catalogue (re)loaded: are the upscaler and face detector on disk?

  // ---------- tray before Develop ----------
  const coverTxt = () => v.cover ? `${v.cover}% painted` : '';
  function formStage() {
    if (!v.pic) {
      X.header('<span class="t-title c2">Start from a picture</span>');
      X.overlay(`<div class="empty">${icon('image', 'lg')}<span class="t-body-strong">Your starting picture shows here</span><p class="help">Drop a picture on the left, or drag one from Recent.</p></div>`);
      return X.layoutTray();
    }
    X.setAspect(v.pic.w / v.pic.h);
    const p = $('#pic'); p.src = v.pic.url; p.hidden = false; $('#size-tag').textContent = `${v.pic.w} × ${v.pic.h}`;
    if (v.kind === 'inpaint') {
      $('#mask').hidden = false; $('#mbar-wrap').hidden = false; tools();
      X.header(`${icon('brush')}<span class="t-title c2">Paint over the part to change</span><span class="t-mono-sm" id="cover">${coverTxt()}</span>`);
    } else X.header(`<span class="t-title c2">Starting picture</span><span class="t-mono-sm">${esc(v.pic.name)} · ${v.pic.w} × ${v.pic.h}</span>`);
    X.layoutTray(); requestAnimationFrame(drawMask);
  }
  new ResizeObserver(drawMask).observe($('#tray'));

  // ---------- mask painter ----------
  let hatchTile = null;
  function hatch(g, dpr) {
    const P = Math.max(4, Math.round(8 * dpr)); // size.mask-hatch
    if (!hatchTile || hatchTile.width !== P) {
      const t = document.createElement('canvas'), x = t.getContext('2d'), cs = getComputedStyle(document.documentElement); t.width = t.height = P;
      x.fillStyle = cs.getPropertyValue('--color-mask-dark').trim(); x.fillRect(0, 0, P, P);
      const lines = st => { x.strokeStyle = st; x.lineWidth = P / 2 / Math.SQRT2; x.lineCap = 'square'; x.beginPath(); [0, P, 2 * P].forEach(c => { x.moveTo(c + P, -P); x.lineTo(c - 2 * P, 2 * P); }); x.stroke(); };
      x.globalCompositeOperation = 'destination-out'; lines('#000'); x.globalCompositeOperation = 'source-over'; lines(cs.getPropertyValue('--color-mask-light').trim());
      hatchTile = t;
    }
    return g.createPattern(hatchTile, 'repeat');
  }
  function drawMask() {
    const c = $('#mask'); if (c.hidden || !v.pic) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1), w = Math.round(c.clientWidth * dpr), h = Math.round(c.clientHeight * dpr); if (!w || !h) return;
    if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
    const g = c.getContext('2d'); g.globalCompositeOperation = 'source-over'; g.clearRect(0, 0, w, h);
    g.fillStyle = hatch(g, dpr); g.fillRect(0, 0, w, h);
    g.globalCompositeOperation = 'destination-in'; g.drawImage(mk, 0, 0, w, h); g.globalCompositeOperation = 'source-over';
  }
  function pen(g, s) { g.globalCompositeOperation = s.tool === 'eraser' ? 'destination-out' : 'source-over'; g.strokeStyle = '#fff'; g.lineWidth = s.size; g.lineCap = g.lineJoin = 'round'; }
  function drawStroke(g, s) {
    if (s.clear) return g.clearRect(0, 0, mk.width, mk.height);
    pen(g, s); g.beginPath(); const p = s.pts; g.moveTo(p[0][0], p[0][1]); if (p.length === 1) g.lineTo(p[0][0] + 0.01, p[0][1]); p.slice(1).forEach(q => g.lineTo(q[0], q[1])); g.stroke(); g.globalCompositeOperation = 'source-over';
  }
  function rebuild() { const g = mk.getContext('2d'); g.clearRect(0, 0, mk.width, mk.height); v.strokes.forEach(s => drawStroke(g, s)); drawMask(); coverage(); }
  function coverage() {
    const W = 160, H = Math.max(1, Math.round(160 * mk.height / Math.max(1, mk.width))), t = document.createElement('canvas'); t.width = W; t.height = H;
    const g = t.getContext('2d'); g.drawImage(mk, 0, 0, W, H); const a = g.getImageData(0, 0, W, H).data; let n = 0; for (let i = 3; i < a.length; i += 4) if (a[i] > 127) n++;
    v.cover = Math.round(n / (W * H) * 100); if (n && !v.cover) v.cover = 1;
    const cv = $('#cover'); if (cv) cv.textContent = coverTxt();
    $('#mask').setAttribute('aria-label', v.cover ? `Mask: ${v.cover}% of the picture painted` : 'Mask: nothing painted yet'); tools();
  }
  function tools() { $('#m-undo').disabled = !v.strokes.length; $('#m-clear').disabled = !v.cover; }
  function setTool(t) { v.tool = t; check('#mtool', `[data-t="${t}"]`); $('#ring').classList.toggle('erase', t === 'eraser'); } // dashed ring: the Eraser
  function setBrush(n) { v.brush = Math.max(8, Math.min(256, Math.round(n / 4) * 4)); const r = $('#bsize'); r.value = v.brush; r.style.setProperty('--p', ((v.brush - 8) / 248 * 100) + '%'); $('#bsize-v').textContent = v.brush; }
  function undo() { if (!v.strokes.length) return; v.strokes.pop(); rebuild(); }
  function clearMask() { if (!v.cover) return; v.strokes.push({ clear: true }); rebuild(); }
  const mc = $('#mask'), ringEl = $('#ring');
  const toPic = e => { const r = mc.getBoundingClientRect(); return [(e.clientX - r.left) / r.width * v.pic.w, (e.clientY - r.top) / r.height * v.pic.h]; };
  function ring(e) {
    const r = $('#tray').getBoundingClientRect(), d = v.brush * r.width / v.pic.w, small = d < 8;
    mc.classList.toggle('small', small); ringEl.hidden = small; if (small) return;
    Object.assign(ringEl.style, { width: d + 'px', height: d + 'px', left: (e.clientX - r.left) + 'px', top: (e.clientY - r.top) + 'px' });
  }
  mc.addEventListener('pointerdown', e => {
    if (e.button !== 0 || !v.pic) return; mc.setPointerCapture(e.pointerId);
    const s = { tool: v.tool, size: v.brush, pts: [toPic(e)] }; v.strokes.push(s); v.painting = s; drawStroke(mk.getContext('2d'), s); drawMask();
    if (!$('#mask-err').hidden) { $('#mask-err').hidden = true; X.layoutTray(); }
  });
  mc.addEventListener('pointermove', e => {
    ring(e); const s = v.painting; if (!s) return;
    const p = toPic(e), last = s.pts[s.pts.length - 1], g = mk.getContext('2d'); s.pts.push(p);
    pen(g, s); g.beginPath(); g.moveTo(last[0], last[1]); g.lineTo(p[0], p[1]); g.stroke(); g.globalCompositeOperation = 'source-over'; drawMask();
  });
  const endStroke = () => { if (!v.painting) return; v.painting = null; coverage(); };
  mc.addEventListener('pointerup', endStroke); mc.addEventListener('pointercancel', endStroke);
  mc.addEventListener('pointerleave', () => { ringEl.hidden = true; });
  $('#mtool').addEventListener('click', e => { const b = e.target.closest('[data-t]'); if (b) setTool(b.dataset.t); });
  $('#bsize').addEventListener('input', () => setBrush(+$('#bsize').value));
  $('#m-undo').onclick = undo; $('#m-clear').onclick = clearMask;
  function maskBlob() { // PNG at the picture's size, white = repaint, black = keep
    const c = document.createElement('canvas'); c.width = mk.width; c.height = mk.height;
    const g = c.getContext('2d'); g.fillStyle = '#000'; g.fillRect(0, 0, c.width, c.height); g.drawImage(mk, 0, 0);
    return new Promise(res => c.toBlob(res, 'image/png'));
  }

  // ---------- Compare (hold) ----------
  function compare(on) {
    const b = $('#cmp'); if (!b) return;
    const p = $('#pic'); if (on) p.src = b.dataset.src;
    p.hidden = !on; $('#before-tag').hidden = !on; b.setAttribute('aria-pressed', String(on));
  }
  const th = $('#th-right');
  th.addEventListener('pointerdown', e => { if (e.target.closest('#cmp')) compare(true); });
  ['pointerup', 'pointerleave', 'pointercancel'].forEach(t => th.addEventListener(t, () => { if ($('#cmp')?.getAttribute('aria-pressed') === 'true') compare(false); }));
  th.addEventListener('keydown', e => { if (e.target.id === 'cmp' && (e.key === ' ' || e.key === 'Enter')) { e.preventDefault(); compare(true); } });
  th.addEventListener('keyup', e => { if (e.target.id === 'cmp' && (e.key === ' ' || e.key === 'Enter')) compare(false); });

  // ---------- Touch up menu ----------
  function closeMenu(focus) { const m = $('#tmenu'); if (!m) return; m.remove(); const b = $('#tu-btn'); if (b) { b.setAttribute('aria-expanded', 'false'); if (focus) b.focus(); } }
  function openMenu() {
    const b = $('#tu-btn'), e0 = X.current(); if (!b || !e0 || $('#tmenu')) return;
    const cur = fromEntry(e0), en = outSize('enhance', cur), big = tooBig(cur);
    const noUp = !v.upscaler ? 'Needs the upscaler (Models tab).' : big ? 'Too large: max 2048 pixels per side.' : '';
    const items = [
      ['upscale', 'expand', 'Upscale ×4', `${cur.w * 4} × ${cur.h * 4}. Same picture, bigger.`, noUp],
      ['enhance', 'sparkle', 'Add detail', `${en[0]} × ${en[1]}, with new fine detail.`, noUp],
      v.detector === 'absent' ? null : ['facefix', 'face', 'Fix faces', 'Redraws the faces it finds.', v.detector === 'missing' ? 'Needs the face detector (Models tab).' : ''],
      ['inpaint', 'brush', 'Repaint a part…', 'Paint over what to change.', ''],
      ['img2img', 'refresh', 'Rework…', 'Redraw it from a new prompt.', ''],
      'hr', ['ref', 'image-plus', 'Use as reference', 'Edit it with an instruction.', ''],
    ].filter(Boolean);
    b.parentNode.insertAdjacentHTML('beforeend', `<div class="tmenu" id="tmenu" role="menu" aria-label="Touch up">${items.map(it => it === 'hr' ? '<hr>' : `<button class="titem" type="button" role="menuitem" data-a="${it[0]}" ${it[4] ? 'aria-disabled="true"' : ''}>${icon(it[1])}<span class="t-body">${it[2]}</span><span class="help">${it[4] || it[3]}</span></button>`).join('')}</div>`);
    b.setAttribute('aria-expanded', 'true'); const first = $('#tmenu .titem:not([aria-disabled=true])'); if (first) first.focus();
  }
  function runAt(kind, e0) { // Touch up Add detail / Fix faces: one picture, at once, on this picture
    const p = fromEntry(e0), o = outSize(kind, p), m = X.app.model();
    const req = { kind, model_id: m?.id || '', prompt: kind === 'enhance' ? e0.prompt || '' : '', init_image: p.path, strength: KINDS[kind].def, width: snap(o[0]), height: snap(o[1]), batch: 1 };
    X.submit(req, { frames: 1, w: o[0], h: o[1], model: m, pic: p });
  }
  th.addEventListener('click', e => {
    if (e.target.closest('#tu-btn')) return $('#tmenu') ? closeMenu(true) : openMenu();
    const it = e.target.closest('.titem'); if (!it || it.getAttribute('aria-disabled') === 'true') return;
    const a = it.dataset.a, e0 = X.current(); closeMenu(); if (!e0) return;
    if (a === 'upscale') return X.upscale(e0);
    if (a === 'enhance' || a === 'facefix') return runAt(a, e0);
    if (a === 'ref') { X.addRef(e0); return toast('The picture is in References. Say what to change, then press Develop.'); }
    v.mode = 'from'; v.kind = a; setPic(fromEntry(e0)); if (a === 'inpaint') $('#prompt').value = '';
    render(); X.show(null);
  });
  th.addEventListener('keydown', e => {
    const m = $('#tmenu'); if (!m) return; const its = $$('#tmenu .titem:not([aria-disabled=true])'), i = its.indexOf(document.activeElement);
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); its[(i + (e.key === 'ArrowDown' ? 1 : its.length - 1)) % its.length].focus(); }
    if (e.key === 'Escape') { e.preventDefault(); closeMenu(true); }
    if (e.key === 'Tab') closeMenu();
  });
  document.addEventListener('mousedown', e => { if ($('#tmenu') && !e.target.closest('.tanchor')) closeMenu(); });

  // ---------- keys: Compare (\), painter (B, E, [, ], Ctrl + Z) ----------
  document.addEventListener('keydown', e => {
    const t = e.target, typing = /^(TEXTAREA|SELECT)$/.test(t.tagName) || (t.tagName === 'INPUT' && t.type !== 'range') || t.isContentEditable;
    if (typing || $('#screen-develop').hidden) return;
    if (e.key === '\\' && !e.repeat && $('#cmp')) { e.preventDefault(); return compare(true); }
    if ($('#mbar-wrap').hidden) return;
    if (e.ctrlKey && !e.shiftKey && (e.key === 'z' || e.key === 'Z')) { e.preventDefault(); return undo(); }
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    if (e.key === 'b' || e.key === 'B') setTool('brush'); else if (e.key === 'e' || e.key === 'E') setTool('eraser');
    else if (e.key === '[') setBrush(v.brush - 8); else if (e.key === ']') setBrush(v.brush + 8);
  });
  document.addEventListener('keyup', e => { if (e.key === '\\') compare(false); });

  // ---------- hooks for develop.js ----------
  return {
    idle() { if (!from()) return false; formStage(); return true; }, // render(null) in From a picture
    reset() { closeMenu(); ['#pic', '#mask', '#ring', '#before-tag', '#mbar-wrap'].forEach(s => { $(s).hidden = true; }); },
    extraH() { const w = $('#mbar-wrap'); return w.hidden ? 0 : w.offsetHeight + 12; },
    summary() {
      if (!from()) return false; const k = KINDS[v.kind], parts = [k.short || k.name];
      if (k.frames) parts.push(`${ui.frames} frame${ui.frames > 1 ? 's' : ''}`);
      if (v.pic) { const o = outSize(v.kind, v.pic); parts.push(`${o[0]} × ${o[1]}`); } else parts.push('no picture yet');
      $('#summary').innerHTML = parts.map(esc).join(SEP); return true;
    },
    fitSize() { if (!from() || !v.pic) return null; const o = outSize(v.kind, v.pic); return { w: o[0], h: o[1], refs: 0 }; },
    check() { // before a run: true = v2 job, false = stop (message shown), null = v1 rules
      if (!from()) return null;
      if (!v.pic) { $('#pic-err').hidden = false; $('#pic-drop').focus(); return false; }
      if (KINDS[v.kind].need && !$('#prompt').value.trim()) { $('#prompt-err').hidden = false; $('#prompt').focus(); return false; }
      if (v.kind === 'inpaint' && !v.cover) { $('#mask-err').hidden = false; X.layoutTray(); return false; }
      return true;
    },
    async request(req, info) { // v1 request -> v2 job; false = the mask upload failed (toast shown)
      const k = KINDS[v.kind], o = outSize(v.kind, v.pic);
      Object.assign(req, { kind: v.kind, init_image: v.pic.path, strength: +(v.str[v.kind] ?? k.def), width: snap(o[0]), height: snap(o[1]), refs: [] });
      if (!k.frames) req.batch = 1;
      if (v.kind === 'inpaint') { try { req.mask = (await api('/api/uploads', await maskBlob())).path; } catch (e) { toast(e.message); return false; } }
      Object.assign(info, { frames: req.batch, w: o[0], h: o[1], pic: v.pic });
      return true;
    },
    doneRight(j) { // every done state in v2: Compare (with a starting picture) · Touch up · Show in folder
      const src = j.pic?.url || (j.req?.init_image && X.srcOf(j.req.init_image));
      return `${src ? `<button class="btn sm desk-only" type="button" id="cmp" data-src="${esc(src)}" aria-pressed="false" title="Hold to see the starting picture (or hold \\)">${icon('compare', 'sm')}Compare</button>` : ''}<span class="tanchor desk-only"><button class="btn sm" type="button" id="tu-btn" aria-haspopup="menu" aria-expanded="false">${icon('sparkle', 'sm')}Touch up<svg class="i chev"><use href="#i-chevron"/></svg></button></span>`;
    },
    from, // Enhance prompt: no button in From a picture
    toNew() { if (from()) { v.mode = 'new'; restoreSize(); render(); } },
    async fill(r) { // Library Re-develop: a v2 job comes back as From a picture with its picture
      // phone: "From a picture" stays PC-only, so a v2 job comes back as a new picture with its prompt
      if (!KINDS[r.kind] || PHONE.matches) { if (from()) { v.mode = 'new'; v.sizePrev = null; render(); } return; }
      v.mode = 'from'; v.kind = r.kind; v.str[r.kind] = r.strength;
      const url = X.srcOf(r.init_image), img = new Image();
      if (url) { img.src = url; try { await img.decode(); setPic({ path: r.init_image, url, name: r.init_image.split(/[\\/]/).pop(), w: img.naturalWidth, h: img.naturalHeight, prompt: r.prompt }); } catch { setPic(null); } }
      else setPic(null); // shortcut: a starting picture older than Recent's 60 entries is not found; the slot stays empty
      render(); toForm(); parts();
    },
    parts,
  };
}
