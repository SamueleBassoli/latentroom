// Settings screen: output folder, data folder, video memory limit, 18+ switch, phone pairing,
// diagnostic report, run the setup again, version. Every change is one POST /api/settings with only that key.
import { $, SEP, esc, icon, api, toast, gb, GiB, openDialog, closeDialog, dlgActs, copyDiagnostics, pickFolder, REPO, extLink, USE_RULE } from './ui.js';

const VRAM = [0, 4, 6, 8, 10, 12, 16, 20, 24];
const NO_RELEASE = 'Could not find a published version on GitHub, so Latentroom cannot tell whether a newer one exists.';
const mmss = s => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
const left = code => Math.max(0, Math.ceil(code.expires - Date.now() / 1000));
const sw = (id, on, label, off) => `<button class="switch" type="button" role="switch" aria-checked="${on}" aria-labelledby="${label}" id="${id}"${off ? ' disabled' : ''}></button>`;
export const srow = (name, help, ctrl, extra = '', nameId = '') => `<div class="srow"><div class="sl"><div class="n"${nameId ? ` id="${nameId}"` : ''}>${name}</div><p class="help">${help}</p>${extra}</div><div class="sc">${ctrl}</div></div>`;
export const pathLine = (p, isDefault, what, label) => `<div class="spath"><span class="p" title="${esc(p)}">${esc(p)}</span>${isDefault ? '<span class="badge quiet">default</span>' : ''}<button class="btn ghost icon sm" type="button" data-open="${what}" aria-label="Show the ${label} in File Explorer" title="Show in folder">${icon('folder', 'sm')}</button></div>`;
// more: trusted markup after the escaped text, like an extLink; attrs: e.g. ' role="status"'
export const errLine = (t, more = '', attrs = '') => `<div class="errline"${attrs}>${icon('alert', 'sm')}<span>${esc(t)}${more}</span></div>`;
const plural = (n, one, many) => (n === 1 ? one : many);

