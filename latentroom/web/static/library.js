// Library screen: contact sheets by day, search, Starred, detail panel.
// shortcut: without Smart search /api/history searches prompts only, so the copy says "prompts", not "prompts, seeds, models".
// With Smart search it finds pictures by what is in them, best match first, in one group.
import { $, $$, SEP, esc, icon, api, toast, radio, arrowKeys, modelName, confirmDialog, saveLink, PHONE } from './ui.js';
import { KINDS } from './develop2.js';

const PER = 60;
// The Develop names (Rework, Repaint a part, Add detail, Fix faces), from Develop's own list
const KIND = { txt2img: 'Text to picture', edit: 'Edit with references', upscale: 'Upscale', ...Object.fromEntries(Object.entries(KINDS).map(([k, v]) => [k, v.name])) };
const pid = id => '#' + String(id).padStart(4, '0');
const onPC = p => /^([A-Za-z]:|[\\/]{2})/.test(p || ''); // a phone gets "<key>/<file name>", never a PC path

// Day group of a unix time: title "Today" / "Yesterday" / "Monday" (last 7 days) / "Thu 15 Oct";
// date = the short date for the meta line, "" when the title already is the date.
export function dayOf(unix, now = new Date()) {
  const d = new Date(unix * 1000);
  const date = d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' }).replace(',', '');
  const midnight = x => new Date(x.getFullYear(), x.getMonth(), x.getDate());
  const days = Math.round((midnight(now) - midnight(d)) / 86400000); // round: DST days are 23 or 25 h
  const title = days === 0 ? 'Today' : days === 1 ? 'Yesterday' : days > 1 && days < 7 ? d.toLocaleDateString('en-GB', { weekday: 'long' }) : date;
  return { key: d.toDateString(), title, date: title === date ? '' : date };
}

