/* DevelopField — the Latentroom "developing" animation. One canvas 2D, no framework, no dependencies.
 *
 * Look: Brownian specks in constant chaos until the photo arrives, then one crossfade to it.
 *   - square specks (fillRect), #EEECE8 on the tray background; as the progress t goes 0 -> 1 their
 *     alpha goes 0.5 -> 0.95 and their size 1.1 -> 1.8 px: the only sign of progress, never a shape
 *   - every frame each speck takes a random step (Gaussian-ish: the sum of three uniforms) of the same
 *     size from prepare() to the end (the mockup's t = 0 step, sigma = 1); nothing pulls it anywhere, and
 *     a speck that leaves one edge comes back at the other, so the field stays an even scatter
 *   - the motion goes on after the last step while the engine decodes the picture: never a still frame
 *   - 3400 specks per 720 x 480 CSS px, scaled by the canvas area, capped
 *   - finish(image): the moving specks crossfade into the photo (700 ms, ease-in-out)
 *   - t is the real progress: step(k, total) events, plus a creep toward the next step between events
 *     (never past it); prepare() = t 0 while the model loads
 *   - reduced motion: no moving specks, one still frame per event; the picture appears as is
 *   - <= 30 fps, one canvas, one draw pass per frame, no DOM per speck, no fonts
 *   - no Pause, no sigma label: the host page shows "12 / 25" and the thin progress line from onChange()
 *
 * API
 *   const f = new DevelopField(canvas);
 *   f.prepare();                 // model loading, no step yet
 *   f.step(k, total);            // call on every REAL engine step event, k = 1..total
 *   f.finish(image);             // final PNG (HTMLImageElement | ImageBitmap | canvas) arrived
 *   f.reset();                   // idle: empty tray
 *   f.setAspect(w / h);          // when the user picks another size
 *   f.setReducedMotion(bool);    // default follows prefers-reduced-motion
 *   f.onChange = s => {};        // {phase, step, total, fraction} — fires on events only, not per frame
 *   f.destroy();
 *   DevelopField.still(canvas, seed)   // a static dim scatter for queued-frame thumbnails
 */
