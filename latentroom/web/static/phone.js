// Phone layout glue. The look is css/phone.css.
// - Develop: "More settings" box, the model line under Develop, Develop scrolls the tray into view.
// - Library: a tapped picture opens full screen under a back bar; Back, Esc and the bar close it.
// - Pairing: what the phone listener shows a phone without the pairing cookie.
// shortcut: a phone that was forgotten on the PC while open sees "Pair this phone first." toasts until it reloads.
import { $, $$, esc, icon, api, modelLabel, PHONE } from './ui.js';
import { loader } from './loader-mark.js';

const calm = () => matchMedia('(prefers-reduced-motion: reduce)').matches;

export function initPhone(app) {
  // ---------- 16.3 More settings: on phone the .ph-more fields move into one box right after the button, so the
  // DOM order (focus order) is what is seen; on a desktop width they go back to their own places. Never saved.
  const btn = $('#ph-more-btn'), box = document.createElement('div');
  box.className = 'ph-more-box'; box.id = 'ph-more'; box.hidden = true; btn.after(box);
  const fields = $$('.col-left .ph-more').map(el => { const mark = document.createComment('ph-more'); el.before(mark); return { el, mark }; });
  function place() {
    if (PHONE.matches) { fields.forEach(f => box.appendChild(f.el)); box.hidden = btn.getAttribute('aria-expanded') !== 'true'; }
    else { fields.forEach(f => f.mark.after(f.el)); box.hidden = false; }
  }
  btn.addEventListener('click', () => {
    const open = btn.getAttribute('aria-expanded') !== 'true';
    btn.setAttribute('aria-expanded', String(open)); box.hidden = !open;
  });
  place();
  // the model line: "Balanced model" (same words as the Models toasts)
  const fmodel = () => { const m = app.model(); $('#fmodel').textContent = m ? modelLabel(m) : ''; };
  window.addEventListener('lr-model', fmodel);
  // the tray is above the settings on phone: pressing Develop brings it into view
  $('#go').addEventListener('click', () => { if (PHONE.matches) $('.stage').scrollIntoView({ behavior: calm() ? 'auto' : 'smooth', block: 'start' }); });

  // ---------- 16.4 Library: full-screen picture ----------
  const lib = $('#screen-library'), panel = $('#lib-panel');
  const back = document.createElement('div'); back.className = 'ph-back';
  back.innerHTML = `<button class="btn ghost" type="button" data-ph-back>${icon('chevron', 'back')}Library</button><span class="t-mono-sm" id="ph-back-id"></span>`;
  lib.appendChild(back);
  const backId = () => { $('#ph-back-id').textContent = $('.lp-head .id', panel)?.textContent || ''; };
  new MutationObserver(backId).observe(panel, { childList: true }); // library.js redraws the panel (delete -> next picture)
  // opening pushes one history entry, so the phone's Back gesture closes the picture
  const openPic = () => { lib.classList.add('ph-open'); history.pushState({ phOpen: 1 }, ''); backId(); $('button', back).focus(); };
  const shut = () => { lib.classList.remove('ph-open'); $('#lib-body .shot[aria-selected="true"]')?.focus(); };
  const closePic = () => { if (history.state?.phOpen) history.back(); else shut(); };
  window.addEventListener('popstate', () => { if (lib.classList.contains('ph-open')) shut(); });
  $('#lib-body').addEventListener('click', e => { if (PHONE.matches && e.target.closest('.shot')) openPic(); }); // after library.js selected it
  back.addEventListener('click', e => { if (e.target.closest('[data-ph-back]')) closePic(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && lib.classList.contains('ph-open') && $('#dialog').hidden) closePic(); });
  window.addEventListener('hashchange', () => lib.classList.remove('ph-open'));
  PHONE.addEventListener('change', () => { place(); if (!PHONE.matches) lib.classList.remove('ph-open'); });
}

// ---------- 16.6 Pairing screen ----------
const SHORT = 'Type the 6 digits the PC shows.'; // checked on the phone, nothing is sent
export function initPair(done) {
  const el = $('#screen-pair');
  function draw(state, err = '', val = '') { // ask | pairing | short | wrong | too-many
    const busy = state === 'pairing';
    el.innerHTML = `<div class="prob"><header class="whead"><span class="wlogo"><img src="/logo-horizontal.svg" alt="Latentroom" height="20"></span></header>
      <section class="pbody" aria-labelledby="pair-title" data-state="${state}"><span class="pic">${icon('phone')}</span>
      <h1 class="t-title-page" id="pair-title" tabindex="-1">${busy ? 'Pairing this phone…' : 'Pair this phone'}</h1>
      ${busy ? `<p class="busy" role="status">${loader()}Checking the code with your PC.</p>` : `
      <p class="ptext">Latentroom runs on your PC. To use it from this phone, pair it once with a code from the PC.</p>
      <ol class="steps"><li>On the PC, open Latentroom, then Settings.</li><li>Under Use from my phone, press Pair a phone.</li><li>Scan the QR code with this phone's camera, or type the 6 digits here.</li></ol>
      <form class="codein" novalidate><label class="label" for="pair-code">Pairing code</label>
        <input class="input" id="pair-code" type="text" name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="000000" value="${esc(val)}"${err ? ' aria-invalid="true" aria-describedby="pair-err"' : ''}>
        ${err ? `<div class="err-line" id="pair-err" role="alert">${icon('alert', 'sm')}<span>${esc(err)}</span></div>` : ''}
        <button class="btn primary block" type="submit">Pair this phone</button></form>`}
      </section><footer class="prfoot help">Pictures are made and kept on your PC.</footer></div>`;
    $(err ? '#pair-code' : '#pair-title').focus();
  }
  async function send(code) {
    draw('pairing');
    try { await api('/api/phone/pair', { code }); } catch (e) { // 400 WRONG_CODE, 429 TOO_MANY: the server's sentence as is
      return draw(e.status === 429 ? 'too-many' : 'wrong', e.message, code);
    }
    done(); // the lr_phone cookie is set
  }
  el.addEventListener('input', e => { if (e.target.id === 'pair-code') e.target.value = e.target.value.replace(/\D/g, '').slice(0, 6); });
  el.addEventListener('submit', e => {
    e.preventDefault(); const v = $('#pair-code').value;
    if (v.length !== 6) return draw('short', SHORT, v);
    send(v);
  });
  const screen = {
    show() { // the QR opens /#code=<6 digits>: read it, take it out of the address bar, send it at once
      const m = /^#code=(\d{6})$/.exec(location.hash);
      if (!m) return draw('ask');
      history.replaceState(null, '', location.pathname + location.search);
      send(m[1]);
    },
  };
  // a QR scanned while this page is open is the same page with a new #code=: no reload, so no route() to call show()
  window.addEventListener('hashchange', () => { if (!el.hidden && /^#code=\d{6}$/.test(location.hash)) screen.show(); });
  return screen;
}