export function initLibrary(app) {
  const L = { entries: [], done: false, loading: false, filter: 'all', q: '', sel: 0, gen: 0, missing: new Set(), smart: false, idx: null };
  const body = $('#lib-body'), panel = $('#lib-panel');
  const sel = () => L.entries.find(e => e.id === L.sel);

  // ---------- loading (60 per page, newest first) ----------
  async function load(reset) {
    if (reset) { L.gen++; L.done = false; } else if (L.loading || L.done) return;
    const gen = L.gen; L.loading = true; foot();
    // page from what is loaded, so deletes do not skip pictures; duplicates are dropped below
    const page = reset ? 0 : Math.floor(L.entries.length / PER);
    let got, smart;
    try {
      ({ entries: got, smart } = await api(`/api/history?page=${page}&per_page=${PER}${L.filter === 'starred' ? '&starred=1' : ''}${L.q ? '&search=' + encodeURIComponent(L.q) : ''}`));
    } catch (e) {
      if (gen === L.gen) { L.loading = false; foot(); toast(e.message); }
      return;
    }
    if (gen !== L.gen) return; // a newer search or filter started meanwhile
    L.loading = false; L.done = got.length < PER;
    if (reset) { L.smart = !!smart; L.entries = got; head(); render(got, true); } else {
      const have = new Set(L.entries.map(e => e.id)), add = got.filter(e => !have.has(e.id));
      L.entries.push(...add); render(add, false);
    }
    more();
  }
  // new results (search, filter, open): the first one, best match or newest, is selected; the
  // old pick may still be in the list further down, and the panel kept it
  function requery() { L.sel = 0; load(true); }
  function more() { // next page when the last row is within 600px of the bottom (phone: the page scrolls, not the body)
    const left = PHONE.matches ? document.documentElement.scrollHeight - scrollY - innerHeight : body.scrollHeight - body.scrollTop - body.clientHeight;
    if (!L.done && !L.loading && !$('#screen-library').hidden && left < 600) load(false);
  }
  body.addEventListener('scroll', more, { passive: true });
  window.addEventListener('scroll', () => { if (PHONE.matches) more(); }, { passive: true });
  function foot() {
    const f = $('#lib-more'); if (f) f.textContent = L.loading ? 'Loading more…' : L.done ? 'That is everything.' : '';
  }

  // ---------- contact sheet ----------
  const miss = e => L.missing.has(e.id);
  const thumb = e => miss(e) ? `<span class="ph miss" title="Picture file not found">${icon('warn')}</span>` : `<span class="ph"><img src="/api/history/${e.id}/thumb" alt="" loading="lazy"></span>`;
  const cell = e => `<button type="button" class="shot" role="option" data-id="${e.id}" aria-selected="${e.id === L.sel}" tabindex="${e.id === L.sel ? 0 : -1}" aria-label="${pid(e.id)}, ${esc(e.prompt || KIND[e.kind] || '')}${e.starred ? ', starred' : ''}${miss(e) ? ', picture file not found' : ''}">${thumb(e)}<span class="lab">${pid(e.id)}${e.kind === 'upscale' ? SEP + '×4' : ''}${e.starred ? icon('star', 'filled') : ''}</span></button>`;
  // a thumb whose file was moved or deleted (/thumb 404) becomes the missing-picture thumb
  body.addEventListener('error', ev => {
    const c = ev.target.closest?.('.shot'), e = c && L.entries.find(x => x.id === +c.dataset.id); if (!e) return;
    const had = document.activeElement === c; L.missing.add(e.id); c.outerHTML = cell(e);
    if (had) $(`.shot[data-id="${e.id}"]`, body).focus({ preventScroll: true }); // a load error, not a click
  }, true);
  function render(list, reset) {
    if (reset) {
      body.classList.remove('center');
      if (!L.entries.length) return empty();
      if (!sel()) L.sel = L.entries[0].id;
      body.innerHTML = '<p class="lib-more" id="lib-more"></p>';
    }
    const touched = new Set();
    let sec = $$('section', body).at(-1);
    for (const e of list) {
      const d = L.smart ? { key: 'smart', title: 'Best matches', date: '' } : dayOf(e.created); // by meaning: not by day
      if (!sec || sec.dataset.day !== d.key) {
        sec = document.createElement('section');
        sec.dataset.day = d.key; sec.dataset.date = d.date; sec.setAttribute('aria-label', d.title);
        sec.innerHTML = `<h2 class="day-head"><span class="t-title">${d.title}</span><span class="m"></span></h2><div class="sheet" role="listbox" aria-label="${d.title}"></div>`;
        $('#lib-more').before(sec);
      }
      $('.sheet', sec).insertAdjacentHTML('beforeend', cell(e)); touched.add(sec);
    }
    touched.forEach(counts); foot();
    if (reset) renderPanel(sel());
  }
  function counts(sec) { // "Thu 22 Oct · 12 pictures": pictures loaded for that day
    const n = $('.sheet', sec).children.length;
    if (!n) return sec.remove();
    $('.m', sec).innerHTML = (sec.dataset.date ? esc(sec.dataset.date) + SEP : '') + n + (n === 1 ? ' picture' : ' pictures');
  }
  function empty() {
    body.classList.add('center');
    body.innerHTML = L.q
      ? `<div class="empty">${icon('search', 'lg')}<span class="t-body-strong">Nothing matches “${esc(L.q)}”</span><p class="help">${L.smart ? (L.idx?.indexing ? 'Smart search is still indexing your pictures. Try again in a minute.' : 'Smart search looks at what is in each picture.') : 'Search looks in the prompts.'}</p><button class="btn" type="button" data-clear style="margin-top:8px">Clear search</button></div>`
      : L.filter === 'starred'
        ? `<div class="empty">${icon('star', 'lg')}<span class="t-body-strong">No starred pictures</span><p class="help">Star a picture to keep it in this list.</p><button class="btn" type="button" data-all style="margin-top:8px">Show all</button></div>`
        : `<div class="empty">${icon('image', 'lg')}<span class="t-body-strong">No pictures yet</span><p class="help">Every picture you develop is kept here, newest first.</p><a class="btn" href="#develop" style="margin-top:8px">Go to Develop</a></div>`;
    renderPanel(null);
  }
  function select(id, focus) {
    L.sel = id;
    for (const c of $$('.shot', body)) {
      const on = +c.dataset.id === id; c.setAttribute('aria-selected', String(on)); c.tabIndex = on ? 0 : -1;
      if (on && focus) { c.focus(); c.scrollIntoView({ block: 'nearest' }); }
    }
    renderPanel(sel());
  }
  // Takes a picture out of the list (deleted, or unstarred inside Starred); the next older one is selected.
  function drop(e) {
    const i = L.entries.indexOf(e); L.entries.splice(i, 1);
    const c = $(`.shot[data-id="${e.id}"]`, body), sec = c?.closest('section'); c?.remove(); if (sec) counts(sec);
    if (!L.entries.length) return load(true); // more pages may wait; else the empty state
    select((L.entries[i] || L.entries[i - 1]).id, true);
  }
  body.addEventListener('click', e => {
    const c = e.target.closest('.shot'); if (c) return select(+c.dataset.id);
    if (e.target.closest('[data-clear]')) { $('#lib-search').value = ''; L.q = ''; requery(); $('#lib-search').focus(); }
    if (e.target.closest('[data-all]')) setFilter('all');
  });
  body.addEventListener('keydown', e => {
    const c = e.target.closest('.shot'); if (!c) return;
    const cells = $$('.shot', body), i = cells.indexOf(c), cols = getComputedStyle(c.parentNode).gridTemplateColumns.split(' ').length;
    const go = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: cols, ArrowUp: -cols, Home: -i, End: cells.length - 1 - i }[e.key];
    if (go !== undefined) { e.preventDefault(); select(+cells[Math.max(0, Math.min(cells.length - 1, i + go))].dataset.id, true); more(); }
    if (e.key === 'Delete') { e.preventDefault(); askDelete(); }
  });

  // ---------- detail panel ----------
  function renderPanel(e) {
    if (!e) { panel.innerHTML = `<div class="empty">${icon('image', 'lg')}<span class="t-body-strong">Nothing selected</span><p class="help">Pick a picture to see how it was made.</p></div>`; return; }
    const r = e.request || {}, m = app.models[e.model_id], d = dayOf(e.created), miss = L.missing.has(e.id), dis = miss ? ' disabled' : '', up = e.kind === 'upscale';
    const time = new Date(e.created * 1000).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
    const rows = up ? [['Made by', 'Upscale ×4', 'txt'], ['Size', `${e.width} × ${e.height}`]] : [
      ['Model', esc(m ? `${modelName(m)} ${m.quant}` : e.model_id), 'txt'], ['Size', `${e.width} × ${e.height}`],
      ['Steps', r.steps ?? m?.steps ?? '–'], ['Guidance', (r.cfg ?? m?.cfg) != null ? (+(r.cfg ?? m.cfg)).toFixed(1) : '–'], ['Seed', e.seed],
      ['Sampler', esc(`${r.sampler || 'euler'} · ${r.scheduler || 'default'}`)],
    ];
    if (e.kind === 'edit') rows.push(['References', (r.refs || []).length]);
    if (r.loras?.length) rows.push(['LoRAs', r.loras.map(l => `${esc(l.name)} ${(+l.weight).toFixed(2)}`).join('<br>')]);
    const fav = e.starred ? 'Remove star' : 'Star';
    panel.innerHTML = `<div class="lp-scroll scrolly">
      <div class="lp-head"><span class="id">${pid(e.id)}</span><div class="acts">
        <button class="btn ghost icon" type="button" data-star aria-pressed="${e.starred}" aria-label="${fav}" title="${fav}">${icon('star', e.starred ? 'filled' : '')}</button>
        <button class="btn ghost icon" type="button" data-del aria-label="Delete" title="Delete (Del)">${icon('trash')}</button></div></div>
      ${miss ? `<div class="lp-missing"><div class="empty">${icon('warn', 'lg')}<span class="t-body-strong">Picture file not found</span><p class="help">It was moved or deleted outside Latentroom. Its settings are still here.</p></div></div>`
             : `<div class="lp-img"><img src="/api/history/${e.id}/preview" alt="${pid(e.id)}"></div>`}
      <div class="lp-meta">${d.title} ${time}${SEP}${KIND[e.kind] || esc(e.kind)}</div>
      ${up ? '' : `<div class="lp-block"><div class="lrow"><span class="label">Prompt</span><button class="btn ghost sm" type="button" data-copyprompt>${icon('copy', 'sm')}Copy</button></div><p class="lp-prompt">${esc(e.prompt)}</p></div>`}
      <dl class="kv">${rows.map(([k, v, c]) => `<dt>${k}</dt><dd${c ? ` class="${c}"` : ''}>${v}</dd>`).join('')}
        <dt>File</dt><dd class="path">${esc(onPC(e.image) ? e.image : e.image.split(/[\\/]/).pop())}<br><button class="btn ghost sm desk-only" type="button" data-show${dis}>${icon('folder', 'sm')}Show in folder</button></dd></dl>
    </div>
    <div class="lp-foot">
      <button class="btn primary block" type="button" data-reuse${up ? ' disabled title="An upscale has no settings to develop again. Use Upscale ×4 on the original."' : ''}>Re-develop with these settings</button>
      ${miss ? `<button class="btn block only-phone" type="button" disabled>${icon('download', 'sm')}Save to phone</button>` : saveLink(e, 'block')}
      <div class="two desk-only"><button class="btn" type="button" data-ref${dis}>Use as reference</button><button class="btn" type="button" data-up${dis}>Upscale ×4</button></div>
      ${miss ? '<p class="help desk-only">The file is gone, so it cannot be shown in its folder, used as a reference or upscaled.</p><p class="help only-phone">The file is gone, so it cannot be saved to the phone.</p>' : ''}
    </div>`;
    $('.lp-img img', panel)?.addEventListener('error', () => { L.missing.add(e.id); if (L.sel === e.id) renderPanel(e); }, { once: true });
  }
  panel.addEventListener('click', async ev => {
    const t = ev.target.closest('button'), e = sel(); if (!t || t.disabled || !e) return;
    if (t.matches('[data-star]')) return star(e);
    if (t.matches('[data-del]')) return askDelete();
    if (t.matches('[data-copyprompt]')) {
      try { await navigator.clipboard.writeText(e.prompt); toast('Prompt copied.'); } catch { toast('Could not copy the prompt. Select it and press Ctrl + C.'); }
    }
    if (t.matches('[data-show]')) { try { await api('/api/open', { what: 'file', entry_id: e.id }); } catch (err) { toast(err.message); } }
    if (t.matches('[data-reuse]')) {
      try { await app.develop.fill(await api(`/api/history/${e.id}/reuse`), e.seed); } catch (err) { return toast(err.message); }
      location.hash = '#develop';
      toast(`Settings of ${pid(e.id)} are in Develop, seed included. Change what you like, then press Develop.`);
    }
    if (t.matches('[data-ref]')) {
      const n = app.develop.addRef(e); location.hash = '#develop';
      toast(`${pid(e.id)} added to Develop as reference <image${n}>.`);
    }
    if (t.matches('[data-up]') && await app.develop.upscale(e)) toast(`Upscale ×4 of ${pid(e.id)} is in the queue. The bigger picture shows up here as a new frame.`);
  });
  async function star(e) {
    try { e.starred = (await api(`/api/history/${e.id}/star`, { starred: !e.starred })).starred; } catch (err) { return toast(err.message); }
    if (L.filter === 'starred' && !e.starred) return drop(e); // the cell leaves the list at once
    const c = $(`.shot[data-id="${e.id}"]`, body); if (c) c.outerHTML = cell(e);
    renderPanel(e); $('[data-star]', panel).focus();
  }
  async function askDelete() {
    const e = sel(); if (!e) return;
    const folder = (onPC(e.image) && e.image.replace(/[\\/][^\\/]*$/, '')) || 'its folder'; // where it really is: maybe a custom output folder
    const yes = await confirmDialog(`Delete ${pid(e.id)}?`, `The picture file is deleted from ${esc(folder)} too, together with its settings. This cannot be undone.`, `<button class="btn" type="button" data-yes>${icon('trash', 'sm')}Delete</button>`);
    if (!yes) return;
    try { await api(`/api/history/${e.id}`, undefined, 'DELETE'); } catch (err) { $(`.shot[data-id="${e.id}"]`, body)?.focus(); return toast(err.message); }
    drop(e); toast(`${pid(e.id)} deleted.`);
  }

  // ---------- header: search, filter ----------
  function setFilter(f) { L.filter = f; radio('#lib-filter', $(`#lib-filter [data-f="${f}"]`)); requery(); }
  $('#lib-filter').addEventListener('click', e => { const b = e.target.closest('[data-f]'); if (b && b.dataset.f !== L.filter) setFilter(b.dataset.f); });
  arrowKeys('#lib-filter');
  const search = $('#lib-search');
  let st = 0;
  search.addEventListener('input', () => { clearTimeout(st); st = setTimeout(() => { L.q = search.value.trim(); requery(); }, 250); });
  search.addEventListener('keydown', e => { if (e.key === 'Escape' && search.value) { e.stopPropagation(); search.value = ''; clearTimeout(st); L.q = ''; requery(); } });
  document.addEventListener('keydown', e => {
    if (e.key === '/' && !$('#screen-library').hidden && !e.target.closest('input, textarea, select') && $('#dialog').hidden) { e.preventDefault(); search.focus(); }
  });
  // Smart search: placeholder, "Indexing 120 of 800" every 2 s while it indexes, the order
  function head() {
    const i = L.idx;
    search.placeholder = i?.available ? 'Describe the picture you want' : 'Search prompts';
    $('.lib-order').innerHTML = (i?.indexing ? `Indexing ${i.indexed} of ${i.total}${SEP}` : '') + (L.smart ? 'Best match first' : 'Newest first');
  }
  let poll = 0;
  async function status() { // also starts indexing new pictures (server side)
    clearTimeout(poll);
    try { L.idx = await api('/api/search'); } catch { return; } // load() toasts real failures
    head();
    if (L.idx.indexing && !$('#screen-library').hidden) poll = setTimeout(status, 2000);
  }
  // a finished job (e.g. an upscale started here) adds pictures: reload, keep the selection
  window.addEventListener('lr-job-end', () => { if (!$('#screen-library').hidden) { load(true); status(); } });

  return { show() { requery(); status(); } }; // on open, the newest picture is selected
}
