"""Browser tests: the real pages in headless Chrome against the real server, the fake
engine and the stub catalogue. Skipped when Chrome is not installed.
LATENTROOM_SHOTS=<folder> also saves a screenshot of every check there."""

import io
import json
import math
import os
import sys
import threading
import time
import urllib.error
import warnings
from dataclasses import replace
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import cdp
import fake_catalogue
import pytest
from cdp import Chrome, find_chrome
from PIL import Image

from latentroom import REPO, __version__, settings
from latentroom.engine import (
    catalogue,
    downloads,
    hardware,
    history,
    install,
    paths,
    prompt_rewrite,
    search,
)
from latentroom.engine.jobs import JobRequest
from latentroom.web import phone, server, update

CHROME = find_chrome()
pytestmark = pytest.mark.skipif(not CHROME, reason="Chrome is not installed")
DESK, PHONE = (1280, 800), (390, 844)
SHOTS = os.environ.get("LATENTROOM_SHOTS")


@pytest.fixture
def start(monkeypatch):
    """start(fresh=, hw=, phone_on=) -> the real server on the stub catalogue + fake engine,
    with one picture in history. Stopped after the test."""
    started = []

    def go(fresh=False, hw="ok", phone_on=False, size=1024):
        fake_catalogue.install_stub(monkeypatch)
        m = fake_catalogue.MODELS
        pairs = [
            catalogue.ModelPair("q4", "Balanced", m["std-q4"], m["unc-q4"]),
            catalogue.ModelPair("turbo-q4", "Fast", m["turbo-q4"], None, True),
        ]
        monkeypatch.setattr(catalogue, "load", lambda: pairs)

        def runs(w, h):  # size < 1024: a small or busy card, nothing runs above it
            return max(w, h) <= size

        def recommend(p, hw, width=1024, height=1024):
            return p[0] if runs(width, height) else None

        def fit(model, hw, width=1024, height=1024, refs=0, lora_bytes=0):
            return catalogue.Fit("full_gpu" if runs(width, height) else "too_big", 0, 0, 1.0, "t")

        monkeypatch.setattr(catalogue, "recommend", recommend)
        if size < 1024:
            monkeypatch.setattr(catalogue, "fit", fit)
        if hw != "ok":
            bad = replace(fake_catalogue.HW, status=hw, message="No NVIDIA card.", gpu=None)
            monkeypatch.setattr(hardware, "detect", lambda: bad)
        if phone_on:  # 127.0.0.2 stands in for the Tailscale address; a fresh port, see test_phone
            monkeypatch.setattr(phone, "tailscale_ip", lambda: ("127.0.0.2", ""))
            monkeypatch.setattr(phone, "PORT", 0)
        s = settings.load()
        s.model_id, s.first_run_done, s.phone_enabled = "std-q4", not fresh, phone_on
        settings.save(s)
        pic = paths.default_output_dir() / "latentroom-test.png"
        Image.new("RGB", (96, 64), "gray").save(pic)
        history.add(JobRequest(model_id="std-q4", prompt="a lighthouse at dusk"), pic, 1)
        started.append(server.start())
        return started[-1]

    yield go
    for s in started:
        s.stop()


@pytest.fixture
def chrome(tmp_path):
    c = Chrome(CHROME, tmp_path / "chrome")
    yield c
    c.close()


def shot(c: Chrome, name: str) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        c.screenshot(Path(SHOTS) / f"{name}.png")


def visible(screen: str) -> str:
    """JS: the screen is the one shown and has drawn its content. The HTML has text before
    start-up has run: route() setting body.dataset.screen marks it done."""
    return f"document.body.dataset.screen === '{screen}' && (m => m && !m.hidden && m.textContent.trim().length > 0)(document.querySelector('#screen-{screen}'))"  # noqa: E501


# JS for a failed wait on a screen: the screen shown, the toast, and the requests that finished
# (path, ms after navigation start); one missing from the list was still pending
LOADED = """[document.body.dataset.screen, document.querySelector('#toast')?.textContent,
  performance.getEntriesByType('resource').map(e => new URL(e.name).pathname + ' '
    + Math.round(e.responseEnd)).join(', ')].join(' | ')"""


# (screen, server setup, URL hash). `pair` = the phone address without a pairing cookie.
SCREENS = [
    ("develop", {}, ""),
    ("library", {}, "#library"),
    ("models", {}, "#models"),
    ("settings", {}, "#settings"),
    ("wizard", {"fresh": True}, ""),
    ("problem", {"hw": "no_nvidia"}, ""),
    ("pair", {"phone_on": True}, ""),
]
PC_ONLY = {"models", "settings", "wizard"}  # not on the phone


@pytest.mark.parametrize(
    "screen, setup, hash_, size",
    [
        pytest.param(name, setup, h, size, id=f"{name}-{size[0]}x{size[1]}")
        for size in (DESK, PHONE)
        for name, setup, h in SCREENS
        if size == DESK or name not in PC_ONLY
    ],
)
def test_every_screen_loads_without_console_errors(start, chrome, screen, setup, hash_, size):
    srv = start(**setup)
    url = srv.url + hash_
    if screen == "pair":
        url = "http://%s:%d/" % srv._phone.server_address[:2]
    chrome.open(url, *size)
    chrome.wait(visible(screen), show=LOADED)
    chrome.wait("[...document.images].every(i => i.complete)")
    chrome.eval("new Promise(r => setTimeout(() => r(1), 500))")  # late fetches and their errors
    shot(chrome, f"t-{screen}-{size[0]}x{size[1]}")
    assert chrome.eval("document.querySelector('#toast.show')?.textContent || ''") == ""
    # an unpaired phone learns it is unpaired from this 403; Chrome logs it
    forbidden = "log: Failed to load resource: the server responded with a status of 403"
    forbidden += " (Forbidden)"
    assert chrome.errors() == ([f"{forbidden} {url}api/version"] if screen == "pair" else [])


# where each screen scrolls when its content is taller than the window. Develop and Library
# keep a fixed layout and scroll inside it (the left column, the picture grid).
SCROLLS = {"develop": ".col-left .scroll", "library": "#lib-body"}
SMALLEST = (960, 640)  # app.css: the .app min-width and min-height


@pytest.mark.parametrize(("screen", "setup", "hash_"), SCREENS, ids=[s[0] for s in SCREENS])
def test_every_screen_scrolls_when_taller_than_the_window(start, chrome, screen, setup, hash_):
    srv = start(**setup)
    url = srv.url + hash_
    if screen == "pair":
        url = "http://%s:%d/" % srv._phone.server_address[:2]
    chrome.open(url, *SMALLEST)
    chrome.wait(visible(screen), show=LOADED)
    box = SCROLLS.get(screen, f"#screen-{screen}")
    tall = """(b => { b.insertAdjacentHTML('beforeend', '<div style="height: 3000px"></div>');
      b.scrollTop = 1e6; return b.scrollTop; })(document.querySelector('%s'))"""
    assert chrome.eval(tall % box) > 0


# JS: every element whose scrollbar is not the one thin dark scrollbar; hidden ones
# count too, so a popover or a strip that opens later is covered
OFF_SCROLLBAR = """(() => {
  const p = document.body.appendChild(document.createElement('i'));
  p.style.color = 'var(--color-border-strong)';
  const want = getComputedStyle(p).color; p.remove();
  return [want, [...document.querySelectorAll('*')].filter(el => {
    const cs = getComputedStyle(el);
    return cs.scrollbarWidth !== 'thin' || !cs.scrollbarColor.startsWith(want + ' ');
  }).map(el => `${el.tagName.toLowerCase()}#${el.id}.${el.className}`)];
})()"""


@pytest.mark.parametrize("hash_", ["", "#library", "#models", "#settings"])
def test_every_box_has_the_thin_dark_scrollbar(start, chrome, hash_):
    srv = start()
    chrome.open(srv.url + hash_, *DESK)
    chrome.wait(visible(hash_[1:] or "develop"), show=LOADED)
    want, off = chrome.eval(OFF_SCROLLBAR)
    assert want.startswith("rgb") and off == []


def test_settings_phone_has_no_v2_badge(start, chrome):
    chrome.open(start().url + "#settings", *DESK)  # Phone works in v1
    chrome.wait("!!document.querySelector('#h-s-phone')", show=LOADED)
    assert chrome.eval("document.querySelector('#h-s-phone').textContent") == "Phone"


def test_open_loads_again_once_on_the_windows_buffer_error_only(chrome, tmp_path, monkeypatch):
    # net::ERR_NO_BUFFER_SPACE cannot be made on purpose, so a missing stylesheet (404)
    # stands in for it; every other failed load must still reach errors()
    for name in ("a", "b"):
        page = "<link rel=icon href='data:,'><link rel=stylesheet href=gone.css>"
        (tmp_path / f"{name}.html").write_text(page)
    files = partial(SimpleHTTPRequestHandler, directory=tmp_path)
    web = ThreadingHTTPServer(("127.0.0.1", 0), files)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{web.server_port}/"
    try:
        with warnings.catch_warnings(record=True) as warned:
            warnings.simplefilter("always")
            chrome.open(base + "a.html", *DESK)
            assert len(chrome.errors()) == 1 and warned == []  # another error: not loaded again
            monkeypatch.setattr(cdp, "RETRY", "status of 404")
            chrome.open(base + "b.html", *DESK)
            assert len(warned) == 1  # loaded again, once
    finally:
        web.shutdown()
        web.server_close()
    errors = chrome.errors()  # a.html's, then only the second load of b.html's: 3 if kept
    assert len(errors) == 2 and all("404" in e for e in errors), errors


# JS: every shown element outside the tray whose box reaches into it (the tray's own overlays and
# the boxes that hold the tray do not count). A control drawn under the canvas shows as a stray box.
ON_TRAY = """(() => {
  const tray = document.querySelector('#tray'), t = tray.getBoundingClientRect();
  return [...document.querySelectorAll('body *')].filter(el => {
    if (tray.contains(el) || el.contains(tray) || el.closest('[hidden]')) return false;
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    if (!r.width || !r.height || cs.visibility === 'hidden') return false;
    return r.left < t.right && r.right > t.left && r.top < t.bottom && r.bottom > t.top;
  }).map(el => `${el.tagName.toLowerCase()}#${el.id}.${el.className} ${el.textContent.trim()}`);
})()"""
HEAD = "document.querySelector('#th-left').textContent"
# JS for a failed wait: the fit line, the toast, and which files the page has finished loading
FIT = """[document.querySelector('#fit').textContent, document.querySelector('#toast').textContent,
  performance.getEntriesByType('resource').map(e => e.name.split('/').pop()).join()].join(' | ')"""
# JS: the field canvas shows the fake engine's picture (flat 40,40,48: tests/fake_sd_cli.py png())
PICTURE_SHOWN = """(() => {
  const c = document.querySelector('#field'), x = c.getContext('2d');
  const at = (a, b) => x.getImageData(c.width * a | 0, c.height * b | 0, 1, 1).data.slice(0, 3);
  return [[.5, .5], [.1, .1], [.9, .9]].every(([a, b]) => at(a, b).join() === '40,40,48');
})()"""


@pytest.mark.parametrize("size", [PHONE, DESK], ids=["390x844", "1280x800"])
def test_develop_shows_the_picture_and_nothing_overlaps_the_tray(
    start, chrome, monkeypatch, tmp_path, size
):
    srv = start()
    gate = tmp_path / "go-on"  # the fake engine holds at step 2 until it exists: no race with
    monkeypatch.setenv("FAKE_HOLD", str(gate))  # a slow machine that would skip "developing"
    chrome.open(srv.url, *size)
    chrome.wait(visible("develop") + " && !document.querySelector('#go').disabled", show=FIT)
    chrome.eval("document.querySelector('#prompt').value = 'a lighthouse at dusk'")
    chrome.eval("document.querySelector('#go').click()")
    chrome.wait(f"/Developing/.test({HEAD})", 30, show=HEAD)
    shot(chrome, f"t-tray-developing-{size[0]}x{size[1]}")
    assert chrome.eval(ON_TRAY) == []
    gate.touch()
    chrome.wait(f"/Done/.test({HEAD})", 30, show=HEAD)
    chrome.wait(PICTURE_SHOWN, 10)  # after the 700 ms crossfade from the specks
    shot(chrome, f"t-tray-done-{size[0]}x{size[1]}")
    assert chrome.eval(ON_TRAY) == []
    assert chrome.errors() == []


def test_jobs_queued_before_the_page_opened_show_in_develop(start, chrome, monkeypatch, tmp_path):
    """Jobs queued again after a crash (or before a reload) run and show like the page's own."""
    srv = start()
    gate = tmp_path / "go-on"
    monkeypatch.setenv("FAKE_HOLD", str(gate))
    for prompt in ("a red barn", "a blue boat"):
        srv.queue().submit(JobRequest(model_id="std-q4", prompt=prompt, steps=4))
    chrome.open(srv.url, *DESK)
    chrome.wait(f"/Preparing|Developing/.test({HEAD})", 30, show=HEAD)  # held: no new step yet
    recent = "document.querySelector('#recent').textContent"
    chrome.wait(f"/a red barn/.test({recent}) && /a blue boat.*queued/.test({recent})", show=recent)
    gate.touch()
    chrome.wait(f"/Done/.test({HEAD})", 60, show=HEAD)
    assert chrome.errors() == []


# JS: a field on its own canvas after its last step, no finish() yet (the engine decodes the
# picture), the progress made instant: [the different pictures it shows in 6 looks 100 ms apart,
# the share of its ink in the middle quarter of the canvas (an even scatter: 1/4)]
AFTER_LAST_STEP = """new Promise(r => { const c = document.createElement('canvas');
  c.style.cssText = 'display: block; width: 480px; height: 320px'; document.body.append(c);
  const f = new DevelopField(c, { followS: 0.01, maxSpeed: 1e3 });
  f.prepare(); for (let k = 1; k <= 25; k++) f.step(k, 25);
  const x = c.getContext('2d'), seen = new Set(); let n = 0;
  const ink = (a, b, w, h) => x.getImageData(a | 0, b | 0, w | 0, h | 0).data
    .reduce((s, v, i) => (i % 4 ? s : s + v - 13), 0);  // red over the 0D background
  setTimeout(() => { const t = setInterval(() => { seen.add(c.toDataURL()); if (++n < 6) return;
    clearInterval(t); const W = c.width, H = c.height;
    r([seen.size, ink(W / 4, H / 4, W / 2, H / 2) / ink(0, 0, W, H)]); }, 100); }, 500); })"""


def test_a_typed_size_shows_the_grid_advice(start, chrome):
    """Very large sizes can show a faint grid with Qwen-Image 2.1 (sd.cpp #2041, #2193): the
    size help that appears once a size is typed says to make it smaller, then Upscale x4."""
    chrome.open(start().url, *DESK)
    chrome.wait(visible("develop"), show=LOADED)
    help_ = "document.querySelector('#size-help')"
    assert chrome.eval(f"{help_}.hidden")  # the presets need no help
    typed = "(w => { w.value = '2048'; w.dispatchEvent(new Event('change')); return 1; })"
    chrome.eval(typed + "(document.querySelector('#w'))")
    assert not chrome.eval(f"{help_}.hidden")
    shot(chrome, "size-help-grid")
    assert chrome.eval(f"{help_}.textContent") == (
        "Sizes snap to steps of 32. Very large sizes can show a faint grid pattern with this"
        " model: make the picture at a normal size, then use Upscale ×4."
    )
    assert chrome.errors() == []


