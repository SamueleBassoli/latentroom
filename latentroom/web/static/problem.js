// Full-window hardware problems: no NVIDIA card, driver too old, check failed.
// Shown instead of every screen (top bar included) while GET /api/hardware status is not "ok".
// shortcut: no full-window "lost connection" screen; ui.js shows a toast that stays instead.
import { $, esc, icon, api, toast, gb, copyDiagnostics, REPO, extLink } from './ui.js';

const DRV = extLink('nvidia.com/drivers');
const HELP_DIAG = `If it keeps happening, copy the diagnostic report, then open an issue at ${extLink(`${REPO}/issues`)} and paste it there.`;
const PROBLEMS = {
  no_nvidia: { title: 'Latentroom needs an NVIDIA graphics card', help: `If this PC does have an NVIDIA card, install its driver from ${DRV}, then press Check again.`,
    facts: h => [['Graphics card', 'No NVIDIA card found', 'danger'], ['Memory', `${gb(h.ram_total)} GB`, '']] },
  driver_too_old: { title: 'Update the graphics driver', help: `Get it from ${DRV}, or with the NVIDIA app.`,
    facts: h => [['Graphics card', h.gpu ? `${h.gpu.name}, ${gb(h.gpu.vram_total)} GB` : 'NVIDIA', 'ok'], ['Driver', `${h.driver || '?'}, too old`, 'warn']] },
  error: { title: 'Latentroom could not check the graphics card', help: HELP_DIAG, facts: () => [] },
};

export function initProblem(app, onFixed) {
  const root = $('#screen-problem');
  let version = '', checking = false;

  function draw() {
    const h = app.hw, p = PROBLEMS[h.status] || PROBLEMS.error;
    const facts = p.facts(h).map(([n, v, d]) => `<div class="srow"><div class="sl"><div class="n">${n}</div></div><div class="sc wval">${d ? `<span class="sdot ${d}"></span>` : ''}<span>${esc(v)}</span></div></div>`).join('');
    root.innerHTML = `<div class="prob"><header class="whead"><span class="wlogo"><img src="/logo-horizontal.svg" alt="Latentroom" height="20"></span></header>
      <section class="pbody" role="alert" aria-labelledby="p-title"><span class="pic">${icon('alert')}</span><h1 class="t-title-page" id="p-title" tabindex="-1">${p.title}</h1>
        <p class="ptext">${esc(h.message)}</p>${facts ? `<div class="slist pfacts">${facts}</div>` : ''}<p class="help">${p.help}</p>
        <div class="acts"><button class="btn primary md" type="button" data-p-check${checking ? ' disabled' : ''}>${checking ? 'Checking…' : 'Check again'}</button><button class="btn ghost" type="button" data-p-diag>${icon('copy', 'sm')}Copy diagnostic report</button></div></section>
      <footer class="prfoot help">${version ? `Latentroom ${esc(version)}` : ''}</footer></div>`;
  }
  async function check() {
    const before = app.hw.status;
    checking = true; draw(); $('[data-p-check]').focus();
    try { app.hw = await api('/api/hardware'); } catch (e) { toast(e.message); }
    checking = false;
    if (app.hw.status === 'ok') return onFixed(); // on to the wizard or Develop
    draw(); $(app.hw.status === before ? '[data-p-check]' : '#p-title').focus();
    if (app.hw.status === before) toast('Same result: nothing changed yet.');
  }
  root.addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b || b.disabled) return;
    if (b.dataset.pCheck != null) check();
    if (b.dataset.pDiag != null) copyDiagnostics();
  });

  return {
    async show() {
      draw(); $('#p-title').focus();
      try { version = (await api('/api/version')).version; draw(); $('#p-title').focus(); } catch { /* the foot stays empty */ }
    },
  };
}
