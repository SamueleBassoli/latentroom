// Loader mark: specks scatter as noise, settle into the Latentroom mark, hold, scatter again.
// <lr-loader size="16"> (16-160 px), via loader(size). Decorative: always aria-hidden, the text next
// to it says the state. Colour: the element's CSS color. One requestAnimationFrame loop for every mark,
// <= 30 fps, idle while no mark is in the page or the tab is hidden; a mark not shown (display: none) is not
// drawn, and with none shown the loop only looks twice a second. Reduced motion: the settled mark, still.
// A mark added mid-cycle (pages re-render their header every poll) draws the current frame at once.
const SETTLE = 1200, HOLD = 1800, CYCLE = 2800, FPS = 30; // ms: settle, hold the mark until 1800, scatter until 2800
const RM = matchMedia('(prefers-reduced-motion: reduce)');
const live = new Set();
const ease = x => (x = Math.max(0, Math.min(1, x)), x * x * (3 - 2 * x));

// the logo's mark, copied from logo-horizontal.svg (first <g>): x, y, side, opacity, in the svg's units
const SQUARES = [
  [8, 7, 1], [6, 9, 1], [0, 10, 2], [3, 10, 2], [9, 10, 2], [12, 10, 2],
  [0, 13, 2], [3, 13, 2], [6, 13, 2], [9, 13, 2], [12, 13, 2],
  [3, 16, 2], [6, 16, 2], [9, 16, 2], [10, 5, 1, 0.55],
];
// one speck per unit cell (a 2 x 2 square is 4 specks), as centres in a 16-unit box: the mark spans
// x 0-14 and y 5-18, so it moves by (+1, -3.5) to sit in the middle; 16 px = 1 device pixel per unit
const BOX = 16, MX = [], MY = [], MA = [];
for (const [x, y, side, o = 1] of SQUARES)
  for (let j = 0; j < side; j++) for (let i = 0; i < side; i++) { MX.push(x + i + 1.5); MY.push(y + j - 3); MA.push(o); }
const N = MX.length, xs = new Float32Array(N), ys = new Float32Array(N), ss = new Float32Array(N);
function noise() { // where the specks start: a disc a bit wider than the mark, specks smaller than a cell
  const n = { x: new Float32Array(N), y: new Float32Array(N), s: new Float32Array(N) };
  for (let i = 0; i < N; i++) {
    const d = 7.5 * Math.sqrt(Math.random()), a = Math.random() * 2 * Math.PI;
    n.x[i] = BOX / 2 + Math.cos(a) * d; n.y[i] = BOX / 2 + Math.sin(a) * d; n.s[i] = 0.25 + Math.random() * 0.75;
  }
  return n;
}
let nA = noise(), nB = noise(), clock = 0, prev = 0, last = 0, raf = 0;

function place(a, src) { // a = 1: noise, 0: the mark; positions are shared by every mark on the page
  for (let i = 0; i < N; i++) {
    xs[i] = MX[i] + (src.x[i] - MX[i]) * a + (Math.random() - 0.5) * 0.25 * a;
    ys[i] = MY[i] + (src.y[i] - MY[i]) * a + (Math.random() - 0.5) * 0.25 * a;
    ss[i] = 1 + (src.s[i] - 1) * a;
  }
}
function draw(el) { // square specks, edges on device pixels and >= 1 px: the settled mark is crisp, no blur
  const cv = el.cv, c = cv.getContext('2d'), px = v => Math.round(v * cv.width / BOX);
  c.clearRect(0, 0, cv.width, cv.height);
  c.fillStyle = getComputedStyle(el).color;
  for (let i = 0; i < N; i++) {
    const h = ss[i] / 2, x = px(xs[i] - h), y = px(ys[i] - h);
    c.globalAlpha = MA[i];
    c.fillRect(x, y, Math.max(1, px(xs[i] + h) - x), Math.max(1, px(ys[i] + h) - y));
  }
}
place(1, nA); // the cycle starts as noise
function tick(now) {
  raf = 0;
  if (!live.size || document.hidden || RM.matches) return;
  const shown = [...live].filter(el => el.getClientRects().length); // display: none, or inside it: not drawn
  if (!shown.length) { raf = setTimeout(() => { raf = 0; kick(); }, 500); return; } // none shown: look twice a second
  raf = requestAnimationFrame(tick);
  if (now - last < 1000 / FPS - 1) return;
  clock += last ? Math.min(50, now - last) : 16; last = now; // capped: no jump after a hidden tab
  const p = clock % CYCLE;
  if (p < prev) { nA = nB; nB = noise(); }
  prev = p;
  if (p < SETTLE) place(1 - ease(p / SETTLE), nA);
  else if (p < HOLD) place(0, nA);
  else place(ease((p - HOLD) / (CYCLE - HOLD)), nB);
  shown.forEach(draw);
}
function kick() {
  if (RM.matches) { place(0, nA); live.forEach(draw); }
  else if (!raf && live.size && !document.hidden) { last = 0; raf = requestAnimationFrame(tick); }
}
document.addEventListener('visibilitychange', kick);
RM.addEventListener('change', kick);

customElements.define('lr-loader', class extends HTMLElement {
  connectedCallback() {
    this.setAttribute('aria-hidden', 'true');
    this.size = Math.min(160, Math.max(16, +this.getAttribute('size') || 16));
    const d = Math.min(3, window.devicePixelRatio || 1);
    this.cv ??= this.appendChild(document.createElement('canvas'));
    this.cv.width = this.cv.height = Math.round(this.size * d);
    this.cv.style.width = this.cv.style.height = this.size + 'px';
    live.add(this);
    if (RM.matches) place(0, nA);
    draw(this); kick();
  }
  disconnectedCallback() { live.delete(this); } // the loop stops by itself when none is left
});

export const loader = (size = 16) => `<lr-loader size="${size}"></lr-loader>`;