(function (root) {
  'use strict';
  const clamp01 = v => (v < 0 ? 0 : v > 1 ? 1 : v);
  const easeInOut = t => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
  const lerp = (a, b, t) => a + (b - a) * t;
  function rng(seed) { // mulberry32 — deterministic, so a thumbnail keeps its scatter
    let s = seed >>> 0;
    return () => { s = (s + 0x6D2B79F5) >>> 0; let t = s; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  }
  const gauss = () => (Math.random() + Math.random() + Math.random() - 1.5) * 1.15;

  const DEF = {
    density: 3400 / (720 * 480), cap: 10000, // specks per CSS px², at most cap
    fps: 30,             // hard cap
    creep: 0.85,         // how far into the next step the field may move before the event arrives
    followS: 0.5,        // smoothing time constant of the displayed progress (s)
    maxSpeed: 0.35,      // displayed progress may not move faster than 35 % per second
    jitter: 3.6,         // random-step size in CSS px per 1/60 s (the mockup's at t = 0)
    fadeMs: 700,
    bg: '#0D0E10', fg: '#EEECE8',
  };

  class DevelopField {
    constructor(canvas, opts = {}) {
      this.cv = canvas; this.ctx = canvas.getContext('2d');
      this.o = Object.assign({}, DEF, opts);
      this.phase = 'idle'; this.k = 0; this.total = 25;
      this.p = 0; this.clock = 0; this.lastT = 0; this.lastStepAt = 0; this.stepMs = 3000; this.stepsSeen = 0;
      this.img = null; this.phaseAt = 0;
      this.n = 0; this.dpr = 1; this.cssW = 1; this.cssH = 1;
      this.raf = 0; this.onChange = null;
      let rm = false; try { this._mq = matchMedia('(prefers-reduced-motion: reduce)'); rm = this._mq.matches; this._mqH = e => this.setReducedMotion(e.matches); this._mq.addEventListener('change', this._mqH); } catch (e) {}
      this.reduced = rm;
      this._vis = () => { if (document.hidden) this._stop(); else this._kick(); };
      document.addEventListener('visibilitychange', this._vis);
      this._ro = new ResizeObserver(() => { this._resize(); this._drawNow(); });
      this._ro.observe(canvas);
      this._resize(); this._drawNow();
    }

    // ---------- public ----------
    prepare() { this.phase = 'preparing'; this.k = 0; this.p = 0; this.stepsSeen = 0; this.img = null; this._emit(); this._kick(); }
    step(k, total) {
      const now = performance.now();
      if (this.phase !== 'developing') { this.phase = 'developing'; this.stepsSeen = 0; this.img = null; }
      if (total) this.total = total;
      if (this.stepsSeen > 0) this.stepMs = lerp(this.stepMs, now - this.lastStepAt, 0.4); // EMA of real step time
      this.stepsSeen++; this.lastStepAt = now;
      this.k = Math.max(0, Math.min(this.total, k)); this._emit(); this._kick();
    }
    finish(image) {
      this.img = image; this.k = this.total;
      this.phase = 'fade'; this.phaseAt = performance.now(); this._emit(); this._kick();
    }
    reset() { this.phase = 'idle'; this.k = 0; this.p = 0; this.img = null; this._scatter(0, this.n); this._emit(); this._stop(); this._drawNow(); }
    setAspect() {} // nothing depends on it: positions are in 0..1 and the canvas follows its CSS box
    setReducedMotion(v) { this.reduced = !!v; this._stop(); this._drawNow(); this._kick(); }
    destroy() { this._stop(); document.removeEventListener('visibilitychange', this._vis); this._ro.disconnect(); if (this._mq) this._mq.removeEventListener('change', this._mqH); }
    get fraction() { return this.total ? this.k / this.total : 0; }

    static still(canvas, seed = 7) { // queued-frame thumbnail: specks scattered, still and dim
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      const w = canvas.clientWidth || canvas.width, h = canvas.clientHeight || canvas.height;
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
      const c = canvas.getContext('2d'), r = rng(seed), W = canvas.width, H = canvas.height;
      const n = Math.min(DEF.cap, Math.round(w * h * DEF.density * 3)), sz = dpr;
      c.fillStyle = DEF.bg; c.fillRect(0, 0, W, H);
      c.globalAlpha = 0.3; c.fillStyle = DEF.fg;
      for (let i = 0; i < n; i++) c.fillRect(r() * W - sz / 2, r() * H - sz / 2, sz, sz);
      c.globalAlpha = 1;
    }

    // ---------- specks ----------
    _scatter(from, to) { for (let i = from; i < to; i++) { this.px[i] = Math.random(); this.py[i] = Math.random(); } }
    _cover(c, img, w, h, alpha) {
      const iw = img.naturalWidth || img.width, ih = img.naturalHeight || img.height, s = Math.max(w / iw, h / ih);
      c.globalAlpha = alpha; c.drawImage(img, (w - iw * s) / 2, (h - ih * s) / 2, iw * s, ih * s); c.globalAlpha = 1;
    }
    _resize() { // canvas pixels follow its CSS size, the speck count its area (positions are in 0..1)
      const dpr = this.dpr = Math.min(2, window.devicePixelRatio || 1);
      this.cssW = Math.max(1, this.cv.clientWidth); this.cssH = Math.max(1, this.cv.clientHeight);
      const w = Math.round(this.cssW * dpr), h = Math.round(this.cssH * dpr);
      if (this.cv.width !== w || this.cv.height !== h) { this.cv.width = w; this.cv.height = h; }
      const n = Math.min(this.o.cap, Math.round(this.cssW * this.cssH * this.o.density)), had = this.px ? this.px.length : 0;
      if (n > had) { // grow: the specks already moving keep going, the new ones start scattered
        const grow = a => { const b = new Float32Array(n); if (a) b.set(a); return b; };
        this.px = grow(this.px); this.py = grow(this.py);
        this._scatter(had, n);
      }
      this.n = n;
    }

    // ---------- loop ----------
    _animating() { return !this.reduced && (this.phase === 'preparing' || this.phase === 'developing' || this.phase === 'fade'); }
    _kick() {
      if (this.reduced) { this._staticFrame(); return; }
      if (this.raf || document.hidden || !this._animating()) { if (!this._animating()) this._drawNow(); return; }
      this.lastT = performance.now();
      const tick = now => {
        this.raf = 0;
        if (document.hidden) return;
        const minDt = 1000 / this.o.fps - 1;
        if (now - this.lastT >= minDt) {
          const dt = Math.min(0.1, (now - this.lastT) / 1000); // internal clock: no jump after a hidden tab
          this.lastT = now; this.clock += dt; this._update(dt, now); this._draw(now);
          this.frames = (this.frames || 0) + 1;
        }
        if (this._animating()) this.raf = requestAnimationFrame(tick);
      };
      this.raf = requestAnimationFrame(tick);
    }
    _stop() { if (this.raf) cancelAnimationFrame(this.raf); this.raf = 0; }
    _emit() { if (this.onChange) this.onChange({ phase: this.phase === 'fade' ? 'morph' : this.phase, step: this.k, total: this.total, fraction: this.fraction }); }

    _t() { return this.phase === 'developing' || this.phase === 'fade' ? this.p : 0; } // the progress the look follows
    _update(dt, now) {
      if (this.phase === 'developing') {
        const creep = this.k < this.total ? Math.min(this.o.creep, (now - this.lastStepAt) / Math.max(300, this.stepMs) * this.o.creep) : 0;
        const goal = Math.min(1, (this.k + creep) / this.total);
        let dp = (goal - this.p) * (1 - Math.exp(-dt / this.o.followS));
        dp = Math.max(-this.o.maxSpeed * dt, Math.min(this.o.maxSpeed * dt, dp));
        this.p = clamp01(this.p + dp);
      } else if (this.phase === 'fade' && now - this.phaseAt >= this.o.fadeMs) {
        this.phase = 'done'; this._emit();
      }
      if (this._animating()) this._sim(dt * 60);
    }
    _sim(f) { // one Brownian step per speck, no pull, wrapped at the edges; f = frame time in 1/60 s
      const amp = this.o.jitter * Math.sqrt(f), ax = amp / this.cssW, ay = amp / this.cssH, { px, py } = this;
      for (let i = 0; i < this.n; i++) {
        const x = px[i] + gauss() * ax, y = py[i] + gauss() * ay;
        px[i] = x - Math.floor(x); py[i] = y - Math.floor(y);
      }
    }

    _drawNow() { if (this.reduced) this._staticFrame(); else this._draw(performance.now()); }

    _staticFrame() { // reduced motion: the specks hold still where they are, the picture appears as is
      if (this.phase === 'fade') { this.phase = 'done'; this._emit(); }
      this._draw(performance.now(), this.phase === 'developing' ? this.fraction : 0);
    }

    _draw(now, t = this._t()) {
      const c = this.ctx, W = this.cv.width, H = this.cv.height, o = this.o;
      c.globalAlpha = 1; c.fillStyle = o.bg; c.fillRect(0, 0, W, H);
      if (this.phase === 'done' && this.img) { this._cover(c, this.img, W, H, 1); return; }
      const sz = (1.1 + 0.7 * t) * this.dpr, h = sz / 2, { px, py } = this; // one pass: one colour, one alpha
      c.globalAlpha = (0.5 + 0.45 * t) * (this.phase === 'idle' ? 0.45 : 1); c.fillStyle = o.fg;
      for (let i = 0; i < this.n; i++) c.fillRect(px[i] * W - h, py[i] * H - h, sz, sz);
      c.globalAlpha = 1;
      // crossfade: the photo over the still-moving specks, so they fade out as it fades in
      if (this.phase === 'fade' && this.img) this._cover(c, this.img, W, H, easeInOut(clamp01((now - this.phaseAt) / o.fadeMs)));
    }
  }
  root.DevelopField = DevelopField;
})(typeof window !== 'undefined' ? window : globalThis);