def test_the_field_keeps_moving_in_chaos_after_the_last_step(start, chrome):
    """At the last step the specks settled and stood still for the seconds the engine decodes
    the picture: it looked hung. They keep moving, and never gather into a shape."""
    chrome.open(start().url, *DESK)
    chrome.wait(visible("develop"), show=LOADED)
    pictures, middle = chrome.eval(AFTER_LAST_STEP)
    assert pictures >= 4, pictures  # 6 when no frame is late
    assert abs(middle - 0.25) < 0.05, middle
    assert chrome.errors() == []


# JS: how many different pictures a loader mark's canvas shows in 1.2 s, one look every 100 ms.
# Drawing, it changes: the mark holds still only 600 ms of each 2.8 s cycle.
MARK_PICTURES = """new Promise(r => { const seen = new Set(); let n = 0;
  const t = setInterval(() => { seen.add(window.__mark.toDataURL());
    if (++n === 13) { clearInterval(t); r(seen.size); } }, 100); })"""


def test_the_loader_mark_draws_and_stops_when_removed(start, chrome):
    chrome.open(start().url, *DESK)
    chrome.wait(visible("develop"), show=LOADED)
    chrome.eval("""(() => { window.__el = document.createElement('lr-loader');
      window.__el.setAttribute('size', '32'); document.body.append(window.__el);
      window.__mark = window.__el.querySelector('canvas'); return 1; })()""")
    inked = "(c => c.getContext('2d').getImageData(0, 0, c.width, c.height).data"
    inked += ".some((v, i) => i % 4 === 3 && v > 0))(window.__mark)"
    assert chrome.eval(inked)  # drawn at once, not one frame later
    assert chrome.eval("window.__el.getAttribute('aria-hidden')") == "true"
    assert chrome.eval(MARK_PICTURES) > 1
    chrome.eval("window.__el.remove()")
    assert chrome.eval(MARK_PICTURES) == 1
    assert chrome.errors() == []


# JS: a settled mark's alpha, one value per canvas pixel
MARK_ALPHA = """s => { const el = document.createElement('lr-loader'); el.setAttribute('size', s);
  document.body.append(el); const c = el.querySelector('canvas');
  return Array.from(c.getContext('2d').getImageData(0, 0, s, s).data.filter((v, i) => i % 4 === 3));
}"""


def test_the_loader_mark_settles_into_the_logo_mark_pixel_for_pixel(start, chrome):
    """Reduced motion shows the settled mark: the logo's 15 squares, centred in a 16-unit box,
    edges on whole pixels (no blur), the faint speck at its opacity."""

    def js_round(v):  # Math.round, not Python's round-half-to-even
        return math.floor(v + 0.5)

    svg = ElementTree.parse(server.STATIC / "logo-horizontal.svg").getroot()
    mark = svg.find("{http://www.w3.org/2000/svg}g")  # the first <g>; the second is the word
    chrome.call("Emulation.setEmulatedMedia", features=[
        {"name": "prefers-reduced-motion", "value": "reduce"}])  # fmt: skip
    chrome.open(start().url, *DESK)
    chrome.wait(visible("develop"), show=LOADED)
    for size in (16, 32, 96):
        k, want = size / 16, [0] * size * size
        for group in mark.iter("{http://www.w3.org/2000/svg}g"):  # the mark, then its faint speck
            alpha = round(255 * float(group.get("opacity", 1)))
            for r in group.findall("{http://www.w3.org/2000/svg}rect"):
                x, y, w, h = (float(r.get(a)) for a in ("x", "y", "width", "height"))
                for row in range(js_round((y - 3.5) * k), js_round((y + h - 3.5) * k)):
                    for col in range(js_round((x + 1) * k), js_round((x + w + 1) * k)):
                        want[row * size + col] = alpha
        got = chrome.eval(f"({MARK_ALPHA})({size})")
        assert sum(map(bool, want)) == 51 * k * k  # 12 squares of 2 x 2 + 3 of 1 x 1, in units
        assert all(abs(g - w) <= 1 for g, w in zip(got, want, strict=True)), size
    assert chrome.errors() == []


# JS: log what the tray's live regions say, once per write
LIVE_LOG = """(() => { window.__said = []; window.__mo = new MutationObserver(ms => {
  const r = new Set(ms.map(m => (m.target.nodeType === 1 ? m.target : m.target.parentElement)
    ?.closest('.stage [aria-live]')).filter(Boolean));
  for (const el of r) window.__said.push(el.textContent);
}); window.__mo.observe(document.body, { subtree: true, childList: true, characterData: true });
  return 1; })()"""  # held in window: an observer nothing holds may be garbage collected


def test_the_tray_announces_state_changes_not_every_step(start, chrome):
    """#th-left was aria-live and rewritten on every step and every 0.5 s poll."""
    srv = start()
    chrome.open(srv.url, *DESK)
    chrome.wait(visible("develop") + " && !document.querySelector('#go').disabled", show=FIT)
    chrome.eval(LIVE_LOG)
    chrome.eval("document.querySelector('#prompt').value = 'a lighthouse at dusk'")
    chrome.eval("document.querySelector('#go').click()")
    chrome.wait(f"/Done.* · /.test({HEAD})", 30, show=HEAD)  # the page opens on the last Done
    chrome.eval(SETTLE)
    said = chrome.eval("window.__said")
    assert said and said[-1] == "Done", said
    assert len(said) == len(set(said)) <= 5, said  # e.g. Waiting, Preparing…, Developing…, Done
    assert not any("step" in s for s in said), said


# JS: two references through the file input, as the file picker hands them over
ADD_REFS = """(async () => {
  const c = document.createElement('canvas'); c.width = c.height = 8;
  const b = await new Promise(r => c.toBlob(r, 'image/png')), dt = new DataTransfer();
  for (const n of ['a.png', 'b.png']) dt.items.add(new File([b], n, { type: 'image/png' }));
  const f = document.querySelector('#ref-file'); f.files = dt.files;
  f.dispatchEvent(new Event('change')); return 1;
})()"""


def test_two_references_read_image1_image2_and_a_done_edit_hints_a_new_seed(start, chrome):
    srv = start()  # SPEC 5fe8905: tags "1", "2"; the prompt and its help use <image1>, <image2>
    chrome.open(srv.url, *DESK)
    chrome.wait(visible("develop") + " && !document.querySelector('#go').disabled")
    chrome.eval(ADD_REFS)
    chrome.wait("document.querySelectorAll('#refs .tag').length === 2")
    tags = "[...document.querySelectorAll('#refs .tag')].map(t => t.textContent).join()"
    assert chrome.eval(tags) == "1,2"
    help_ = chrome.eval("document.querySelector('#refs-help').textContent")
    assert help_ == "Point at a picture with <image1>, <image2>… in the instruction."
    chrome.eval("document.querySelector('#prompt').value = ''")
    chrome.eval("document.querySelectorAll('#refs .tag')[1].click()")
    assert chrome.eval("document.querySelector('#prompt').value") == "<image2> "
    chrome.eval("document.querySelector('#prompt').value = 'the boat of <image2> on <image1>'")
    chrome.wait("!document.querySelector('#go').disabled")
    chrome.eval("document.querySelector('#go').click()")
    chrome.wait(f"/Done/.test({HEAD})", 30, show=HEAD)
    chrome.wait("!document.querySelector('#warnline').hidden")
    shot(chrome, "t-edit-two-refs-done-1280x800")
    hint = chrome.eval("document.querySelector('#warnline').textContent")
    assert hint == "Not what you wanted? Try again: each run uses a new seed."
    assert chrome.errors() == []


# JS: the Recent rows that show the missing-picture thumb, as [accessible name, draggable, tooltip]
MISSING_ROWS = """[...document.querySelectorAll('#recent .rrow')]
  .filter(r => r.querySelector('.miss'))
  .map(r => [r.getAttribute('aria-label'), r.draggable, r.querySelector('.miss').title])"""


def test_a_recent_row_whose_file_is_gone_shows_the_missing_picture_thumb(start, chrome):
    srv = start()
    pic = paths.default_output_dir() / "latentroom-gone.png"
    Image.new("RGB", (96, 64), "gray").save(pic)
    gone = history.add(JobRequest(model_id="std-q4", prompt="a boathouse in fog"), pic, 2)
    pic.unlink()  # moved or deleted outside Latentroom: /thumb answers 404
    chrome.open(srv.url, *DESK)
    chrome.wait("!!document.querySelector('#recent .miss')")
    shot(chrome, "t-recent-missing-1280x800")
    name = "a boathouse in fog, 1 frame · now, picture file not found"
    assert chrome.eval(MISSING_ROWS) == [[name, False, "Picture file not found"]]
    assert chrome.eval("document.querySelectorAll('#recent .rrow img').length") == 1  # the other
    # Chrome logs the 404s of the gone file: its thumb, and its picture (the tray opens the newest)
    lost = f"404 (Not Found) {srv.url.split('?')[0]}api/history/{gone.id}/"
    assert [e for e in chrome.errors() if lost not in e] == []


# JS: the Library cells that show the missing-picture thumb, as [accessible name, tooltip, has img]
MISSING_CELLS = """[...document.querySelectorAll('#lib-body .shot')]
  .filter(c => c.querySelector('.miss'))
  .map(c => [c.getAttribute('aria-label'), c.querySelector('.miss').title,
    !!c.querySelector('img')])"""


# JS: the visible help lines about the gone file in the Library panel
GONE_HELP = """[...document.querySelectorAll('#lib-panel p.help')]
  .filter(p => p.checkVisibility() && p.textContent.includes('file is gone'))
  .map(p => p.textContent)"""
GONE_SAYS = {
    DESK: "The file is gone, so it cannot be shown in its folder, used as a reference or upscaled.",
    PHONE: "The file is gone, so it cannot be saved to the phone.",
}


@pytest.mark.parametrize("size", [PHONE, DESK], ids=["390x844", "1280x800"])
def test_a_library_cell_whose_file_is_gone_shows_the_missing_picture_thumb(start, chrome, size):
    srv = start()  # no broken-image glyph in the contact sheet
    pic = paths.default_output_dir() / "latentroom-gone.png"
    Image.new("RGB", (96, 64), "gray").save(pic)
    gone = history.add(JobRequest(model_id="std-q4", prompt="a boathouse in fog"), pic, 2)
    pic.unlink()
    chrome.open(srv.url + "#library", *size)
    chrome.wait("!!document.querySelector('#lib-body .shot .miss')", 10)
    shot(chrome, f"t-library-missing-{size[0]}x{size[1]}")
    name = f"#{gone.id:04d}, a boathouse in fog, picture file not found"
    assert chrome.eval(MISSING_CELLS) == [[name, "Picture file not found", False]]
    assert chrome.eval("document.querySelectorAll('#lib-body .shot img').length") == 1  # the other
    chrome.eval("document.querySelector('#lib-body .shot .miss').closest('.shot').click()")
    panel = "document.querySelector('#lib-panel').innerText"
    chrome.wait(f"{GONE_HELP}.length > 0", 10, show=panel)
    shot(chrome, f"t-library-missing-open-{size[0]}x{size[1]}")
    assert chrome.eval(GONE_HELP) == [GONE_SAYS[size]]
    lost = f"404 (Not Found) {srv.url.split('?')[0]}api/history/{gone.id}/"
    assert [e for e in chrome.errors() if lost not in e] == []


def test_the_delete_dialog_names_the_folder_the_picture_is_in(start, chrome, tmp_path):
    srv = start()  # a custom output folder, not "Pictures\Latentroom"
    folder = tmp_path / "My Photos"
    folder.mkdir()
    pic = folder / "latentroom-own.png"
    Image.new("RGB", (96, 64), "gray").save(pic)
    own = history.add(JobRequest(model_id="std-q4", prompt="a boathouse in fog"), pic, 2)
    chrome.open(srv.url + "#library", *DESK)
    chrome.wait(f"!!document.querySelector('#lib-body .shot[data-id=\"{own.id}\"]')", 10)
    chrome.eval(f"document.querySelector('#lib-body .shot[data-id=\"{own.id}\"]').click()")
    chrome.wait("!!document.querySelector('#lib-panel [data-del]')", 10)
    chrome.eval("document.querySelector('#lib-panel [data-del]').click()")
    chrome.wait("!!document.querySelector('#dlg p')", 10)
    shot(chrome, "t-library-delete-1280x800")
    says = f"The picture file is deleted from {pic.parent} too, together with its settings."
    assert chrome.eval("document.querySelector('#dlg p').textContent").startswith(says)
    assert chrome.errors() == []


# JS: the buttons the shown toast covers. A button counts where it is seen: the element at its
# centre is the button (clicks go through the toast, so that element is never the toast)
TOAST_ON_BUTTONS = """(() => {
  const t = document.querySelector('#toast.show')?.getBoundingClientRect();
  if (!t) return ['no toast shown'];
  return [...document.querySelectorAll('button, .btn, [role=tab]')].filter(b => {
    const r = b.getBoundingClientRect(), mid = [r.left + r.width / 2, r.top + r.height / 2];
    return r.width && r.height && b.contains(document.elementFromPoint(...mid))
      && r.left < t.right && r.right > t.left && r.top < t.bottom && r.bottom > t.top;
  }).map(b => b.textContent.trim() || b.getAttribute('aria-label'));
})()"""
GONE_TOAST = "import('/ui.js').then(m => m.toast('This picture is no longer in its folder.'))"
SETTLE = "new Promise(r => setTimeout(() => r(1), 500))"  # the toast's 280 ms slide-in, and more


@pytest.mark.parametrize("size", [PHONE, DESK], ids=["390x844", "1280x800"])
def test_a_toast_never_covers_a_button(start, chrome, size):
    """A 390x844 screenshot: a gone file's Library panel opened while Develop's
    toast showed, and the toast covered "Save to phone". On a phone Develop's button sticks
    where the toast goes too."""
    srv = start()
    pic = paths.default_output_dir() / "latentroom-gone.png"
    Image.new("RGB", (96, 64), "gray").save(pic)
    history.add(JobRequest(model_id="std-q4", prompt="a boathouse in fog"), pic, 2)
    pic.unlink()
    chrome.open(srv.url, *size)
    chrome.wait(visible("develop"))
    chrome.eval(GONE_TOAST)
    chrome.eval(SETTLE)
    shot(chrome, f"t-toast-develop-{size[0]}x{size[1]}")
    assert chrome.eval(TOAST_ON_BUTTONS) == []
    chrome.eval("document.querySelector('#tab-library').click()")
    chrome.wait("!!document.querySelector('#lib-body .shot .miss')", 10)
    chrome.eval(GONE_TOAST)  # shown before the panel opens: it moves when the panel comes
    chrome.eval("document.querySelector('#lib-body .shot .miss').closest('.shot').click()")
    chrome.wait(f"{GONE_HELP}.length > 0", 10)
    chrome.eval(SETTLE)
    shot(chrome, f"t-toast-library-missing-{size[0]}x{size[1]}")
    assert chrome.eval(TOAST_ON_BUTTONS) == []