export function initSettings(app) {
  const root = $('#screen-settings');
  const S = { inst: null, hw: null, ver: null, ph: null, code: null, outErr: '', timer: 0, upd: null };

  async function load() {
    S.outErr = ''; S.code = null; if (S.upd !== 'busy') S.upd = null; // nothing kept: a new check each time
    // /api/phone runs tailscale.exe: ~0.4 s when Windows has not run it lately. The page does not
    // wait for it; the phone row fills in when it answers
    const ph = api('/api/phone').then(p => { S.ph = p; }, e => { toast(e.message); S.ph ??= { available: false, failed: true, enabled: false, phones: 0, reason: 'Could not check for Tailscale. Press Check again.' }; });
    try {
      [S.inst, S.hw, S.ver] = await Promise.all([api('/api/models'), api('/api/hardware'), api('/api/version'), app.refresh()]);
    } catch (e) { toast(e.message); }
    redraw(); tick();
    await ph; redraw();
  }

  // the answer of the last Check for updates; links come from REPO, never from GitHub's answer
  function updHTML() {
    const u = S.upd;
    if (!u || u === 'busy') return '';
    if (u.error) return errLine(u.error, '', ' role="status" id="upd-line"');
    const text = u.newer ? `Version ${esc(u.latest)} is available. Get the new installer at ${extLink(`${REPO}/releases`)}.`
      : u.latest ? 'You have the latest version.' : NO_RELEASE; // latest null: GitHub answered 404
    return `<p class="help" role="status" id="upd-line">${text}</p>`;
  }
  async function checkUpdate() {
    S.upd = 'busy'; redraw();
    try { S.upd = await api('/api/update'); } catch (e) { S.upd = { error: e.message }; }
    redraw('[data-update]');
  }

  function phoneHTML() {
    const p = S.ph || { enabled: false, phones: 0 }, n = p.phones; // no S.ph yet: switch off and disabled until /api/phone answers
    const help = 'Open Latentroom in the browser of your own phone. It works through Tailscale, a private link between your own devices, never the open internet. This PC and Latentroom must stay on; pictures are still made here, one at a time.';
    let extra = '';
    // no Tailscale: the reason, what to do, where to get it; the links open the system browser
    if (S.ph && !p.available) extra = `<div class="warnline">${icon('warn', 'sm')}<span>${esc(p.reason)}</span></div>
      ${p.failed ? '' : `<ol class="steps"><li>Install Tailscale on this PC and sign in.</li><li>Install it on your phone and sign in with the same account.</li><li>Press Check again.</li></ol>
      <p class="help">Get Tailscale: ${extLink('tailscale.com/download')}</p><p class="help">Step-by-step guide: ${extLink(`${REPO}/blob/main/docs/PHONE.md`)}</p>`}
      <div><button class="btn ghost sm nudge" type="button" data-ph-check>${icon('refresh', 'sm')}Check again</button></div>`;
    else if (!p.enabled && n) extra = `<p class="help">${n} ${plural(n, 'phone is', 'phones are')} paired. ${plural(n, 'It', 'They')} can connect again when you turn this on.</p>`;
    let html = srow('Use from my phone', help, sw('ph-switch', p.enabled, 'sn-phone', !p.available && !p.enabled), extra, 'sn-phone');
    if (!p.enabled) return html;
    html += `<div class="phone"><div class="addr"><span class="label">Address</span><span class="v">${esc(p.address)}</span><button class="btn ghost icon sm" type="button" data-ph-copy aria-label="Copy the address" title="Copy the address">${icon('copy', 'sm')}</button></div>`;
    const c = S.code;
    if (c) {
      const t = left(c), old = t <= 0;
      html += `<div class="pair${old ? ' old' : ''}" role="group" aria-labelledby="pair-title"><div class="qr" role="img" aria-label="QR code with the pairing address">${c.qr_svg}</div><div class="pinfo">
        <span class="t-body-strong" id="pair-title">Pair a phone</span>
        <ol class="steps"><li>On the phone, open the Tailscale app and check that it is on.</li><li>Point the phone camera at this code and open the link.</li><li>Or open the address above in the phone's browser and type this code:</li></ol>
        <div class="code" aria-label="Pairing code ${c.code.split('').join(' ')}">${c.code.slice(0, 3)}<span></span>${c.code.slice(3)}</div>
        ${old ? `<div class="warnline">${icon('warn', 'sm')}<span>This code is too old. Make a new one.</span></div><div class="acts"><button class="btn" type="button" data-ph-pair>${icon('refresh', 'sm')}Make a new code</button><button class="btn ghost" type="button" data-ph-close>Close</button></div>`
          : `<p class="help" id="ph-left">Works once, for ${mmss(t)} more.</p><div class="acts"><button class="btn ghost" type="button" data-ph-pair>${icon('refresh', 'sm')}New code</button><button class="btn ghost" type="button" data-ph-close>Close</button></div>`}
      </div></div>`;
    }
    html += `<div class="pfoot"><span class="help">${n ? `${n} ${plural(n, 'phone', 'phones')} can use Latentroom.` : 'No phone is paired yet.'}</span><div class="acts">${c ? '' : `<button class="btn" type="button" data-ph-pair>${icon('phone', 'sm')}Pair a phone</button>`}${n ? '<button class="btn ghost" type="button" data-ph-forget>Forget all phones</button>' : ''}</div></div></div>`;
    return html;
  }

  function draw() {
    const s = app.settings, inst = S.inst || {}, g = S.hw?.gpu;
    const out = inst.output_dir || '', store = inst.disk_use ? Object.values(inst.disk_use).reduce((a, b) => a + b, 0) : 0;
    const top = g ? g.vram_total / GiB : Infinity, cur = s.max_vram_gb || 0;
    const opts = [...new Set([...VRAM.filter(v => v <= top), cur])].sort((a, b) => a - b)
      .map(v => `<option value="${v}"${v === cur ? ' selected' : ''}>${v ? v + ' GB' : 'Automatic'}</option>`).join('');
    $('#set-body').innerHTML = `
      <section class="sset" aria-labelledby="h-s-pic"><h2 class="t-title" id="h-s-pic">Pictures</h2><div class="slist">
        ${srow('Output folder', 'Every new picture is saved here as a PNG file. Pictures you already made stay where they are.',
          '<button class="btn" type="button" data-out-change>Change…</button>',
          pathLine(out, !s.output_dir, 'output', 'output folder') + (S.outErr ? errLine(S.outErr) : '') + (s.output_dir ? '<div><button class="btn ghost sm nudge" type="button" data-out-default>Use the default folder</button></div>' : ''))}
      </div></section>
      <section class="sset" aria-labelledby="h-s-mod"><h2 class="t-title" id="h-s-mod">Models and memory</h2><div class="slist">
        ${srow('Data folder', `Models, the engine and the Library list live here (${gb(store)} GB now). It was chosen during setup and cannot be moved yet.`, '', pathLine(inst.data_dir || '', !s.data_dir, 'data', 'data folder'))}
        ${srow('Video memory limit', 'Latentroom uses the video memory that is free when a picture starts. Set a limit if you play games or edit video at the same time. Fit labels in Models follow it.',
          `<div class="select-wrap svram"><select class="select" id="set-vram" aria-labelledby="sn-vram">${opts}</select>${icon('chevron')}</div>`, '', 'sn-vram')}
        ${srow('Picture models', 'Download, delete or import models, and see which ones fit this PC.', '<a class="btn" href="#models">Open Models</a>')}
      </div></section>
      <section class="sset" aria-labelledby="h-s-adult"><h2 class="t-title" id="h-s-adult">Uncensored models</h2><div class="slist">
        ${srow('Show uncensored models', 'Community models with no content filter. Only for people 18 or older. Hiding them again does not delete downloaded files.', sw('adult-switch', !!s.adult_ok, 'sn-adult'), '', 'sn-adult')}
      </div></section>
      <section class="sset" aria-labelledby="h-s-phone"><h2 class="t-title" id="h-s-phone">Phone</h2><div class="slist" id="set-phone">${phoneHTML()}</div></section>
      <section class="sset" aria-labelledby="h-s-help"><h2 class="t-title" id="h-s-help">Help</h2><div class="slist">
        ${srow('Diagnostic report', `Copies versions, the graphics card and the last engine errors. Never prompts, pictures or folder names. To get help, open an issue at ${extLink(`${REPO}/issues`)} and paste it there.`, `<button class="btn" type="button" data-diag>${icon('copy', 'sm')}Copy diagnostic report</button>`)}
        ${srow('Setup', 'Checks this PC again and downloads anything that is missing, like the engine. Your pictures and models stay.', '<a class="btn" href="#wizard">Run the setup again</a>')}
        ${srow(`Latentroom ${esc(S.ver?.version)}`, `Latentroom only goes online to download models and the engine, and to check for updates when you press the button. New versions are on the GitHub Releases page at ${extLink(`${REPO}/releases`)} as a new installer.`, `<button class="btn" type="button" data-update${S.upd === 'busy' ? ' disabled' : ''}>${icon('refresh', 'sm')}${S.upd === 'busy' ? 'Checking…' : 'Check for updates'}</button>`, `${updHTML()}<p class="mono-line">engine ${esc(S.ver?.engine_release)}${SEP}fonts Geist and Geist Mono, SIL Open Font License 1.1</p>`)}
      </div></section>`;
  }
  // Re-draw, then put focus back on the same control (or `next`). still: from a timer, not a
  // click, so focus comes back without scrolling the page.
  function redraw(next, still) {
    const a = document.activeElement, mine = a && root.contains(a) && a !== root;
    const same = mine && (a.id ? '#' + a.id : [...a.attributes].filter(t => t.name.startsWith('data-')).map(t => `[${t.name}]`).join(''));
    draw();
    const t = (next && root.querySelector(next)) || (same && root.querySelector(same)), keep = { preventScroll: !!still };
    if (t) t.focus(keep); else if (mine && !a.isConnected) root.querySelector('#set-body button')?.focus(keep);
  }
  async function set(change) {
    await api('/api/settings', change);
    await app.refresh();
  }

  // Pairing card: count down every second; poll /api/phone every 2 s for a new phone.
  let n = 0;
  function tick() {
    clearTimeout(S.timer);
    if (root.hidden || !S.code) return;
    S.timer = setTimeout(async () => {
      const el = $('#ph-left'), t = left(S.code);
      if (t > 0 && el) el.textContent = `Works once, for ${mmss(t)} more.`;
      else if (t <= 0 && el) redraw('', true);
      if (++n % 2 === 0 && t > 0) {
        try {
          const before = S.ph.phones; S.ph = await api('/api/phone');
          if (S.ph.phones > before) { S.code = null; redraw('[data-ph-pair]', true); toast('Your phone is paired.'); }
        } catch { /* next poll */ }
      }
      tick();
    }, 1000);
  }

  function askAdult() {
    openDialog(`<h2 id="dlg-title">Are you 18 or older?</h2><p>Uncensored models have no content filter. Latentroom remembers your answer on this PC.</p><p>${USE_RULE}</p>${dlgActs('No', '<button class="btn" type="button" data-yes>I am 18 or older</button>')}`, async ev => {
      if (ev.target.closest('[data-close]')) closeDialog();
      if (!ev.target.closest('[data-yes]')) return;
      try { await set({ adult_ok: true }); closeDialog(); redraw('#adult-switch'); toast('Uncensored models are shown in Models.'); } catch (e) { closeDialog(); toast(e.message); }
    });
  }
  function askForget() {
    const k = S.ph.phones;
    openDialog(`<h2 id="dlg-title">Forget all phones?</h2><p>${k > 1 ? `The ${k} paired phones lose` : 'The paired phone loses'} access at once. To use a phone again, pair it with a new code. Pictures stay on this PC.</p>${dlgActs('Cancel', '<button class="btn" type="button" data-yes>Forget all phones</button>')}`, async ev => {
      if (ev.target.closest('[data-close]')) closeDialog();
      if (!ev.target.closest('[data-yes]')) return;
      closeDialog(false);
      try { await api('/api/phone/forget', {}); S.ph = await api('/api/phone'); toast('All phones are forgotten.'); } catch (e) { toast(e.message); }
      redraw('[data-ph-pair]');
    });
  }
  async function outDir(path) {
    try {
      await set({ output_dir: path }); S.outErr = '';
      S.inst = await api('/api/models');
      toast(path ? `New pictures go to ${path}.` : 'New pictures go to Pictures\\Latentroom again.');
    } catch (e) { S.outErr = e.message; }
    redraw('[data-out-change]');
  }
  async function phoneSwitch(on) {
    try {
      await set({ phone_enabled: on });
      toast(on ? 'Latentroom can now be opened from paired phones.' : 'Phones can no longer open Latentroom.');
      if (!on) S.code = null;
    } catch (e) { if (e.code !== 'not_available') toast(e.message); } // 409: the row shows the reason
    S.ph = await api('/api/phone').catch(() => S.ph);
    redraw('#ph-switch');
  }

  root.addEventListener('click', async e => {
    const b = e.target.closest('button'); if (!b || b.disabled) return;
    const d = b.dataset;
    try {
      if (d.outChange != null) { const f = await pickFolder(app.settings.output_dir || S.inst?.output_dir); if (f) await outDir(f); }
      else if (d.outDefault != null) await outDir('');
      else if (d.open) await api('/api/open', { what: d.open });
      else if (b.id === 'adult-switch') {
        if (!app.settings.adult_ok) askAdult();
        else { await set({ adult_ok: false }); redraw(); toast('Uncensored models are hidden.'); }
      } else if (b.id === 'ph-switch') await phoneSwitch(!S.ph.enabled);
      else if (d.phCheck != null) {
        S.ph = await api('/api/phone'); redraw(S.ph.available ? '#ph-switch' : '');
        toast(S.ph.available ? 'Tailscale found. You can turn this on now.' : 'Still no Tailscale address on this PC.');
      } else if (d.phCopy != null) { await navigator.clipboard.writeText(S.ph.address); toast('Address copied.'); }
      else if (d.phPair != null) { S.code = await api('/api/phone/code', {}); n = 0; redraw('[data-ph-close]'); tick(); }
      else if (d.phClose != null) { S.code = null; redraw('[data-ph-pair]'); }
      else if (d.phForget != null) askForget();
      else if (d.diag != null) copyDiagnostics();
      else if (d.update != null) await checkUpdate();
    } catch (err) { toast(err.message); }
  });
  root.addEventListener('change', async e => {
    if (e.target.id !== 'set-vram') return;
    const v = +e.target.value;
    try {
      await set({ max_vram_gb: v });
      toast(v ? `Latentroom now uses at most ${v} GB of video memory. Fit labels are checked again.` : 'Latentroom uses whatever video memory is free.');
    } catch (err) { toast(err.message); redraw(); }
  });

  return { show: load, hide: () => { S.code = null; clearTimeout(S.timer); } };
}