TOAST_RECT = """(r => [r.top, r.bottom, innerHeight])(
  document.querySelector('#toast.show').getBoundingClientRect())"""
LONG_TOAST = (  # two lines on a desktop: too tall for the gap above the grid
    "Smart search could not start. Close other programs that use the graphics card, then try again."
)


@pytest.mark.parametrize("size", [PHONE, DESK], ids=["390x844", "1280x800"])
def test_a_toast_stays_on_screen_over_a_full_library(start, chrome, size):
    """Every Library thumbnail is a button; moving above all of them sent the toast off the
    top. With no free place on screen it stays where it always is."""
    srv = start()
    for seed in range(2, 60):
        pic = paths.default_output_dir() / f"latentroom-{seed}.png"
        Image.new("RGB", (96, 64), "gray").save(pic)
        history.add(JobRequest(model_id="std-q4", prompt="a lighthouse at dusk"), pic, seed)
    chrome.open(srv.url + "#library", *size)
    chrome.wait("document.querySelectorAll('#lib-body .shot img').length > 20", 10)
    chrome.eval("document.querySelectorAll('#lib-body .shot')[20].scrollIntoView({block:'center'})")
    chrome.eval(f"import('/ui.js').then(m => m.toast({json.dumps(LONG_TOAST)}))")
    chrome.eval(SETTLE)
    shot(chrome, f"t-toast-library-full-{size[0]}x{size[1]}")
    top, bottom, height = chrome.eval(TOAST_RECT)
    assert 0 <= top and bottom <= height


SHOWN = "(document.querySelector('#toast.show')?.textContent || '')"
TWO_FAILED = """import('/ui.js').then(m => Promise.allSettled([m.api('/api/version'),
  m.api('/api/version')])).then(r => r.map(x => x.reason?.message))"""


@pytest.mark.parametrize("size", [PHONE, DESK], ids=["390x844", "1280x800"])
def test_a_lost_connection_stays_on_screen_until_it_is_back(start, chrome, size):
    """'lr-lost' fired after 2 failed calls but nothing listened. Lost connection, short form:
    a toast that stays, a try every 2 s, "Connected again."; no "close the window" on a phone."""
    srv = start()
    chrome.open(srv.url, *size)
    chrome.wait(visible("develop"))
    offline = partial(chrome.call, "Network.emulateNetworkConditions", latency=0)
    offline = partial(offline, downloadThroughput=-1, uploadThroughput=-1)
    chrome.call("Network.enable")
    offline(offline=True)  # a stopped server still answers on open keep-alive connections
    errors = chrome.eval(TWO_FAILED)
    chrome.wait(f"{SHOWN}.startsWith('Lost the connection')", 5)
    time.sleep(4.5)  # longer than a plain toast lives
    lost = chrome.eval(SHOWN)
    shot(chrome, f"t-lost-{size[0]}x{size[1]}")
    if size == PHONE:
        assert all("PC is on" in e and "Close the window" not in e for e in errors), errors
        assert "your PC" in lost and "Close the window" not in lost
    else:
        assert lost.startswith("Lost the connection to Latentroom.")
    offline(offline=False)
    chrome.wait(f"{SHOWN} === 'Connected again.'", 5)


GO_ON = "!document.querySelector('#go').disabled"


def test_a_failed_size_check_is_tried_again(start, chrome, monkeypatch):
    """One failed fit check left the Develop button disabled until a setting changed."""
    srv = start()
    real, broken = catalogue.fit, threading.Event()

    def fit(*a, **k):
        if broken.is_set():
            raise OSError("graphics card busy")
        return real(*a, **k)

    monkeypatch.setattr(catalogue, "fit", fit)
    chrome.open(srv.url, *DESK)
    chrome.wait(visible("develop"))
    chrome.wait(GO_ON, 10, show=FIT)
    broken.set()
    chrome.eval("document.querySelector('#sizes button:not([aria-checked=true])').click()")
    chrome.wait("document.querySelector('#fit').textContent.includes('Could not check')", 5)
    assert not chrome.eval(GO_ON)
    broken.clear()
    chrome.wait(GO_ON, 8, show=FIT)


def test_open_lora_folder_on_models_opens_it_once(start, chrome, monkeypatch):
    """Models' own handler and Develop's page-wide one both called /api/open."""
    opened = []
    monkeypatch.setattr(server, "_open_folder", opened.append)
    srv = start()
    chrome.open(srv.url + "#models", *DESK)
    chrome.wait(visible("models"))
    chrome.eval("document.querySelector('#screen-models [data-open-loras]').click()")
    chrome.eval(SETTLE)  # both calls went out at the click
    assert opened == [paths.loras_dir()]


HTML_PROMPT = 'a lighthouse <b>at dusk</b> <img src=x onerror="window.__x=1">'
RECENT_TEXTS = "[...document.querySelectorAll('#recent .rrow .t')].map(t => t.textContent)"


def test_a_prompt_with_html_shows_as_plain_text(start, chrome):
    srv = start()  # a saved prompt is text, never markup (ui.js esc)
    pic = paths.default_output_dir() / "latentroom-html.png"
    Image.new("RGB", (96, 64), "gray").save(pic)
    e = history.add(JobRequest(model_id="std-q4", prompt=HTML_PROMPT), pic, 2)
    chrome.open(srv.url, *DESK)
    chrome.wait(f"{RECENT_TEXTS}.length === 2", show=RECENT_TEXTS)
    assert chrome.eval(RECENT_TEXTS) == [HTML_PROMPT, "a lighthouse at dusk"]  # newest first
    chrome.eval("location.hash = '#library'")
    prompt = "document.querySelector('#lib-panel .lp-prompt')?.textContent"
    chrome.wait(f"{prompt} !== undefined", 10)  # the newest is selected
    assert chrome.eval(prompt) == HTML_PROMPT
    cell = f"document.querySelector('#lib-body .shot[data-id=\"{e.id}\"]').ariaLabel"
    assert chrome.eval(cell) == f"#{e.id:04d}, {HTML_PROMPT}"
    assert chrome.eval("window.__x") is None  # the onerror never ran
    assert chrome.errors() == []


UNCENSORED = "!!document.querySelector('#pairs [data-use=\"unc-q4\"]')"  # its Use this button
GATE = "document.querySelector('#pairs .adult')"
RULE = (
    "You are responsible for what you make. No real people without their consent. "
    "Nobody under 18. Nothing illegal where you live."
)
QWEN_LIC = (
    "Qwen-Image models: research or evaluation use only, no commercial use. "
    "Licence: huggingface.co/Qwen/Qwen-Image-2.1/blob/main/LICENSE"
)
LIC = "document.querySelector('#lic-note').textContent"
QWEN_URL = "https://huggingface.co/Qwen/Qwen-Image-2.1/blob/main/LICENSE"


def test_models_hides_uncensored_models_until_18_plus_is_confirmed(start, chrome):
    srv = start()  # the 18+ gate (models.js adult)
    chrome.open(srv.url + "#models", *DESK)
    chrome.wait(f"!!{GATE} || {UNCENSORED}", show="document.querySelector('#pairs')?.innerText")
    assert chrome.eval(UNCENSORED) is False
    assert chrome.eval(f"{GATE}.querySelector('.t-body-strong').textContent") == (
        "Uncensored models are hidden"
    )
    assert RULE in chrome.eval(f"{GATE}.textContent")  # the rule, at the question
    assert chrome.eval(LIC) == QWEN_LIC  # in full words, under the table
    assert chrome.eval("document.querySelector('#lic-note a.ext').href") == QWEN_URL
    shot(chrome, "product-b1-models-gate")
    chrome.eval(f"{GATE}.querySelector('[data-adult]').click()")
    chrome.wait(UNCENSORED, 10)
    assert chrome.eval(f"!{GATE}")
    assert settings.load().adult_ok is True  # remembered: the gate does not come back
    assert chrome.errors() == []


def test_settings_18_plus_question_states_the_rule(start, chrome):
    chrome.open(start().url + "#settings", *DESK)  # third place the question is asked
    chrome.wait("!!document.querySelector('#adult-switch')", show=LOADED)
    chrome.eval("document.querySelector('#adult-switch').click()")
    chrome.wait("!!document.querySelector('#dlg [data-yes]')", 10)
    assert RULE in chrome.eval("document.querySelector('#dlg').textContent")
    shot(chrome, "product-b1-settings-dialog")
    assert chrome.errors() == []


SEARCH = {  # the real sizes of the Smart search files
    f.id: f
    for f in (
        replace(fake_catalogue._file("embed", "embed.gguf", "embedding"), size=309855456),
        replace(fake_catalogue._file("embed-mm", "mm.gguf", "embedding_vision"), size=554821024),
        replace(fake_catalogue._file("llama", "llama.zip", "llama"), size=36969567),
    )
}
SEARCH_ROW = "[...document.querySelectorAll('#parts .prow')].filter(r => r.textContent.includes('Smart search'))"  # noqa: E501


def test_smart_search_is_one_row_that_gets_and_deletes_its_3_files(start, chrome, monkeypatch):
    srv = start()  # ONE "Smart search" row; Download and Delete act on all 3 files
    files, have, got, gone = {**fake_catalogue.FILES, **SEARCH}, set(fake_catalogue.FILES), {}, []

    def begin(item):
        got[item.id] = downloads.DownloadState(
            item.id, item.name, "queued", 0, item.size, 0.0, None, None
        )  # fmt: skip
        return got[item.id]

    def delete(file_id):
        gone.append(file_id)
        have.discard(file_id)

    monkeypatch.setattr(catalogue, "files", lambda: files)
    monkeypatch.setattr(install, "installed", lambda: have)
    monkeypatch.setattr(downloads, "start", begin)
    monkeypatch.setattr(downloads, "progress", lambda: list(got.values()))
    monkeypatch.setattr(install, "delete_model", delete)
    chrome.open(srv.url + "#models", *DESK)
    chrome.wait(f"{SEARCH_ROW}.length === 1", show="document.querySelector('#parts')?.innerText")
    chrome.eval(f"{SEARCH_ROW}[0].scrollIntoView()")  # for the screenshots
    assert chrome.eval(f"{SEARCH_ROW}[0].querySelector('.sz').textContent") == "0.84 GB"
    help_ = "Find pictures by what is in them. Runs on the processor. About 0.8 GB."
    assert chrome.eval(f"{SEARCH_ROW}[0].querySelector('.help').textContent") == help_
    chrome.eval("document.querySelector('[data-part=\"search\"]').click()")
    chrome.wait("document.querySelectorAll('#parts [data-blk=\"search\"]').length === 1")
    assert set(got) == set(SEARCH)  # one block for the 3 downloads
    shot(chrome, "t-models-smart-search-dl-1280x800")
    for i in SEARCH:  # all 3 finish: one toast, then the row reads Installed
        got[i] = replace(got[i], status="done", done=got[i].total)
        have.add(i)
    chrome.wait("!!document.querySelector('#parts [data-rm=\"search\"]')")
    chrome.eval(f"{SEARCH_ROW}[0].scrollIntoView()")
    shot(chrome, "t-models-smart-search-installed-1280x800")
    toast = chrome.eval("document.querySelector('#toast').textContent")
    assert toast == "Smart search is installed."
    chrome.eval("document.querySelector('[data-rm=\"search\"]').click()")
    chrome.wait("!!document.querySelector('#dlg [data-yes]')")
    says = chrome.eval("document.querySelector('#dlg p').textContent")
    assert says.startswith("This frees 0.84 GB.")
    chrome.eval("document.querySelector('#dlg [data-yes]').click()")
    chrome.wait("!!document.querySelector('[data-part=\"search\"]')")
    assert gone[0] == "llama" and set(gone) == set(SEARCH)  # the program first
    assert chrome.errors() == []


def test_smart_search_busy_deletes_nothing(start, chrome, monkeypatch):
    srv, tried = start(), []  # llama-server runs: the program refuses, the other 2 files stay

    def busy(file_id):
        tried.append(file_id)
        raise install.LatentroomError("not_available", install.SEARCH_IN_USE, 409)

    monkeypatch.setattr(catalogue, "files", lambda: {**fake_catalogue.FILES, **SEARCH})
    monkeypatch.setattr(install, "installed", lambda: {*fake_catalogue.FILES, *SEARCH})
    monkeypatch.setattr(install, "delete_model", busy)
    chrome.open(srv.url + "#models", *DESK)
    chrome.wait("!!document.querySelector('#parts [data-rm=\"search\"]')")
    chrome.eval("document.querySelector('[data-rm=\"search\"]').click()")
    chrome.wait("!!document.querySelector('#dlg [data-yes]')")
    chrome.eval("document.querySelector('#dlg [data-yes]').click()")
    chrome.wait("document.querySelector('#toast').textContent.includes('busy')")
    assert chrome.eval("document.querySelector('#toast').textContent") == install.SEARCH_IN_USE
    assert tried == ["llama"]  # stopped at the first refusal
    assert chrome.eval("!!document.querySelector('#parts [data-rm=\"search\"]')")  # still Installed
    refused = "status of 409"  # the expected refusal, logged by Chrome; nothing else may fail
    assert [e for e in chrome.errors() if refused not in e] == []


VAE_ROW = "document.querySelector('#parts [data-blk=\"vae\"]')"


def test_models_page_stays_where_it_is_scrolled_while_a_download_runs(start, chrome, monkeypatch):
    """Scrolled up while the decoder (VAE) downloads, the page jumped back down to its row by
    itself: each poll re-drew the screen and put focus back on the row's button, which scrolled
    it into view. The model in use waits for the decoder, its old download long finished."""
    srv = start()
    have, gib = set(fake_catalogue.FILES) - {"vae"}, 1 << 30
    got = {"std-q4": downloads.DownloadState("std-q4", "s.gguf", "done", 1, 1, 0.0, None, None)}

    def begin(item):
        got[item.id] = downloads.DownloadState(item.id, item.name, "queued", 0, gib, 0, None, None)
        return got[item.id]

    def progress():  # every poll: 1% further
        d = got.get("vae")
        if d and d.status == "downloading":
            got["vae"] = replace(d, done=d.done + gib // 100)
        return list(got.values())

    monkeypatch.setattr(install, "installed", lambda: have)
    monkeypatch.setattr(downloads, "start", begin)
    monkeypatch.setattr(downloads, "progress", progress)
    chrome.open(srv.url + "#models", *SMALLEST)
    chrome.wait("!!document.querySelector('[data-part=\"vae\"]')")
    # a mouse click focuses the button; a click() from script does not
    chrome.eval(
        "(b => { b.scrollIntoView(); b.focus(); b.click(); })"
        "(document.querySelector('[data-part=\"vae\"]'))"
    )
    chrome.wait(f"{VAE_ROW}?.dataset.st === 'queued'")
    got["vae"] = replace(got["vae"], status="downloading", speed=50 << 20, eta=20.0)
    chrome.wait(f"{VAE_ROW}?.dataset.st === 'downloading'")
    box = "document.querySelector('#screen-models')"
    chrome.eval(f"{box}.scrollTop = 0; {VAE_ROW}.kept = 1")
    assert chrome.eval(f"{VAE_ROW}.getBoundingClientRect().top") > SMALLEST[1]  # out of sight
    line = f"{VAE_ROW}.querySelector('.line').textContent"
    before = chrome.eval(line)
    chrome.eval("new Promise(r => setTimeout(() => r(1), 3500))")  # 3 polls
    assert chrome.eval(line) != before  # the polls ran and moved the bar
    assert chrome.eval(f"{box}.scrollTop") == 0
    assert chrome.eval(f"{VAE_ROW}.kept") == 1  # progress painted in place, no re-draw
    assert chrome.eval("document.activeElement.matches('[data-k=\"vae\"]')")  # focus kept
    # a real re-draw: the row loses its buttons, focus goes to the first button on the page.
    # Scrolled to between that button and the row, neither may pull the page.
    below = "(b => { const t = b.querySelector('button').getBoundingClientRect().bottom; b.scrollTop += Math.ceil(t - b.getBoundingClientRect().top) + 10; return b.scrollTop; })"  # noqa: E501
    y = chrome.eval(f"{below}({box})")
    assert y > 0 and chrome.eval(f"{VAE_ROW}.getBoundingClientRect().top") > SMALLEST[1]
    got["vae"] = replace(got["vae"], status="verifying")
    chrome.wait(f"{VAE_ROW}?.dataset.st === 'verifying'")
    assert chrome.eval(f"{box}.scrollTop") == y
    assert chrome.errors() == []


FAKE_LLAMA = Path(__file__).parent / "fake_llama_server.py"  # reads a picture's colour
ORDER = "document.querySelector('.lib-order').textContent"
SECTIONS = "[...document.querySelectorAll('#lib-body .day-head')].map(h => h.textContent)"
TYPE = "(s => {{ s.value = '{1}'; s.dispatchEvent(new Event('input')); }})({0})"  # as a key does
PANEL_ID = "document.querySelector('#lib-panel .lp-head .id')?.textContent"  # "#0003"
SELECTED = "+document.querySelector('#lib-body .shot[aria-selected=\"true\"]')?.dataset.id"


@pytest.mark.parametrize("size", [PHONE, DESK], ids=["390x844", "1280x800"])
def test_library_search_finds_pictures_by_what_is_in_them(start, chrome, monkeypatch, size):
    srv = start()  # Smart search downloaded, the fake llama-server behind it
    monkeypatch.setattr(catalogue, "files", lambda: {**fake_catalogue.FILES, **SEARCH})
    monkeypatch.setattr(install, "installed", lambda: {*fake_catalogue.FILES, *SEARCH})
    monkeypatch.setenv("LATENTROOM_LLAMA", f'"{sys.executable}" "{FAKE_LLAMA}"')
    monkeypatch.setenv("FAKE_LLAMA_EMBED_S", "0.4")  # 4 pictures: "Indexing" shows ~2 s
    search._skipped.clear()
    pics = {}
    for colour in ("red", "green", "blue"):
        pic = paths.default_output_dir() / f"latentroom-{colour}.png"
        Image.new("RGB", (96, 64), colour).save(pic)
        pics[colour] = history.add(JobRequest(model_id="std-q4", prompt="a boathouse"), pic, 2)
    try:
        chrome.open(srv.url + "#library", *size)
        box = "document.querySelector('#lib-search')"
        chrome.wait(f"{box}.placeholder === 'Describe the picture you want'", 10)
        chrome.wait(f"{ORDER}.startsWith('Indexing ')", 10, show=ORDER)
        assert chrome.eval(ORDER).endswith(" of 4 · Newest first")
        shot(chrome, f"t-library-indexing-{size[0]}x{size[1]}")
        chrome.wait(f"{ORDER} === 'Newest first'", 30, show=ORDER)  # all 4 indexed
        green = f"document.querySelector('#lib-body .shot[data-id=\"{pics['green'].id}\"]')"
        chrome.eval(f"{green}.click(); document.querySelector('[data-ph-back]')?.click()")
        assert chrome.eval(PANEL_ID) == f"#{pics['green'].id:04d}"  # a picture picked by hand
        chrome.eval(TYPE.format(box, "a red fox"))
        chrome.wait(f"{ORDER} === 'Best match first'", 15, show=ORDER)
        shot(chrome, f"t-library-smart-search-{size[0]}x{size[1]}")
        first = "+document.querySelector('#lib-body .shot').dataset.id"
        assert chrome.eval(first) == pics["red"].id  # by colour: every prompt is the same
        assert chrome.eval(SECTIONS) == ["Best matches4 pictures"]  # one group, not by day
        # the panel follows the new results (green is still in them, 3rd or 4th)
        assert chrome.eval(SELECTED) == pics["red"].id
        assert chrome.eval(PANEL_ID) == f"#{pics['red'].id:04d}"
        chrome.eval(TYPE.format(box, ""))
        chrome.wait(f"{ORDER} === 'Newest first' && {SECTIONS}[0]?.startsWith('Today')", 15)
        assert chrome.eval(SELECTED) == chrome.eval(first)  # back to the newest
        assert chrome.errors() == []
    finally:
        search.stop()
        if search._indexer:
            search._indexer.join(30)


def test_develop_and_models_fit_count_the_loras_and_turbo(start, chrome, monkeypatch):
    """The fit line in Develop and the labels in Models count the LoRAs and the
    Turbo add-on Develop has on, as the queue does (jobs.lora_bytes)."""
    srv = start()
    (paths.loras_dir() / "test-style-a.safetensors").write_bytes(b"x" * 300)
    lora, turbo = 300, fake_catalogue.FILES["turbo-lora"].size
    seen = []

    def fit(model, hw, width=1024, height=1024, refs=0, lora_bytes=0):
        seen.append((model.id, lora_bytes))
        return catalogue.Fit("full_gpu", 0, 0, 1.0, "test")

    def saw(*calls):  # the server got these fit calls, in time
        end = time.monotonic() + 10
        while not set(calls) <= set(seen) and time.monotonic() < end:
            time.sleep(0.05)
        assert set(calls) <= set(seen), seen

    monkeypatch.setattr(catalogue, "fit", fit)
    chrome.open(srv.url, *DESK)
    chrome.wait(visible("develop"))
    chrome.eval("document.querySelector('#turbo').click()")
    saw(("std-q4", turbo))
    chrome.eval("document.querySelector('#lora-add').click()")
    pick = "document.querySelector('#lora-list input[value=\"test-style-a\"]')"
    chrome.wait(f"!!{pick}")
    chrome.eval(f"{pick}.click()")
    saw(("std-q4", lora + turbo))
    seen.clear()
    chrome.eval("document.querySelector('#tab-models').click()")
    chrome.wait(visible("models"))
    saw(("std-q4", lora + turbo), ("turbo-q4", lora))  # a Turbo model takes no add-on
    assert chrome.errors() == []


def test_models_fit_note_names_the_reference_pictures(start, chrome):
    """With reference pictures in Develop the fit labels count them, so the note
    under the table says so; without any it names only the size, as before."""
    srv = start()
    note = "document.querySelector('#fit-note').textContent"
    pc = ", on this PC (RTX test, 16 GB)."
    chrome.open(srv.url + "#models", *DESK)
    want = "Fit is checked for 1024 × 1024, the size set in Develop" + pc
    chrome.wait(f"{visible('models')} && {note} === {json.dumps(want)}", show=note)
    chrome.eval("location.hash = '#develop'")
    chrome.wait(visible("develop") + " && !document.querySelector('#go').disabled")
    chrome.eval(ADD_REFS)
    chrome.wait("document.querySelectorAll('#refs .tag').length === 2")
    chrome.eval("document.querySelector('#tab-models').click()")
    want = "Fit is checked for 1024 × 1024 with 2 reference pictures, as set in Develop" + pc
    chrome.wait(f"{visible('models')} && {note} === {json.dumps(want)}", show=note)
    shot(chrome, "models-fit-note-refs")
    assert chrome.errors() == []


def test_visible_waits_until_the_page_has_started(start, chrome):
    """The Develop HTML has text before start-up has run, so a wait on the text
    alone let a test click Turbo too early; start-up then undid the click (onModel) and the fit
    with the add-on never came (the "once under load" flake). Hold start-up: the wait is false."""
    srv = start()
    chrome.call("Fetch.enable", patterns=[{"urlPattern": "*/api/catalogue"}])
    chrome.open(srv.url, *DESK)
    end = time.monotonic() + 15
    while not (held := [e for e in chrome.events if e["method"] == "Fetch.requestPaused"]):
        assert time.monotonic() < end, "start-up never asked for the catalogue"
        chrome.eval("1")  # pulls in the events that arrived
        time.sleep(0.05)
    assert not chrome.eval(visible("develop"))
    chrome.call("Fetch.continueRequest", requestId=held[0]["params"]["requestId"])
    chrome.call("Fetch.disable")
    chrome.wait(visible("develop"), show=LOADED)
    assert chrome.errors() == []


DO_OFF = "[...document.querySelectorAll('#dolist [aria-disabled=true]')].map(b => b.dataset.k)"


def test_from_a_picture_offers_no_part_it_has_not_checked(start, chrome):
    srv = start()  # until /api/models answers, the upscaler + face detector are not assumed
    chrome.call("Fetch.enable", patterns=[{"urlPattern": "*/api/models"}])  # held, never answered
    chrome.open(srv.url, *DESK)
    chrome.wait(visible("develop"))
    pick = "document.querySelector('#start [data-m=\"%s\"]').click()"
    chrome.eval(pick % "from")
    chrome.wait("!!document.querySelector('#dolist [data-k=\"facefix\"]')")
    assert chrome.eval(DO_OFF) == ["enhance", "facefix"]
    chrome.call("Fetch.disable")  # now it answers: both parts are on disk in the stub
    chrome.eval(pick % "new")
    chrome.eval(pick % "from")
    chrome.wait(f"{DO_OFF}.length === 0", show=DO_OFF)
    assert chrome.errors() == []


# JS: the Strength slider as shown: [min, max, value, the number next to the label]
STRENGTH = """(r => [r.min, r.max, r.value, document.querySelector('#str-v').textContent])(
  document.querySelector('#str'))"""


def test_rework_starts_at_a_strength_that_visibly_changes_the_picture(start, chrome):
    srv = start()  # on Qwen-Image 0.75 barely changes the photo, 0.9 restyles it
    chrome.open(srv.url, *DESK)
    chrome.wait(visible("develop"))
    chrome.eval("document.querySelector('#start [data-m=\"from\"]').click()")
    chrome.wait('!!document.querySelector(\'#dolist [data-k="img2img"][aria-checked="true"]\')')
    assert chrome.eval(STRENGTH) == ["0.7", "0.95", "0.85", "0.85"]
    chrome.eval("document.querySelector('#dolist [data-k=\"inpaint\"]').click()")
    assert chrome.eval(STRENGTH) == ["0.05", "1", "0.75", "0.75"]  # Repaint keeps its scale
    assert chrome.eval("document.querySelector('#str-help').textContent").startswith("Low keeps")
    assert chrome.errors() == []


def test_redevelop_keeps_an_older_rework_strength_below_the_new_scale(start, chrome):
    srv = start()  # a Rework made at 0.50 comes back at 0.50, not pushed up to 0.70
    pic = paths.default_output_dir() / "latentroom-test.png"  # the starting picture, in Recent
    out = paths.default_output_dir() / "latentroom-oil.png"
    Image.new("RGB", (96, 64), "gray").save(out)
    req = JobRequest(kind="img2img", model_id="std-q4", init_image=str(pic), strength=0.5)
    old = history.add(req, out, 2)
    chrome.open(srv.url + "#library", *DESK)
    chrome.wait(f"!!document.querySelector('#lib-body .shot[data-id=\"{old.id}\"]')", 10)
    chrome.eval(f"document.querySelector('#lib-body .shot[data-id=\"{old.id}\"]').click()")
    chrome.wait("!!document.querySelector('#lib-panel [data-reuse]')", 10)
    chrome.eval("document.querySelector('#lib-panel [data-reuse]').click()")
    chrome.wait("document.querySelector('#str-v')?.checkVisibility()", 10)
    assert chrome.eval(STRENGTH) == ["0.5", "0.95", "0.5", "0.50"]
    assert chrome.errors() == []


def test_library_names_the_kinds_as_develop_does(start, chrome):
    """Library said 'Picture to picture', 'Paint over part', 'Enhance', 'Face fix'."""
    srv = start()
    src = str(paths.default_output_dir() / "latentroom-test.png")
    names = {"img2img": "Rework", "inpaint": "Repaint a part", "enhance": "Add detail"}
    names["facefix"] = "Fix faces"
    ids = {}
    for seed, kind in enumerate(names, 2):
        pic = paths.default_output_dir() / f"latentroom-{kind}.png"
        Image.new("RGB", (96, 64), "gray").save(pic)
        req = JobRequest(kind=kind, model_id="std-q4", prompt="", init_image=src)
        ids[kind] = history.add(req, pic, seed).id
    chrome.open(srv.url + "#library", *DESK)
    label = "document.querySelector('#lib-body .shot[data-id=\"{}\"]')?.ariaLabel"
    chrome.wait(label.format(ids["facefix"]), 10)
    for kind, name in names.items():
        assert chrome.eval(label.format(ids[kind])).startswith(f"#{ids[kind]:04d}, {name}")
    chrome.eval(f"document.querySelector('#lib-body .shot[data-id=\"{ids['inpaint']}\"]').click()")
    meta = "document.querySelector('#lib-panel .lp-meta')?.textContent || ''"
    chrome.wait(f"({meta}).endsWith('Repaint a part')", 10, show=meta)
    assert chrome.errors() == []


# JS: the Library panel's picture as [url path, natural width], once it has loaded
PANEL_IMG = """(() => { const i = document.querySelector('#lib-panel .lp-img img');
  return i?.complete && i.naturalWidth ? [new URL(i.src).pathname, i.naturalWidth] : null; })()"""


def test_the_library_panel_shows_a_screen_sized_preview(start, chrome):
    """The panel loaded the full PNG (~5k px after an upscale) on every arrow key."""
    srv = start()
    ids = []
    for seed in (2, 3):
        pic = paths.default_output_dir() / f"latentroom-big-{seed}.png"
        Image.new("RGB", (2600, 1600), "gray").save(pic)
        req = JobRequest(model_id="std-q4", prompt="a lighthouse at dusk")
        ids.append(history.add(req, pic, seed).id)
    chrome.open(srv.url + "#library", *DESK)
    cell = 'document.querySelector(`#lib-body .shot[data-id="{}"]`)'
    chrome.wait(f"!!{cell.format(ids[1])}", 10)
    chrome.eval(f"{cell.format(ids[1])}.click()")
    chrome.wait(PANEL_IMG, 10, show=PANEL_IMG)
    assert chrome.eval(PANEL_IMG) == [f"/api/history/{ids[1]}/preview", 1280]
    chrome.eval(f"{cell.format(ids[1])}.focus()")
    key = "new KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true})"
    chrome.eval(f"document.activeElement.dispatchEvent({key})")  # the next one: the older
    want = [f"/api/history/{ids[0]}/preview", 1280]
    chrome.wait(f"JSON.stringify({PANEL_IMG}) === '{json.dumps(want, separators=(',', ':'))}'", 10)
    shot(chrome, "t-library-preview-1280x800")
    assert chrome.errors() == []


# JS: every visible faint separator under 4.5:1 and every enabled field border under 3:1 against
# the colours really behind it (WCAG 2 luminance; see-through backgrounds blended down)
LOW_CONTRAST = """(() => {
  const rgba = s => { const v = (s.match(/[\\d.]+/g) || []).map(Number); return [v[0], v[1], v[2], v[3] ?? 1]; };
  const behind = el => { const ls = []; for (let e = el; e; e = e.parentElement) {
      const c = rgba(getComputedStyle(e).backgroundColor); if (c[3] > 0) ls.push(c); if (c[3] >= 1) break; }
    return ls.reverse().reduce((b, c) => b.map((x, i) => c[i] * c[3] + x * (1 - c[3])), [0, 0, 0]); };
  const lum = c => { const l = c.map(x => x / 255).map(x => x <= .04045 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4);
    return .2126 * l[0] + .7152 * l[1] + .0722 * l[2]; };
  const ratio = (a, b) => { const [h, l] = [lum(a), lum(b)].sort((x, y) => y - x); return (h + .05) / (l + .05); };
  const shown = el => el.checkVisibility() && el.getClientRects().length;
  const low = [], seen = { sep: 0, field: 0 };
  for (const el of document.querySelectorAll('.sep')) if (shown(el)) { seen.sep++;
    const r = ratio(rgba(getComputedStyle(el).color), behind(el)); if (r < 4.5) low.push('sep ' + r.toFixed(2)); }
  for (const el of document.querySelectorAll('.input, .textarea, .select')) if (shown(el) && !el.disabled) { seen.field++;
    const b = rgba(getComputedStyle(el).borderTopColor);
    const r = Math.min(ratio(b, behind(el)), ratio(b, behind(el.parentElement)));
    if (r < 3) low.push(`#${el.id} border ${r.toFixed(2)}`); }
  return { low, seen };
})()"""  # noqa: E501


def test_faint_text_and_field_borders_are_readable(start, chrome):
    """text-faint was 3.48:1 and field borders 1.3:1."""
    srv = start()
    chrome.open(srv.url, *DESK)
    seen = {"sep": 0, "field": 0}
    for screen in ("develop", "library", "models", "settings"):
        chrome.eval(f"location.hash = '#{screen}'")
        chrome.wait(visible(screen), 10)
        chrome.eval(SETTLE)
        got = chrome.eval(LOW_CONTRAST)
        assert got["low"] == [], (screen, got)
        seen = {k: seen[k] + got["seen"][k] for k in seen}
    assert seen["sep"] and seen["field"], seen  # it measured something
    shot(chrome, "t-contrast-settings-1280x800")


TITLE = "document.querySelector('#wiz-title')?.textContent"
ROWS = "[...document.querySelectorAll('.wbody .srow .n')].map(n => n.textContent)"


SIZES = """import('/ui.js').then(m => [m.gb(34359738368), m.gb(12282 * 2 ** 20), m.gb(0),
  m.sizeGB(5 * 2 ** 30), m.sizeGB(2 ** 20), m.sizeGB(0), m.sizeGB(undefined)])"""


def test_the_one_size_formatter_reads_gb_as_windows_does(start, chrome):
    """1 GB = 1024^3 bytes, as Task Manager and Explorer show: 32 GB of RAM and a 12 GB
    card (12282 MiB) read 32 and 12 (were 34.4 and 12.9); unknown or 0 file sizes show nothing."""
    chrome.open(start().url, *DESK)
    assert chrome.eval(SIZES) == ["32", "12", "0", "5.00 GB", "0.01 GB", "", ""]


def test_wizard_models_and_settings_say_32_gb_and_12_gb(start, chrome, monkeypatch):
    """On a PC Task Manager shows with 32 GB of RAM and a 12 GB RTX 4070 SUPER (12282 MiB),
    the wizard's first step, Models and Settings say 32 GB and 12 GB, never 34.4 / 12.9; the
    video memory limit offers only limits below the card (10 GB tops: 12 would be no limit)."""
    gpu = hardware.Gpu("NVIDIA GeForce RTX 4070 SUPER", 12282 << 20, 11071 << 20)
    hw = replace(fake_catalogue.HW, gpu=gpu, ram_total=32 << 30, ram_free=20 << 30)
    monkeypatch.setattr(fake_catalogue, "HW", hw)  # install_stub's detect() reads it per call
    text = "document.querySelector('#screen-{}').innerText"
    chrome.open(srv := start(fresh=True).url, *DESK)
    chrome.wait(f"{TITLE} === 'Welcome to Latentroom'", show=TITLE)
    wiz = chrome.eval(text.format("wizard"))
    assert "12 GB video memory" in wiz and "\n32 GB" in wiz, wiz
    shot(chrome, "j015-wizard-pc")
    s = settings.load()
    s.first_run_done = True  # setup done: the app opens the screens instead of the wizard
    settings.save(s)
    chrome.open(srv + "#models", *DESK)
    note = "!!document.querySelector('#fit-note')?.textContent"
    chrome.wait(visible("models") + " && " + note, show=LOADED)
    chrome.wait(text.format("models") + ".includes(' / 32 GB')", show=text.format("models"))
    models = chrome.eval(text.format("models"))
    assert "1.2 / 12 GB" in models and "12 / 32 GB" in models and "12.9" not in models, models
    assert "(RTX 4070 SUPER, 12 GB)" in models
    shot(chrome, "j015-models")
    chrome.open(srv + "#settings", *DESK)
    chrome.wait("!!document.querySelector('#set-vram option')", show=LOADED)
    limits = chrome.eval("[...document.querySelectorAll('#set-vram option')].map(o => o.text)")
    assert limits == ["Automatic", "4 GB", "6 GB", "8 GB", "10 GB"]
    shot(chrome, "j015-settings")


def test_wizard_warns_on_a_card_without_rtx_and_a_risky_data_folder(start, chrome, monkeypatch):
    """A GTX card and a FAT32 data folder each get one warning line; Continue stays open."""
    gpu = hardware.Gpu("NVIDIA GeForce GTX 1660 SUPER", 6 << 30, 5 << 30)
    hw = replace(fake_catalogue.HW, gpu=gpu, warning=hardware.NOT_RTX)
    monkeypatch.setattr(fake_catalogue, "HW", hw)  # install_stub's detect() reads it per call
    monkeypatch.setattr(server, "data_warnings", lambda folder: [server.FAT32])
    warns = "[...document.querySelectorAll('#screen-wizard .warnline')].map(w => w.textContent)"
    chrome.open(start(fresh=True).url, *DESK)
    chrome.wait(f"{TITLE} === 'Welcome to Latentroom'", show=TITLE)
    assert chrome.eval(warns) == [hardware.NOT_RTX]
    left = "document.querySelector('#screen-wizard {}').getBoundingClientRect().left"
    x = chrome.eval(left.format(".verdict"))
    assert chrome.eval(left.format(".warnline")) == x  # under the verdict, not centred
    shot(chrome, "pc-guards-wizard-gtx")
    chrome.eval("document.querySelector('[data-w-next]').click()")
    chrome.wait(f"{TITLE} === 'Where should models go?'", show=TITLE)
    assert chrome.eval(warns) == [server.FAT32]
    assert chrome.eval("document.querySelector('[data-w-next]').disabled") is False
    shot(chrome, "pc-guards-wizard-fat32")


def test_settings_does_not_wait_for_the_tailscale_check(start, chrome, monkeypatch):
    """/api/phone runs tailscale.exe (~0.4 s cold). Settings draws without it; the
    phone row shows a disabled switch until it answers, then its real state."""
    go = threading.Event()

    def slow():
        go.wait(10)
        return None, phone.NO_TAILSCALE

    monkeypatch.setattr(phone, "tailscale_ip", slow)
    row = "document.querySelector('#set-phone').innerText"
    chrome.open(start().url + "#settings", *DESK)
    try:
        chrome.wait("!!document.querySelector('#set-vram option')", show=LOADED)
        assert chrome.eval("document.querySelector('#ph-switch').disabled") is True
        assert "Tailscale address" not in chrome.eval(row)
    finally:
        go.set()
    chrome.wait(f"{row}.includes('No Tailscale address found')", show=row)
    assert chrome.eval("!!document.querySelector('[data-ph-check]')")
    assert chrome.errors() == []


def test_check_for_updates_asks_github_only_when_pressed(start, chrome, monkeypatch):
    """Opening Settings asks nothing; each press asks once (button disabled
    meanwhile) and says what came back. The link is the REPO one, never GitHub's html_url."""
    answers, go = [], threading.Event()
    replies = [
        {"tag_name": "v9.0.0", "html_url": "https://evil.example"},
        urllib.error.URLError("no network"),
        {"tag_name": f"v{__version__}"},
        urllib.error.HTTPError(update.URL, 404, "Not Found", {}, io.BytesIO(b"{}")),
    ]

    def urlopen(rq, timeout):
        go.wait(10)
        answers.append(rq.full_url)
        a = replies[len(answers) - 1]
        if isinstance(a, Exception):
            raise a
        return io.BytesIO(json.dumps(a).encode())

    monkeypatch.setattr(update.urllib.request, "urlopen", urlopen)
    line, btn = (
        "document.querySelector('#upd-line')?.innerText",
        "document.querySelector('[data-update]')",
    )
    chrome.open(start().url + "#settings", *DESK)
    chrome.wait(f"!!{btn} && !!document.querySelector('#set-vram option')", show=LOADED)
    assert answers == [] and chrome.eval(line) is None  # Settings opened: GitHub not asked
    chrome.eval(f"{btn}.click()")
    chrome.wait(f"{btn}.disabled", show=f"{btn}.outerHTML")
    go.set()
    rel = f"{REPO}/releases"
    chrome.wait(
        f"{line} === {json.dumps(f'Version 9.0.0 is available. Get the new installer at {rel}.')}",
        show=line,
    )
    assert chrome.eval("document.querySelector('#upd-line a').href") == f"https://{rel}"
    assert chrome.eval(f"!{btn}.disabled && document.activeElement === {btn}")
    shot(chrome, "update-newer")
    chrome.eval(f"{btn}.click()")
    chrome.wait(f"{line} === {json.dumps(update.OFFLINE)}", show=line)
    shot(chrome, "update-offline")
    chrome.eval(f"{btn}.click()")
    chrome.wait(f"{line} === 'You have the latest version.'", show=line)
    shot(chrome, "update-latest")
    chrome.eval(f"{btn}.click()")  # a 404 (private/renamed repo, no release) is unknown
    nothing = "Could not find a published version on GitHub, so Latentroom cannot tell whether a newer one exists."  # noqa: E501
    chrome.wait(f"{line} === {json.dumps(nothing)}", show=line)
    shot(chrome, "update-unknown")
    assert len(answers) == 4
    fail = "log: Failed to load resource: the server responded with a status of 502 (Bad Gateway)"
    assert chrome.errors() == [
        f"{fail} {chrome.eval('location.origin')}/api/update"
    ]  # the offline one


def test_wizard_pick_step_shows_no_time(start, chrome, monkeypatch):
    """Step 3 shows no time under a model that fits (a time from one card is wrong for another
    with the same video memory), and the plain Fast help with no speed-up factor (the stub
    card leaves 15 GiB)."""
    turbo = replace(fake_catalogue.MODELS["turbo-q4"], family="turbo")  # as in the real catalogue
    monkeypatch.setitem(fake_catalogue.MODELS, "turbo-q4", turbo)
    chrome.open(start(fresh=True).url, *DESK)
    for title in ("Welcome to Latentroom", "Where should models go?"):
        chrome.wait(f"{TITLE} === {title!r}", show=TITLE)
        chrome.eval("document.querySelector('[data-w-next]').click()")
    chrome.wait(f"{TITLE} === 'Pick a model'", show=TITLE)
    chrome.eval("document.querySelector('[data-w-more]').click()")  # the Fast level is in there
    chrome.wait("document.querySelector('[data-w-more]').ariaExpanded === 'true'")
    shot(chrome, "j022a-wizard-pick")
    assert chrome.eval("document.querySelectorAll('.wbody .slow').length") == 0  # both fit fully
    page = chrome.eval("document.querySelector('.wbody').textContent")
    assert not any(w in page for w in ("second", "minute", "per picture")), page
    fast = chrome.eval("document.querySelector('.wgh .help').textContent")
    assert fast == "Qwen-Image 2.1 Turbo. Faster, a bit less detail. 8 steps.", fast
    assert "room for pictures" not in chrome.eval(SUB)  # a card that runs 1024 x 1024
    assert chrome.errors() == []


SUB = "document.querySelector('.wbody .sub').textContent"


def test_wizard_pick_step_names_the_model_family(start, chrome):
    """Step 3 named no model. The Standard | Uncensored switch says
    Qwen-Image 2.1 for each side (the Fast group says Qwen-Image 2.1 Turbo: test above)."""
    chrome.open(start(fresh=True).url, *DESK)
    for title in ("Welcome to Latentroom", "Where should models go?"):
        chrome.wait(f"{TITLE} === {title!r}", show=TITLE)
        chrome.eval("document.querySelector('[data-w-next]').click()")
    chrome.wait(f"{TITLE} === 'Pick a model'", show=TITLE)
    wgate = "document.querySelector('.wgate').textContent"
    assert RULE in chrome.eval(wgate)  # the rule, at the 18+ question
    assert chrome.eval("document.querySelector('#w-licence').textContent") == QWEN_LIC  # B3
    assert chrome.eval("document.querySelector('#w-licence a.ext').href") == QWEN_URL
    shot(chrome, "product-b1-wizard-gate")
    chrome.eval("document.querySelector('[data-w-adult]').click()")  # shows the switch
    side = "document.querySelector('.wside > .help')?.textContent"
    chrome.wait(f"{side} === 'Qwen-Image 2.1, the official model'", show=side)
    chrome.eval("document.querySelector('[data-w-side=\"uncensored\"]').click()")
    chrome.wait(f"{side} === 'Qwen-Image 2.1, community model'", show=side)
    shot(chrome, "ac1-wizard-pick-names")
    assert chrome.errors() == []


def test_models_screen_names_the_model_family_like_the_wizard(start, chrome, monkeypatch):
    """The Models table had no family name. Its headers and a Fast row now say the
    same words as wizard step 3 (models.js SIDE_HELP + rowHelp, used by both screens)."""
    turbo = replace(fake_catalogue.MODELS["turbo-q4"], family="turbo")  # as in the real catalogue
    monkeypatch.setitem(fake_catalogue.MODELS, "turbo-q4", turbo)
    chrome.open(start().url + "#models", *DESK)
    texts = "[...document.querySelectorAll('#pairs %s .help')].map(e => e.textContent)"
    chrome.wait(f"{texts % '.qc'}.length > 0", show=texts % ".qc")
    shot(chrome, "ac1-models-names")
    assert chrome.eval(texts % ".colh") == [
        "Qwen-Image 2.1, the official model", "Qwen-Image 2.1, community model",
    ]  # fmt: skip
    rows = chrome.eval(texts % ".qc")
    assert [r for r in rows if r.startswith("Qwen-Image 2.1 Turbo. ")], rows  # the Fast row
    assert chrome.errors() == []


MAXIMISED, MIN_WINDOW = (1920, 1000), (1024, 700)  # 1920 x 1080 less taskbar + title; app.py
CARD = "document.querySelector('#hw .card:nth-child(%d) %s')"  # 1 Graphics card ... 4 Engine


@pytest.mark.parametrize("size", [DESK, MAXIMISED, MIN_WINDOW], ids=["1280", "max", "min"])
def test_models_engine_card_shows_the_whole_engine_version(start, chrome, size):
    """At 1280 px the Engine card cut "master-994-e779d4f" with an ellipsis. A version
    is never cut: it wraps at its hyphens, in the same mono-lg as the other cards' values."""
    big = CARD % (4, ".big")
    chrome.open(start().url + "#models", *size)
    chrome.wait(f"{big}?.textContent === {install.ENGINE_RELEASE!r}", show=f"{big}?.textContent")
    shot(chrome, f"ac1-models-engine-{size[0]}x{size[1]}")
    assert chrome.eval(f"{big}.scrollWidth <= {big}.clientWidth"), "the version is cut"
    assert chrome.errors() == []


# JS: card widths (px) at which a separator starts a line in the foot `f`, or "driver" and its
# number end up on two lines; the foot is narrowed step by step, so any GPU name length is covered
SEP_FIRST = """(f => {
  const bad = [], rects = (t, i, j) => { const r = document.createRange();
    r.setStart(t, i); r.setEnd(t, j); return r.getClientRects(); };
  for (let w = 90; w <= 400; w++) {  // 90 px: the narrowest that still holds "driver 581.42"
    f.style.width = w + 'px';
    const left = f.getBoundingClientRect().left;
    for (const s of f.querySelectorAll('.sep')) {
      const t = s.firstChild, i = t.data.indexOf('·');
      if (rects(t, i, i + 1)[0].left - left < 1) bad.push(w);  // the dot opens a line
    }
    const d = [...f.childNodes].find(t => t.data?.includes('driver'));
    if (rects(d, d.data.indexOf('driver'), d.data.length).length !== 1) bad.push(w);
  }
  f.style.width = '';
  return bad;
})(%s)"""


def test_models_card_feet_never_start_a_line_with_the_separator(start, chrome, monkeypatch):
    """At 1280 px the Graphics card foot wrapped to "· video memory in use" alone on
    line 2. That label now comes first, under the bar and the number it describes; a separator
    (ui.js SEP, every screen) ends a line, never starts one, and "driver" keeps its number."""
    gpu = hardware.Gpu("NVIDIA GeForce RTX 4070 SUPER", 12282 << 20, 11071 << 20)
    hw = replace(fake_catalogue.HW, gpu=gpu, driver="581.42")
    monkeypatch.setattr(fake_catalogue, "HW", hw)  # install_stub's detect() reads it per call
    foot = CARD % (1, ".foot")
    chrome.open(start().url + "#models", *DESK)
    chrome.wait(f"{foot}?.textContent.includes('RTX 4070 SUPER')", show=LOADED)
    shot(chrome, "ac1-models-gpu-foot-1280x800")
    assert chrome.eval(f"{foot}.innerText").startswith("video memory in use\n")
    assert chrome.eval(SEP_FIRST % foot) == []
    assert chrome.errors() == []


def test_wizard_proposes_a_smaller_size_on_a_small_card(start, chrome):
    """A card that runs nothing at 1024 x 1024 still gets a pick in
    step 3, the fits at the size it runs at, one plain sentence, and Develop at that size."""
    chrome.open(start(fresh=True, size=768).url, *DESK)
    for title in ("Welcome to Latentroom", "Where should models go?"):
        chrome.wait(f"{TITLE} === {title!r}", show=TITLE)
        chrome.eval("document.querySelector('[data-w-next]').click()")
    chrome.wait(f"{TITLE} === 'Pick a model'", show=TITLE)
    shot(chrome, "j023-wizard-small-card")
    assert chrome.eval(SUB).endswith(
        " The graphics card in this PC has room for pictures up to 768 × 768, so Develop starts"
        " at that size. You can change it there."
    )
    assert chrome.eval(f"{PICKED}?.value") == "std-q4"
    assert chrome.eval("document.querySelectorAll('.wbody :is(.wtile, .wrow)').length") == 2  # both
    sizes = "[document.querySelector('#w').value, document.querySelector('#h').value]"
    assert chrome.eval(sizes) == ["768", "768"]  # Develop starts at that size
    assert chrome.errors() == []


PICKED = "document.querySelector('.wbody input[name=wpick]:checked')"
LINES = ".map(e => e.innerText.split(/\\n+/))"  # each element's lines of text
TILES = "[...document.querySelectorAll('.wtile')]" + LINES
SIDE = "document.querySelector('[data-w-side=\"%s\"]')"


def _pick_step(start, chrome, monkeypatch, slow=()):
    """Step 3 with Balanced (both sides), Light (no uncensored file) and one Fast level; the
    models in `slow` are 2x slower (partial GPU offload), the rest Full GPU."""
    turbo = replace(fake_catalogue.MODELS["turbo-q4"], family="turbo")  # as in the real catalogue
    monkeypatch.setitem(fake_catalogue.MODELS, "turbo-q4", turbo)
    srv = start(fresh=True)
    m = fake_catalogue.MODELS
    pairs = [
        catalogue.ModelPair("q4", "Balanced", m["std-q4"], m["unc-q4"]),
        catalogue.ModelPair("q3", "Light", m["plain-q4"], None),
        catalogue.ModelPair("turbo-q4", "Fast", m["turbo-q4"], None, True),
    ]
    monkeypatch.setattr(catalogue, "load", lambda: pairs)  # recommend (stub): the first, Balanced

    def fit(model, hw, width=1024, height=1024, refs=0, lora_bytes=0):
        label, slowdown = ("partial", 2.0) if model.id in slow else ("full_gpu", 1.0)
        return catalogue.Fit(label, 0, 0, slowdown, "t")

    monkeypatch.setattr(catalogue, "fit", fit)
    chrome.open(srv.url, *DESK)
    for title in ("Welcome to Latentroom", "Where should models go?"):
        chrome.wait(f"{TITLE} === {title!r}", show=TITLE)
        chrome.eval("document.querySelector('[data-w-next]').click()")
    chrome.wait(f"{TITLE} === 'Pick a model'", show=TITLE)


def test_wizard_uncensored_switch_keeps_the_level_and_recommended(start, chrome, monkeypatch):
    """One Standard | Uncensored switch above the tiles. Flipping it keeps the picked level when
    it has a file on that side, and Recommended; a level with no uncensored file stays in its
    place, disabled, with the reason. Left/Right flip it from the keyboard."""
    _pick_step(start, chrome, monkeypatch)
    assert chrome.eval("document.querySelector('.wside')") is None  # the 18+ gate first, as before
    chrome.eval("document.querySelector('[data-w-adult]').click()")
    std, unc = SIDE % "standard", SIDE % "uncensored"
    chrome.wait(f"{std}?.ariaChecked === 'true'", show=TILES)
    assert chrome.eval(f"{PICKED}.value") == "std-q4"
    assert chrome.eval(TILES) == [
        ["Recommended", "Balanced", "Good detail. The usual pick."],
        ["Light", "For smaller cards. Less detail than Balanced."],
    ]  # fmt: skip
    chrome.eval(f"{unc}.click()")
    chrome.wait(f"{unc}.ariaChecked === 'true' && document.activeElement === {unc}", show=TILES)
    shot(chrome, "picker-uncensored-1280x800")
    assert chrome.eval(f"{PICKED}.value") == "unc-q4"  # the same level, its uncensored file
    assert chrome.eval(TILES) == [
        ["Recommended", "Balanced", "Good detail. The usual pick."],
        ["Light", "No uncensored file at this level."],
    ]  # fmt: skip
    off = "document.querySelector('.wtile.off')"
    assert chrome.eval(f"{off}.ariaDisabled") == "true"
    assert chrome.eval(f"{off}.querySelector('input')") is None  # nothing to pick there
    # the Fast level has no uncensored file either: no More models, one line instead
    assert chrome.eval("document.querySelector('[data-w-more]')") is None
    page = chrome.eval("document.querySelector('.wbody').innerText")
    assert "Uncensored is not available for the Fast levels." in page, page
    key = "new KeyboardEvent('keydown', {key: 'ArrowLeft', bubbles: true})"
    chrome.eval(f"{unc}.dispatchEvent({key})")
    chrome.wait(f"{std}.ariaChecked === 'true' && document.activeElement === {std}")
    assert chrome.eval(f"{PICKED}.value") == "std-q4"
    assert chrome.eval(f"[{std}.tabIndex, {unc}.tabIndex]") == [0, -1]  # one tab stop
    assert chrome.errors() == []


def test_wizard_more_models_and_details_on_tiles_and_rows(start, chrome, monkeypatch):
    """The Fast level is under More models, in its own group; a slow row says so. One Details
    toggle shows the real model, quant, file size and fit on every tile and row."""
    _pick_step(start, chrome, monkeypatch, slow={"turbo-q4"})
    more = "document.querySelector('[data-w-more]')"
    assert chrome.eval(f"[{more}.ariaExpanded, {more}.textContent]") == ["false", "More models 1"]
    assert chrome.eval("document.querySelector('#w-more').hidden") is True
    assert chrome.eval("document.querySelectorAll('.wbody .det').length") == 0
    chrome.eval(f"{more}.click()")
    chrome.wait(f"{more}.ariaExpanded === 'true' && document.activeElement === {more}")
    rows = "[...document.querySelectorAll('.wlist > *')]" + LINES
    assert chrome.eval(rows) == [
        ["Fast", "Qwen-Image 2.1 Turbo. Faster, a bit less detail. 8 steps."],
        ["Fast", "Works, but slower."],
    ]  # fmt: skip
    det = "document.querySelector('[data-w-det]')"
    chrome.eval(f"{det}.click()")
    chrome.wait(f"{det}.ariaPressed === 'true' && document.activeElement === {det}")
    shot(chrome, "picker-details-1280x800")
    dets = "[...document.querySelectorAll('.wbody .det')]" + LINES
    assert chrome.eval(dets) == [
        ["Qwen-Image 2.1", "Q4_K · 0.01 GB", "Full GPU"],
        ["Qwen-Image 2.1", "Q4_K · 0.01 GB", "Full GPU"],
        ["Qwen-Image 2.1 Turbo", "Q4_K · 0.01 GB", "Partial GPU offload"],
    ]  # fmt: skip
    assert chrome.eval(f"{more}.ariaExpanded") == "true"  # Details keeps More models open
    chrome.eval("document.querySelector('.wrow input').click()")
    assert chrome.eval(f"{PICKED}.value") == "turbo-q4"
    assert chrome.eval("document.querySelector('[data-w-next]').disabled") is False
    assert chrome.errors() == []


@pytest.fixture
def setup_run(start, chrome, monkeypatch):
    """setup_run(installed=) -> the wizard on a PC with only `installed` on disk, at step 4; every
    stub file is 1 GB; downloads are recorded (by file id), never fetched."""
    for k, f in fake_catalogue.FILES.items():
        monkeypatch.setitem(fake_catalogue.FILES, k, replace(f, size=1 << 30))
    for k, m in fake_catalogue.MODELS.items():
        monkeypatch.setitem(fake_catalogue.MODELS, k, replace(m, file=fake_catalogue.FILES[k]))
    got = {}

    def begin(item):
        got[item.id] = downloads.DownloadState(
            item.id, item.name, "queued", 0, item.size, 0.0, None, None
        )  # fmt: skip
        return got[item.id]

    def go(installed=frozenset(), done=frozenset()):
        """installed: file ids on disk; done: file ids with an old finished download."""
        for i in done:
            f = fake_catalogue.FILES[i]
            got[i] = downloads.DownloadState(i, f.name, "done", f.size, f.size, 0.0, None, None)
        srv = start(fresh=True)
        monkeypatch.setattr(install, "installed", lambda: set(installed))
        exe = Path("sd-cli.exe") if "engine" in installed else None
        monkeypatch.setattr(install, "engine_exe", lambda: exe)
        monkeypatch.setattr(downloads, "start", begin)
        monkeypatch.setattr(downloads, "progress", lambda: list(got.values()))
        chrome.open(srv.url, *DESK)
        for title in ("Welcome to Latentroom", "Where should models go?", "Pick a model"):
            chrome.wait(f"{TITLE} === {title!r}", show=TITLE)
            chrome.eval("document.querySelector('[data-w-next]').click()")
        chrome.wait(f"{TITLE} !== 'Pick a model'", show=TITLE)
        return got

    return go


def test_wizard_has_a_way_on_when_no_model_fits(start, chrome):
    """No model fits at any size -> step 3 had only a disabled Download. Now it says
    so and offers Skip setup, which ends setup and opens Develop."""
    chrome.open(start(fresh=True, size=0).url, *DESK)
    for title in ("Welcome to Latentroom", "Where should models go?"):
        chrome.wait(f"{TITLE} === {title!r}", show=TITLE)
        chrome.eval("document.querySelector('[data-w-next]').click()")
    chrome.wait(f"{TITLE} === 'Pick a model'", show=TITLE)
    shot(chrome, "t-wizard-nothing-fits-1280x800")
    assert "No model fits this PC" in chrome.eval("document.querySelector('.wbody').innerText")
    chrome.eval("document.querySelector('[data-w-skip]').click()")
    chrome.wait(visible("develop"), 5)
    assert settings.load().first_run_done
    assert chrome.errors() == []


def test_back_to_another_model_pauses_the_first_download(setup_run, chrome, monkeypatch):
    """Back from step 4 and a different pick left the first model downloading unseen."""
    paused = []
    got = setup_run()  # step 4, picked std-q4
    monkeypatch.setattr(downloads, "pause", lambda i: (paused.append(i), got[i])[1])
    chrome.eval("document.querySelector('[data-w-back]').click()")
    chrome.wait(f"{TITLE} === 'Pick a model'", show=TITLE)
    chrome.eval("document.querySelector('[data-w-more]').click()")
    chrome.eval("document.querySelector('.wrow input[value=\"turbo-q4\"]').click()")
    chrome.eval("document.querySelector('[data-w-next]').click()")
    chrome.wait(f"{TITLE} !== 'Pick a model'", show=TITLE)
    assert sorted(paused) == ["std-q4", "turbo-lora"]  # a Turbo model takes no add-on
    assert "turbo-q4" in got


def test_setup_counts_the_turbo_add_on_in_the_disk_space_and_the_download(setup_run, chrome):
    """Setup gets the Turbo add-on for a standard model, not for a Turbo model: every stub
    file is 1 GB, so std-q4 needs 7 (engine, 3 parts, upscaler, add-on, model), turbo-q4 6."""
    setup_run()  # step 4, picked std-q4
    chrome.eval("document.querySelector('[data-w-back]').click()")
    chrome.wait(f"{TITLE} === 'Pick a model'", show=TITLE)
    assert chrome.eval("document.querySelector('#w-total').textContent") == "Download: 7.00 GB"
    chrome.eval("document.querySelector('[data-w-back]').click()")
    chrome.wait(f"{TITLE} === 'Where should models go?'", show=TITLE)
    fit = "document.querySelector('.wbody .fit .help').textContent"
    assert chrome.eval(fit).startswith("Setup needs 6.00 GB to 7.00 GB,"), chrome.eval(fit)
    assert chrome.errors() == []


def test_a_restart_mid_setup_goes_back_to_the_download(start, chrome, monkeypatch):
    """Setup always started again at step 1, even with its download half done."""
    for k, f in fake_catalogue.FILES.items():
        monkeypatch.setitem(fake_catalogue.FILES, k, replace(f, size=1 << 30))
    for k, m in fake_catalogue.MODELS.items():
        monkeypatch.setitem(fake_catalogue.MODELS, k, replace(m, file=fake_catalogue.FILES[k]))
    half = downloads.DownloadState("std-q4", "m.gguf", "paused", 1 << 29, 1 << 30, 0.0, None, None)
    srv = start(fresh=True)
    monkeypatch.setattr(install, "installed", set)  # nothing on disk yet
    monkeypatch.setattr(downloads, "progress", lambda: [half])
    chrome.open(srv.url, *DESK)
    chrome.wait(f"{TITLE} === 'Downloading'", 5, show=TITLE)
    chrome.wait("document.querySelector('[data-w-pause]')?.title === 'Resume download'", 5)
    shot(chrome, "t-wizard-resumed-1280x800")


def test_setup_downloads_the_upscaler_and_the_turbo_add_on_too(setup_run, chrome):
    got = setup_run()  # after setup, Upscale ×4 and Turbo work without internet
    chrome.wait("document.querySelectorAll('.wbody .srow').length > 0")
    shot(chrome, "t-wizard-4-1280x800")
    assert {"Upscaler", "Turbo LoRA"} <= set(chrome.eval(ROWS))
    assert {"esrgan-x4", "turbo-lora"} <= set(got)
    assert chrome.errors() == []


def test_setup_rows_come_in_the_models_part_order(setup_run, chrome):
    setup_run()  # the Models part order, not the catalogue's needs order
    chrome.wait("document.querySelectorAll('.wbody .srow').length > 0")
    assert chrome.eval(ROWS) == [
        "Picture engine", "Text encoder", "Vision encoder", "Image decoder (VAE)", "Upscaler",
        "Turbo LoRA", "Balanced model",
    ]  # fmt: skip


SUB = "document.querySelector('.wbody .sub').textContent"
LINE = "document.querySelector('[data-p=line]')?.textContent"


def test_setup_sub_says_the_same_total_as_the_block(setup_run, chrome):
    # one number for both; a file already on disk is in neither,
    # even when an old finished download of it is still listed
    setup_run(installed={"vae"}, done={"vae"})
    chrome.wait(f"{LINE} === 'Waiting · 6.00 GB'", show=LINE)
    assert chrome.eval(SUB).startswith("About 6.00 GB. ")


def test_setup_with_nothing_to_download_is_complete_at_once(setup_run, chrome):
    setup_run(installed=set(fake_catalogue.FILES))  # no "About ." or "Done · "
    chrome.wait(f"{TITLE} === 'Download complete'", show=TITLE)
    assert chrome.eval(LINE) == "Done"
    assert chrome.errors() == []


def test_finishing_the_setup_is_remembered(setup_run, chrome):
    """After the last step the app opens Develop, and opened again it does not
    show the wizard: settings.json says first_run_done."""
    setup_run(installed=set(fake_catalogue.FILES))
    chrome.wait(f"{TITLE} === 'Download complete'", show=TITLE)
    chrome.eval("document.querySelector('[data-w-next]').click()")
    chrome.wait(f"{TITLE} === 'Latentroom is ready'", show=TITLE)  # the fake engine's test picture
    chrome.eval("document.querySelector('[data-w-finish]').click()")
    chrome.wait(visible("develop"), show=LOADED)
    assert settings.load().first_run_done
    chrome.open(chrome.eval("location.origin"), *DESK)  # Latentroom opened again
    chrome.wait(visible("develop"), show=LOADED)
    assert chrome.errors() == []


def test_setup_runs_again_when_the_models_or_the_engine_are_gone(start, chrome, monkeypatch):
    """settings.json says setup is done (an uninstall keeps it), but the uninstall
    deleted the models and the engine, or they were deleted by hand: the app opens the wizard
    again. Models kept (uninstall answered No, or an update): the app opens Develop, as before."""
    srv = start()
    files = set(fake_catalogue.FILES)
    pictures = {i for i, f in fake_catalogue.FILES.items() if f.kind == "diffusion"}
    for gone in ({"engine"}, pictures):
        monkeypatch.setattr(install, "installed", lambda gone=gone: files - gone)
        chrome.open(srv.url, *DESK)
        chrome.wait(f"{TITLE} === 'Welcome to Latentroom'", show=LOADED)
        shot(chrome, "setup-again-" + ("engine" if "engine" in gone else "pictures"))
    monkeypatch.setattr(install, "installed", lambda: files)
    chrome.open(srv.url, *DESK)
    chrome.wait(visible("develop"), show=LOADED)
    assert settings.load().first_run_done
    assert chrome.errors() == []


def ready_step(setup_run, chrome) -> None:
    """The wizard at step 5, the fake engine's test picture done."""
    setup_run(installed=set(fake_catalogue.FILES))
    chrome.wait(f"{TITLE} === 'Download complete'", show=TITLE)
    chrome.eval("document.querySelector('[data-w-next]').click()")
    chrome.wait(f"{TITLE} === 'Latentroom is ready'", show=TITLE)


def test_ready_step_has_no_back(setup_run, chrome):
    """Back on the finished last step was useless; step 4 keeps it."""
    setup_run(installed=set(fake_catalogue.FILES))
    chrome.wait(f"{TITLE} === 'Download complete'", show=TITLE)
    back = "!!document.querySelector('[data-w-back]')"
    assert chrome.eval(back)
    chrome.eval("document.querySelector('[data-w-next]').click()")
    chrome.wait(f"{TITLE} === 'Latentroom is ready'", show=TITLE)
    assert not chrome.eval(back)
    assert chrome.eval("!!document.querySelector('[data-w-finish]:not([disabled])')")


# the test picture tile, the Start using Latentroom button, and whether the wizard scrolls
PIC = """(() => {
  const p = document.querySelector('.wpic').getBoundingClientRect(), s = document.querySelector('#screen-wizard');
  return { w: Math.round(p.width), h: Math.round(p.height), scrolls: s.scrollHeight > s.clientHeight,
    button: Math.round(document.querySelector('[data-w-finish]').getBoundingClientRect().bottom) };
})()"""  # noqa: E501


def test_ready_step_picture_fills_the_free_space(setup_run, chrome):
    """The test picture was a 256 px box with half the page empty below. It is
    square, as big as the window allows, and Start using Latentroom stays on screen without
    scrolling, down to 960 x 640 (smaller than the app's 1024 x 700 minimum window)."""
    ready_step(setup_run, chrome)
    seen = {}
    for w, h in (DESK, (960, 640)):
        chrome.call("Emulation.setDeviceMetricsOverride", width=w, height=h,
                    deviceScaleFactor=1, mobile=False)  # fmt: skip
        seen[h] = box = chrome.eval(PIC)
        shot(chrome, f"ac1-wizard-ready-{w}x{h}")
        assert box["w"] == box["h"] > 256 and box["button"] <= h and not box["scrolls"], box
    assert seen[800]["w"] > seen[640]["w"], seen  # it grows with the window
    assert chrome.errors() == []


# ---------- Enhance prompt: the button by the prompt box ----------
IDEA, BETTER = "un faro al tramonto", "A lighthouse at dusk on a rocky shore, warm light, calm sea."
ENH = """(() => {
  const b = document.querySelector('#enh'), p = document.querySelector('#prompt');
  return { shown: !b.hidden, off: b.getAttribute('aria-disabled') === 'true', why: b.title,
    label: b.textContent, undo: !document.querySelector('#enh-undo').hidden, text: p.value,
    locked: p.readOnly };
})()"""
READY = {"shown": True, "off": False, "why": "", "label": "Enhance prompt", "undo": False}
TOAST = "document.querySelector('#toast.show')?.textContent"
NO_MODEL = "Needs the text encoder (Models tab)."


def write(text: str) -> str:
    """JS: the user types `text` into the prompt box."""
    return f"(p => {{ p.value = {json.dumps(text)}; p.dispatchEvent(new Event('input')); }})(document.querySelector('#prompt'))"  # noqa: E501


@pytest.fixture
def llm(monkeypatch):
    """The text model behind Enhance prompt: answers `answer` once `go` is set (clear it to see
    the progress state); asks = the ideas it got."""
    s = SimpleNamespace(
        go=threading.Event(), asks=[], answer=json.dumps({"rewritten_prompt": BETTER})
    )
    s.go.set()

    def chat(system, user, **sampling):
        s.asks.append(user)
        s.go.wait(15)
        return s.answer

    monkeypatch.setattr(prompt_rewrite, "llama_chat", chat)
    return s


def test_enhance_prompt_replaces_the_text_and_undo_brings_it_back(start, chrome, llm):
    srv = start()
    chrome.open(srv.url, *DESK)
    chrome.wait(visible("develop"))
    chrome.wait(f"{ENH}.why === 'Write an idea first.'", show=ENH)  # the text encoder is on disk
    chrome.eval(write(IDEA))
    assert chrome.eval(ENH) == {**READY, "text": IDEA, "locked": False}
    llm.go.clear()
    chrome.eval("document.querySelector('#enh').click()")
    chrome.wait(f"{ENH}.label === 'Enhancing…'", show=ENH)
    shot(chrome, "t-enhance-running-1280x800")
    busy = chrome.eval(ENH)
    assert (busy["off"], busy["locked"], busy["undo"], busy["text"]) == (True, True, False, IDEA)
    llm.go.set()
    chrome.wait(f"{ENH}.text === {json.dumps(BETTER)}", show=ENH)
    shot(chrome, "t-enhance-done-1280x800")
    assert chrome.eval(ENH) == {**READY, "undo": True, "text": BETTER, "locked": False}
    assert llm.asks == [IDEA]
    chrome.eval("document.querySelector('#enh-undo').click()")  # one click: the idea is back
    assert chrome.eval(ENH) == {**READY, "text": IDEA, "locked": False}
    chrome.eval("document.querySelector('#enh').click()")
    chrome.wait(f"{ENH}.undo", show=ENH)
    chrome.eval(write(BETTER + " Seen from the beach."))  # edited: now it is the user's text
    assert chrome.eval(ENH)["undo"] is False
    assert chrome.errors() == []


READS = "performance.getEntriesByType('resource').filter(e => /api.hardware$/.test(e.name)).length"


def test_the_vram_meter_reads_only_while_a_job_or_enhance_runs(
    start, chrome, llm, monkeypatch, tmp_path
):
    """The top-bar meter reads the card every 2 s while a picture job or Enhance runs,
    once more when it ends, then not at all (no polling while nothing runs)."""
    srv = start()
    gate = tmp_path / "go-on"  # the fake engine holds at step 2 until it exists
    monkeypatch.setenv("FAKE_HOLD", str(gate))
    chrome.open(srv.url, *DESK)
    chrome.wait(visible("develop") + " && !document.querySelector('#go').disabled", show=FIT)
    chrome.wait(f"{ENH}.why === 'Write an idea first.'", show=ENH)

    def quiet():  # no reading for longer than one 2 s period
        n = chrome.eval(READS)
        time.sleep(2.3)
        return chrome.eval(READS) - n

    def live():  # two more readings within ~4 s
        n = chrome.eval(READS)
        chrome.wait(f"{READS} >= {n + 2}", 6, show=READS)

    assert quiet() == 0
    chrome.eval(write(IDEA))
    llm.go.clear()
    chrome.eval("document.querySelector('#enh').click()")
    live()
    llm.go.set()
    chrome.wait(f"{ENH}.text === {json.dumps(BETTER)}", show=ENH)
    time.sleep(2.3)  # the reading after the end
    assert quiet() == 0
    chrome.eval("document.querySelector('#go').click()")
    chrome.wait(f"/Developing/.test({HEAD})", 30, show=HEAD)
    live()
    gate.touch()
    chrome.wait(f"/Done/.test({HEAD})", 30, show=HEAD)
    time.sleep(2.3)
    assert quiet() == 0
    assert chrome.errors() == []


LONG = " ".join([BETTER] * 18)  # 198 words: an Enhanced prompt is ~200
BOX = "(p => [p.offsetHeight, p.scrollHeight > p.clientHeight])(document.querySelector('#prompt'))"


@pytest.mark.parametrize("size", [PHONE, DESK], ids=["390x844", "1280x800"])
def test_the_prompt_box_grows_with_its_text_then_scrolls(start, chrome, llm, size):
    """Min height 112, grows with content to 240, then scrolls. It sat at 112
    (5 lines) for an Enhanced prompt: nothing made it grow."""
    srv = start()
    llm.answer = json.dumps({"rewritten_prompt": LONG})
    chrome.open(srv.url, *size)
    chrome.wait(visible("develop"))
    chrome.wait(f"{ENH}.why === 'Write an idea first.'", show=ENH)
    chrome.eval(write(IDEA))
    assert chrome.eval(BOX) == [112, False]  # a short idea: the minimum
    chrome.eval("document.querySelector('#enh').click()")
    chrome.wait(f"{ENH}.text === {json.dumps(LONG)}", 15, show=ENH)
    chrome.eval("document.querySelector('#prompt').scrollIntoView({ block: 'center' })")
    shot(chrome, f"t-prompt-grows-enhanced-{size[0]}x{size[1]}")
    assert chrome.eval(BOX) == [240, True]  # Enhance filled it: the maximum, then it scrolls
    chrome.eval("document.querySelector('#enh-undo').click()")
    assert chrome.eval(BOX) == [112, False]  # Undo: back to the idea, back to the minimum
    chrome.eval(write("\n".join(["a line"] * 7)))
    assert chrome.eval(BOX) == [7 * 20 + 20 + 2, False]  # typing: 7 lines + padding + border
    neg = "getComputedStyle(document.querySelector('#neg')).fieldSizing"
    assert chrome.eval(neg) == "fixed"  # the negative prompt box stays 64 px (not asked to grow)
    assert chrome.errors() == []


@pytest.mark.parametrize("size", [PHONE, DESK], ids=["390x844", "1280x800"])
def test_enhance_prompt_failure_shows_a_toast_and_keeps_the_text(start, chrome, llm, size):
    srv = start()
    llm.answer = "Sure! Here is a better prompt."  # no JSON: the raw answer is never shown
    chrome.open(srv.url, *size)
    chrome.wait(visible("develop"))
    chrome.wait(f"{ENH}.why === 'Write an idea first.'", show=ENH)
    chrome.eval(write(IDEA))
    chrome.eval("document.querySelector('#enh').click()")
    chrome.wait(f"{TOAST} === {json.dumps(prompt_rewrite.FAILED)}", show=TOAST)
    shot(chrome, f"t-enhance-failed-{size[0]}x{size[1]}")
    assert chrome.eval(ENH) == {**READY, "text": IDEA, "locked": False}
    fail = "log: Failed to load resource: the server responded with a status of 502 (Bad Gateway)"
    assert chrome.errors() == [f"{fail} {srv.url.split('?')[0]}api/prompt/enhance"]


@pytest.mark.parametrize("size", [PHONE, DESK], ids=["390x844", "1280x800"])
def test_enhance_prompt_says_what_to_download_first(start, chrome, llm, monkeypatch, size):
    srv = start()
    monkeypatch.setattr(install, "installed", lambda: set(fake_catalogue.FILES) - {"llm"})
    chrome.open(srv.url, *size)
    chrome.wait(visible("develop"))
    asked = "performance.getEntriesByType('resource').some(e => e.name.endsWith('/api/models'))"
    chrome.wait(asked)
    chrome.eval(SETTLE)
    chrome.eval(write(IDEA))
    off = {**READY, "off": True, "why": NO_MODEL, "text": IDEA, "locked": False}
    assert chrome.eval(ENH) == off
    chrome.eval("document.querySelector('#enh').click()")  # a phone has no tooltips: a tap says it
    assert chrome.eval(TOAST) == NO_MODEL
    shot(chrome, f"t-enhance-no-model-{size[0]}x{size[1]}")
    assert llm.asks == []
    assert chrome.errors() == []


def test_enhance_prompt_waits_for_the_picture_and_is_for_new_pictures(
    start, chrome, llm, monkeypatch, tmp_path
):
    srv = start()
    gate = tmp_path / "go-on"  # the fake engine holds at step 2 until it exists
    monkeypatch.setenv("FAKE_HOLD", str(gate))
    chrome.open(srv.url, *DESK)
    chrome.wait(visible("develop") + " && !document.querySelector('#go').disabled", show=FIT)
    chrome.eval(write(IDEA))
    chrome.wait(f"!{ENH}.off", show=ENH)
    chrome.eval("document.querySelector('#go').click()")
    chrome.wait(f"{ENH}.why === 'Wait for the picture to finish.'", 30, show=ENH)
    shot(chrome, "t-enhance-wait-1280x800")
    chrome.eval("document.querySelector('#enh').click()")
    assert chrome.eval(TOAST) == "Wait for the picture to finish."
    gate.touch()
    chrome.wait(f"/Done/.test({HEAD})", 30, show=HEAD)
    chrome.wait(f"!{ENH}.off", 10, show=ENH)
    assert llm.asks == []
    # the official rewrite prompt is for new pictures: no button for an edit or From a picture
    chrome.eval("document.querySelector('#start [data-m=from]').click()")
    assert chrome.eval(ENH)["shown"] is False
    chrome.eval("document.querySelector('#start [data-m=new]').click()")
    assert chrome.eval(ENH)["shown"] is True
    chrome.eval(ADD_REFS)
    chrome.wait("document.querySelectorAll('#refs .tag').length === 2")
    assert chrome.eval(ENH)["shown"] is False
    assert chrome.errors() == []


# JS: the ring of a thumbnail, as its ::after draws it (computed box-shadow)
RING = "getComputedStyle(document.querySelector('%s'), '::after').boxShadow"
LIB_SEL = ".shot[aria-selected=true] .ph"  # the newest picture: first row, first column
ACCENT = "getComputedStyle(document.body).getPropertyValue('--color-accent')"


def test_selection_rings_are_drawn_inside_the_thumbnail(start, chrome, tmp_path):
    srv = start()  # an outer ring was cut by the sticky day header and the strip's scroll
    pic = paths.default_output_dir() / "latentroom-test-2.png"
    Image.new("RGB", (96, 64), "gray").save(pic)
    req = JobRequest(model_id="std-q4", prompt="a lighthouse at dusk")
    history.add(req, pic, 2)  # seed 2 after the fixture's seed 1: frame 02 of the same batch
    chrome.open(srv.url, *DESK)
    chrome.wait("document.querySelector('#strip .th.sel img')?.complete", 10)
    assert "inset" in chrome.eval(RING % "#strip .th.sel")
    chrome.open(srv.url + "#library", *DESK)
    chrome.wait(f"document.querySelector('{LIB_SEL} img')?.complete")
    chrome.wait(f"/2px inset/.test({RING % LIB_SEL})")
    chrome.eval(SETTLE)
    box = f"document.querySelector('{LIB_SEL}').getBoundingClientRect()"
    x, y = chrome.eval(f"(r => [r.left + r.width / 2, r.top])({box})")
    chrome.screenshot(tmp_path / "lib.png")
    shot(chrome, "t-ring-inside-1280x800")
    red = bytes.fromhex(chrome.eval(ACCENT).strip()[1:])
    px = Image.open(tmp_path / "lib.png").convert("RGB").getpixel((int(x), int(y) + 1))  # top edge
    assert max(abs(a - b) for a, b in zip(px, red, strict=True)) < 24, (px, red.hex())
    assert chrome.errors() == []


# JS: the queued rows in Recent, as [title, status line]
QUEUED = """[...document.querySelectorAll('#recent .rrow.q')]
  .map(r => [r.querySelector('.t').textContent, r.querySelector('.m').textContent])"""
GO = "document.querySelector('#prompt').value = '%s'; document.querySelector('#go').click()"


def test_jobs_added_behind_a_running_one_wait_in_recent_and_cancel_there(
    start, chrome, monkeypatch, tmp_path
):
    srv = start()  # a job added while one ran showed only a 3 s toast
    gate = tmp_path / "go-on"  # the first job holds at step 2, the others wait behind it
    monkeypatch.setenv("FAKE_HOLD", str(gate))
    chrome.open(srv.url, *DESK)
    chrome.wait(visible("develop") + " && !document.querySelector('#go').disabled", show=FIT)
    chrome.eval(GO % "a lighthouse at dusk")
    chrome.wait(f"/Developing/.test({HEAD})", 30, show=HEAD)
    chrome.eval(GO % "a harbour at noon")  # two clicks race to the server: let the 1st arrive
    chrome.wait(f"{QUEUED}.length === 1", show=QUEUED)
    chrome.eval(GO % "a pier at night")
    chrome.wait(f"{QUEUED}.length === 2 && /2nd/.test({QUEUED}[1][1])", show=QUEUED)
    shot(chrome, "t-recent-queued-1280x800")
    assert chrome.eval(QUEUED) == [
        ["a harbour at noon", "queued · next"],
        ["a pier at night", "queued · 2nd"],
    ]
    chrome.eval("document.querySelector('#recent .rrow.q [data-q]').click()")  # the harbour's
    chrome.wait(f"{QUEUED}.length === 1 && /next/.test({QUEUED}[0][1])", show=QUEUED)
    assert chrome.eval(QUEUED) == [["a pier at night", "queued · next"]]  # moved up on the server
    gate.touch()
    chrome.wait(f"/Done/.test({HEAD})", 30, show=HEAD)
    assert chrome.errors() == []


def test_the_eraser_ring_is_dashed_and_the_brush_ring_solid(start, chrome):
    srv = start()  # both rings looked the same, so the Eraser seemed to do nothing
    chrome.open(srv.url, *DESK)
    chrome.wait("!!document.querySelector('#tu-btn')", 10)  # the newest picture, done, in the tray
    chrome.eval("document.querySelector('#tu-btn').click()")
    chrome.eval("document.querySelector('#tmenu [data-a=inpaint]').click()")  # Repaint a part
    chrome.wait("!document.querySelector('#mask').hidden")
    box = "document.querySelector('#mask').getBoundingClientRect()"
    x, y = chrome.eval(f"(r => [r.left + r.width / 2, r.top + r.height / 2])({box})")
    chrome.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)  # the ring follows it
    style = "getComputedStyle(document.querySelector('#ring')).borderTopStyle"
    assert chrome.eval(style) == "solid"
    chrome.eval("document.querySelector('#mtool [data-t=eraser]').click()")
    shot(chrome, "t-eraser-ring-1280x800")
    assert chrome.eval(style) == "dashed"
    chrome.eval("document.querySelector('#mtool [data-t=brush]').click()")
    assert chrome.eval(style) == "solid"
    assert chrome.errors() == []


# JS: every external link on the screen shown (hidden screens keep theirs: Models' licence line),
# as [text, href, target, rel]
EXT = (
    "[...document.querySelectorAll('main:not([hidden]) a.ext')]"
    ".map(a => [a.textContent, a.href, a.target, a.rel])"
)


def _ext(addr: str) -> list[str]:
    return [addr, f"https://{addr}", "_blank", "noopener noreferrer"]


def test_external_addresses_are_links_for_the_system_browser(start, chrome, monkeypatch):
    """The GitHub and NVIDIA addresses were plain text. They are links now, target
    _blank: pywebview hands that to the system browser (test_server checks its setting), a
    phone opens a tab. A drag across the address still selects its text."""
    monkeypatch.setattr(phone, "tailscale_ip", lambda: ("127.0.0.2", ""))  # no Tailscale links
    srv = start()
    chrome.open(srv.url + "#settings", *DESK)
    chrome.wait(f"{EXT}.length === 2", show=EXT)
    assert chrome.eval(EXT) == [_ext(f"{REPO}/issues"), _ext(f"{REPO}/releases")]
    chrome.eval("document.addEventListener('click', e => e.preventDefault(), true)")  # no tab
    box = "document.querySelector('main:not([hidden]) a.ext').getBoundingClientRect()"
    where = f"(r => [r.left + 2, r.right - 2, r.top + r.height / 2])({box})"
    # the Phone row fills in late and moves the link: drag only once it holds still
    last, deadline = None, time.monotonic() + 10
    while (now := chrome.eval(where)) != last and time.monotonic() < deadline:
        last = now
        time.sleep(0.3)
    x0, x1, y = now
    mouse = partial(chrome.call, "Input.dispatchMouseEvent", y=y, button="left")
    mouse(type="mousePressed", x=x0, clickCount=1)
    mouse(type="mouseMoved", x=x1, buttons=1)
    mouse(type="mouseReleased", x=x1, clickCount=1)
    picked = chrome.eval("getSelection().toString()")
    assert len(picked) > 20 and picked in f"{REPO}/issues", picked
    assert chrome.errors() == []


def test_no_tailscale_shows_the_steps_and_both_links(start, chrome, monkeypatch):
    """Without Tailscale the phone row says why, lists 3 steps, links the Tailscale
    download and the step-by-step guide, and keeps Check again."""
    monkeypatch.setattr(phone, "tailscale_ip", lambda: (None, phone.NO_TAILSCALE))
    chrome.open(start().url + "#settings", *DESK)
    row = "document.querySelector('#set-phone')"
    chrome.wait(f"{row}?.querySelectorAll('a.ext').length === 2", show=f"{row}?.innerText")
    links = chrome.eval(
        f"[...{row}.querySelectorAll('a.ext')].map(a => [a.textContent, a.href, a.target, a.rel])"
    )
    assert links == [_ext("tailscale.com/download"), _ext(f"{REPO}/blob/main/docs/PHONE.md")]
    assert chrome.eval(f"{row}.querySelectorAll('.steps li').length") == 3
    assert chrome.eval(f"{row}.querySelector('.warnline').textContent") == phone.NO_TAILSCALE
    assert chrome.eval(f"!!{row}.querySelector('[data-ph-check]')")
    # the warning starts at the left edge of the row text, not centred like Develop's tray line
    assert chrome.eval(
        f"{row}.querySelector('.warnline').getBoundingClientRect().x"
        f" === {row}.querySelector('.n').getBoundingClientRect().x"
    )
    chrome.eval(f"{row}.scrollIntoView({{block: 'center'}})")
    shot(chrome, "phone-no-tailscale")
    assert chrome.errors() == []


def test_the_driver_address_is_a_link_too(start, chrome):
    chrome.open(start(hw="no_nvidia").url, *DESK)  # the problem screen
    chrome.wait(visible("problem"), show=LOADED)
    shot(chrome, "ac1-problem-link")
    assert chrome.eval(EXT) == [_ext("nvidia.com/drivers")]
    assert chrome.errors() == []


def test_the_tab_ring_on_a_thumbnail_is_drawn_inside(start, chrome, tmp_path):
    """The keyboard ring around a Library picture or a strip frame was cut like the
    selection ring. It is drawn inside the picture now, 2px within the red ring."""
    srv = start()
    pic = paths.default_output_dir() / "latentroom-test-2.png"  # a batch of 2: the strip shows it
    Image.new("RGB", (96, 64), "gray").save(pic)
    history.add(JobRequest(model_id="std-q4", prompt="a lighthouse at dusk"), pic, 2)
    chrome.open(srv.url, *DESK)
    chrome.wait("document.querySelector('#strip .th.sel img')?.complete", 10)
    chrome.eval("document.querySelector('#strip .cell').focus()")
    assert chrome.eval("document.activeElement.matches('#strip .cell:focus-visible')")
    after = "(s => [s.outlineStyle, s.outlineOffset])(getComputedStyle(document.querySelector('%s'), '::after'))"  # noqa: E501
    assert chrome.eval(after % "#strip .cell .th") == ["solid", "-4px"]
    assert chrome.eval("getComputedStyle(document.activeElement).outlineStyle") == "none"
    chrome.open(srv.url + "#library", *DESK)
    chrome.wait(f"document.querySelector('{LIB_SEL} img')?.complete")
    chrome.eval("document.querySelector('.shot[aria-selected=true]').focus()")
    chrome.wait(f"{after % LIB_SEL}[0] === 'solid'", show=after % LIB_SEL)
    chrome.eval(SETTLE)
    rect = f"document.querySelector('{LIB_SEL}').getBoundingClientRect()"
    x, y = chrome.eval(f"(r => [r.left + r.width / 2, r.top])({rect})")
    chrome.screenshot(tmp_path / "lib.png")
    shot(chrome, "ac1-tab-ring-inside-1280x800")
    img = Image.open(tmp_path / "lib.png").convert("RGB")
    for dy, var in ((1, ACCENT), (3, ACCENT.replace("accent", "focus"))):  # red edge, then ring
        want = bytes.fromhex(chrome.eval(var).strip()[1:])
        px = img.getpixel((int(x), int(y) + dy))
        assert max(abs(a - b) for a, b in zip(px, want, strict=True)) < 24, (dy, px, want.hex())
    assert chrome.errors() == []
