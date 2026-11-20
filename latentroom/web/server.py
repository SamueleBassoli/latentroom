"""Local HTTP server for the UI (stdlib ThreadingHTTPServer, 127.0.0.1 only).
The HTTP API it serves is the _ROUTES table below.

Security, in the order a request meets it:
  Host must be exactly our address (DNS rebinding) -> Origin, when sent, must be ours, and
  Sec-Fetch-Site must be same-origin/none (CSRF, other sites on 127.0.0.1) -> /api/ needs the
  per-launch token cookie (the window opens /?token=..., gets an HttpOnly SameSite=Strict
  cookie) -> POST bodies are JSON only (uploads: PNG/JPEG/WebP) with size caps.
Static files come only from web/static/. Request lines are never logged (token, search text).
"""

import contextlib
import hmac
import io
import json
import logging
import os
import platform
import re
import secrets
import socket
import socketserver
import subprocess
import threading
import time
import traceback
from collections import deque
from dataclasses import asdict, fields, replace
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePath, PureWindowsPath
from urllib.parse import parse_qs, unquote, urlsplit

from PIL import Image, UnidentifiedImageError

from latentroom import REPO, __version__, settings
from latentroom.engine import (
    catalogue,
    downloads,
    hardware,
    history,
    install,
    jobs,
    paths,
    prompt_rewrite,
    runner,
    search,
    thumbs,
)
from latentroom.engine.jobs import JobRequest, LoraRef
from latentroom.errors import LatentroomError
from latentroom.web import phone, update

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
COOKIE = "lr_token"
MAX_JSON = 1 << 20
MAX_UPLOAD = 50 << 20
# shortcut: explicit table, because the Windows registry can map .js to text/plain;
# add a row when a new asset type ships.
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".ttf": "font/ttf",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}
UPLOAD_TYPES = {"image/png", "image/jpeg", "image/webp"}
# MPO: the multi-picture JPEG Samsung and iPhone cameras save; every reader uses its 1st frame
UPLOAD_EXT = {"PNG": ".png", "JPEG": ".jpg", "MPO": ".jpg", "WEBP": ".webp"}
MAX_PIXELS = 100_000_000  # what the message says; Pillow's own bomb stop is ~179 MP
UPLOAD_NAME = r"[0-9a-f]{32}\.(?:png|jpg|webp)"  # h_upload: token_hex(16) + UPLOAD_EXT
CSP = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)

INTERNAL = (
    "Something went wrong inside Latentroom. Copy the diagnostic report from Settings, then open "
    f"an issue at {REPO}/issues and paste it there."
)
FORBIDDEN = "Open Latentroom from its own window."
NOT_PAIRED = "Pair this phone first."  # code not_paired: the phone page shows its pairing screen
DESKTOP_ONLY = "This works only on the PC, in the Latentroom window."
STALLED = "The connection stopped before everything arrived. Check it and try again."
BAD = "The request was not understood. Reload Latentroom and try again."
GONE = "That picture no longer exists."
SIZE = "{} must be a multiple of 32 between 256 and 2048."
_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\)[^\"'\r\n]*")  # a Windows path, to the quote/EOL
_PC_PATH = re.compile(r"[A-Za-z]:[\\/]|[\\/]{2}")  # a value that is a path on the PC
_HIDDEN = re.compile(r"([0-9a-f]{16})/[^\\/]*")  # _PhonePaths.hide: "<key>/<file name>"
PICTURES = ("refs", "init_image", "mask")  # the job fields that name a picture on the PC


def _bad(message: str = BAD) -> LatentroomError:
    return LatentroomError("bad_request", message, 400)


def _forbidden(message: str = FORBIDDEN) -> LatentroomError:
    return LatentroomError("forbidden", message, 403)


def _not_found(message: str = "That page does not exist.") -> LatentroomError:
    return LatentroomError("not_found", message, 404)


class _Bytes:
    def __init__(self, data: bytes, mime: str) -> None:
        self.data, self.mime = data, mime


def _file(p: Path) -> _Bytes:
    if not p.is_file():
        raise _not_found(GONE)
    return _Bytes(p.read_bytes(), MIME.get(p.suffix.lower(), "application/octet-stream"))


class _Req:
    def __init__(
        self, query: dict, body: dict, raw: bytes, args: tuple, ctype: str, client: str = ""
    ) -> None:
        self.query, self.body, self.raw, self.args, self.ctype = query, body, raw, args, ctype
        self.client = client  # the caller's IP address: pairing counts wrong codes per phone


class _PhonePaths:
    """A phone never sees a path on the PC: it holds the Windows user name. In an answer to
    a phone each PC path becomes "<key>/<file name>" (the page reads only the name and sends the
    text back as it got it); in a job from a phone that text becomes the path again. Keys last
    while Latentroom runs: a page left open across a restart adds its pictures again."""

    def __init__(self) -> None:
        self._secret = secrets.token_bytes(16)
        self._paths: dict[str, str] = {}  # key -> path; one set per item, safe across threads

    def hide(self, value):
        if isinstance(value, dict):
            return {k: self.hide(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.hide(v) for v in value]
        if isinstance(value, PurePath):
            value = str(value)
        if not isinstance(value, str) or not _PC_PATH.match(value):
            return value
        key = hmac.new(self._secret, value.encode(), "sha256").hexdigest()[:16]
        self._paths[key] = value
        return f"{key}/{PureWindowsPath(value).name}"

    def show(self, body: dict) -> dict:
        """The picture fields of a phone's job, back to paths. Anything else there (a path the
        phone made up, a key from before a restart) is refused before it reaches the disk."""

        def path(text):
            if not isinstance(text, str) or not text:
                return text  # "" = none; a wrong type is refused by JobRequest.from_dict
            m = _HIDDEN.fullmatch(text)
            if m is None or m[1] not in self._paths:
                raise _not_found("That picture is no longer there. Add it again.")
            return self._paths[m[1]]

        out = dict(body)
        for name in PICTURES:
            if isinstance(out.get(name), list):
                out[name] = [path(t) for t in out[name]]
            elif name in out:
                out[name] = path(out[name])
        return out


# ---- input helpers (trust boundary: every value from the browser is checked here) ----


def _qint(q: dict, name: str, default: int, lo: int, hi: int, label: str) -> int:
    text = q.get(name, "")
    if text == "":
        return default
    try:
        n = int(text)
    except ValueError:
        n = lo - 1
    if not lo <= n <= hi:
        raise _bad(f"{label} must be a whole number between {lo} and {hi}.")
    return n


def _size(q: dict) -> tuple[int, int, int]:
    w = _qint(q, "width", 1024, 256, 2048, "Width")
    h = _qint(q, "height", 1024, 256, 2048, "Height")
    for label, v in (("Width", w), ("Height", h)):
        if v % 32:
            raise _bad(SIZE.format(label))
    return w, h, _qint(q, "refs", 0, 0, 16, "The number of reference pictures")


def _fit_hw() -> hardware.Hardware:
    """What fit sees: the detected card under the user's video memory limit."""
    return catalogue.capped(hardware.detect(), settings.load().max_vram_gb)


def _lora_bytes(q: dict, model_id: str) -> int:
    """What the queue counts for this model (jobs.lora_bytes): the LoRAs and the Turbo add-on
    Develop has on. `loras` = names joined by "/", which no file name holds;
    a name that is not in the LoRA folder counts nothing."""
    names = set(q.get("loras", "").split("/")) & set(install.list_loras())
    loras = [LoraRef(n) for n in names]
    return jobs.lora_bytes(JobRequest(model_id=model_id, turbo=q.get("turbo") == "1", loras=loras))


def _only(body: dict, key: str, kind: type) -> object:
    """Body must be exactly {key: <kind>}."""
    value = body.get(key)
    is_bool = isinstance(value, bool)
    if set(body) != {key} or not isinstance(value, kind) or is_bool != (kind is bool):
        raise _bad()
    return value


def _output_dir() -> Path:
    chosen = settings.load().output_dir
    p = Path(chosen) if chosen else paths.default_output_dir()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _open_folder(folder: Path) -> None:
    os.startfile(folder)  # a folder Latentroom built, never text from the browser


def _show_file(p: Path) -> None:
    # explorer wants /select,"<path>" as one string; Windows paths cannot contain quotes.
    subprocess.Popen(f'explorer /select,"{p}"')  # no shell; p comes from history, not the browser


# ---- endpoints ----


def h_version(srv, rq):
    return {"version": __version__, "engine_release": install.ENGINE_RELEASE}


def h_update(srv, rq):
    # the only call that asks GitHub; only from the button in Settings
    return update.check()


def h_hardware(srv, rq):
    return asdict(hardware.detect())


# shared parts Models lists; Develop checks upscaler + detector on disk; the Smart search
# files are one row in Models
PART_KINDS = ("llm", "llm_vision", "vae", "upscaler", "detector", "lora")
SEARCH_KINDS = ("embedding", "embedding_vision", "llama")  # Smart search
PART_KINDS += SEARCH_KINDS


def h_catalogue(srv, rq):
    w, h, refs = _size(rq.query)
    hw, have, pairs = _fit_hw(), install.installed(), catalogue.load()
    out, on_disk = [], False
    for pair in pairs:
        d = asdict(pair)
        for side in ("standard", "uncensored"):
            model = getattr(pair, side)
            if model is not None:
                ft = catalogue.fit(model, hw, w, h, refs, _lora_bytes(rq.query, model.id))
                d[side]["fit"] = asdict(ft)
                d[side]["speed"] = catalogue.speed(ft)  # wizard line 3
                d[side]["installed"] = all(f.id in have for f in catalogue.required(model.id))
                on_disk |= model.file.id in have
        out.append(d)
    engine = all(f.id in have for f in catalogue.files().values() if f.kind == "engine")
    best = catalogue.recommend_size(pairs, hw)  # a small or busy card: a smaller size
    parts = [asdict(f) for f in catalogue.files().values() if f.kind in PART_KINDS]
    return {
        "pairs": out,
        "recommended": best[0].id if best else None,
        "size": best[1] if best else None,  # square side the wizard proposes
        "files": parts,
        "fast_help": catalogue.fast_help(hw),
        # the engine and at least one picture model on disk: false after setup re-opens the
        # wizard at start. Engine ids count once sd-cli.exe is there (install.installed)
        "ready": engine and on_disk,
    }


def h_fit(srv, rq):
    model_id = rq.query.get("model_id", "")
    if not model_id:
        raise _bad("Pick a model first.")
    w, h, refs = _size(rq.query)
    model = catalogue.get(model_id)
    lora = _lora_bytes(rq.query, model_id)
    return asdict(catalogue.fit(model, _fit_hw(), w, h, refs, lora))


def h_downloads(srv, rq):
    return {"downloads": [asdict(s) for s in downloads.progress()]}


def h_download_start(srv, rq):
    if set(rq.body) == {"model_id"}:
        items = catalogue.required(_only(rq.body, "model_id", str))
    else:
        item = catalogue.files().get(_only(rq.body, "file_id", str))
        if item is None:
            raise _not_found("That download does not exist.")
        items = [item]
    have = install.installed()
    return {"downloads": [asdict(downloads.start(i)) for i in items if i.id not in have]}


def h_download_action(srv, rq):
    file_id, action = rq.args
    act = {"pause": downloads.pause, "resume": downloads.resume, "cancel": downloads.cancel}
    return asdict(act[action](file_id))


def h_engine(srv, rq):
    files = [asdict(f) for f in catalogue.files().values() if f.kind == "engine"]  # wizard step 4
    return {
        "release": install.ENGINE_RELEASE,
        "installed": install.engine_exe() is not None,
        "files": files,
    }


def h_engine_install(srv, rq):
    install.install_engine()
    return {"release": install.ENGINE_RELEASE, "installed": True}


ONEDRIVE = "This folder is in OneDrive, so OneDrive would upload every model you download."
NETWORK = "This folder is on a network drive, so models may load slowly or fail when it drops."
FAT32 = "This drive uses FAT32, which cannot hold files over 4 GB, and many models are bigger."


def data_warnings(folder: Path) -> list[str]:
    """One plain sentence per risk of the data folder. Setup shows them; it never blocks."""
    text, out = str(folder), []
    synced = (os.environ.get(k) for k in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"))
    if any(s and jobs._in_folder(text, Path(s)) for s in synced):
        out.append(ONEDRIVE)
    if jobs.is_network_path(text):  # \\host\share: told by its name, the share is not asked
        return [*out, NETWORK]
    fs, remote = hardware.drive(folder)  # a mapped drive (Z:) is only known by asking Windows
    if remote:
        out.append(NETWORK)
    if fs in ("FAT32", "FAT"):  # exFAT and NTFS hold big files
        out.append(FAT32)
    return out


def h_models(srv, rq):
    return {
        "installed": sorted(install.installed()),
        "disk_use": install.disk_use(),
        "free": hardware.free_disk(paths.data_dir()),
        "data_dir": str(paths.data_dir()),
        "data_warnings": data_warnings(paths.data_dir()),  # setup step 2
        "output_dir": settings.load().output_dir or str(paths.default_output_dir()),  # Settings
    }


def h_models_import(srv, rq):
    folder = Path(_only(rq.body, "folder", str))
    if not folder.is_absolute() or not folder.is_dir():
        raise _bad("That folder does not exist. Pick it again.")
    return {"imported": install.import_models(folder)}


def h_model_delete(srv, rq):
    item = catalogue.files().get(rq.args[0])
    if item is not None and item.kind in SEARCH_KINDS:
        search.stop()  # a running llama-server keeps its files locked
    install.delete_model(rq.args[0])
    return {"ok": True}


def h_loras(srv, rq):
    return {"loras": install.list_loras()}


def h_upload(srv, rq):
    try:
        with Image.open(io.BytesIO(rq.raw)) as im:
            fmt, (width, height) = im.format, im.size
            if width * height > MAX_PIXELS:  # from the header, before any decode
                raise Image.DecompressionBombError(f"{width}x{height}")
            im.verify()  # PNG: walks every chunk to the end mark and checks CRCs
        with Image.open(io.BytesIO(rq.raw)) as im:  # verify() leaves the image unusable
            im.load()  # full decode: a truncated JPEG/WebP fails here, not in the engine
    except Image.DecompressionBombError:
        raise _bad("That picture is far too large. Use one under 100 megapixels.") from None
    except UnidentifiedImageError:
        raise _bad(
            "That file is not a picture Latentroom can read. Use a PNG, JPEG or WebP."
        ) from None
    except Exception:  # Pillow raises many types for broken files
        raise _bad("This picture is damaged. Try another file.") from None
    if fmt not in UPLOAD_EXT:
        raise _bad("Only PNG, JPEG and WebP pictures can be used.")
    dst = paths.uploads_dir() / (secrets.token_hex(16) + UPLOAD_EXT[fmt])
    with open(dst, "xb") as f:  # x: never overwrite
        f.write(rq.raw)
    return {"path": str(dst), "name": dst.name, "width": width, "height": height}


def h_upload_get(srv, rq):
    return _file(paths.uploads_dir() / rq.args[0])


def sweep_uploads() -> int:
    """Delete the uploaded pictures (refs, source images, masks) no history entry and no job a
    crash left queued (runner.saved_jobs) uses: often photos of people, in a folder nobody
    opens. Call at app start, before the server: no job exists yet. A page left open on a phone
    may name a deleted one; Develop then says to add it again. Only h_upload's own names in
    uploads\\ are touched. Returns the count; a file still held stays until the next start. A
    broken history raises: nothing deleted."""
    with contextlib.closing(history._db()) as con:
        requests = [json.loads(raw) for (raw,) in con.execute("SELECT request FROM entries")]
    used = set()
    for r in requests + [job["request"] for job in runner.saved_jobs()]:
        for p in [*r.get("refs", []), r.get("init_image"), r.get("mask")]:
            if isinstance(p, str):
                used.add(Path(p).name)  # by name: a moved data folder keeps them
    gone = 0
    for p in paths.uploads_dir().iterdir():
        if re.fullmatch(UPLOAD_NAME, p.name) and p.name not in used:
            with contextlib.suppress(OSError):
                p.unlink()
                gone += 1
    return gone


def h_jobs_submit(srv, rq):
    req = JobRequest.from_dict(rq.body)
    s = settings.load()
    req.max_vram_gb = s.max_vram_gb
    req.model_id = req.model_id or s.model_id
    return {"job_id": srv.queue().submit(req)}


def h_jobs(srv, rq):
    return {"jobs": srv.queue().jobs()}


def h_job_cancel(srv, rq):
    return {"cancelled": srv.queue().cancel(rq.args[0])}


def h_prompt_enhance(srv, rq):  # the LLM gets the GPU only while no job runs
    idea = _only(rq.body, "prompt", str)
    return {"prompt": srv.queue().run_exclusive(lambda: prompt_rewrite.enhance(idea))}


def h_events(srv, rq):
    since = _qint(rq.query, "since", 0, 0, 2**53, "since")
    q = srv.queue()
    events = q.events(since)
    if events:
        last = events[-1].seq
    else:  # nothing new: still report the newest seq so a restarted Latentroom is noticed
        every = q.events(0)
        last = every[-1].seq if every else 0
    return {"events": [asdict(e) for e in events], "last_seq": last}


def h_history(srv, rq):
    q = rq.query
    starred = {"": None, "1": True, "0": False}
    if q.get("starred", "") not in starred:
        raise _bad()
    text = q.get("search", "")
    if len(text) > 500:
        raise _bad("The search text is too long.")
    day = q.get("day", "")
    if day and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise _bad("The day must look like 2026-10-22.")
    page = _qint(q, "page", 0, 0, 1_000_000, "Page")
    per_page = _qint(q, "per_page", 60, 1, 200, "Pictures per page")
    star = starred[q.get("starred", "")]
    if text.strip() and not day and search.available():  # Smart search: by meaning, best first
        found = search.search(text, (page + 1) * per_page, starred=star)
        found = found[page * per_page : (page + 1) * per_page]
        return {"entries": [asdict(e) for e in found], "smart": True}
    entries = history.list_entries(page=page, per_page=per_page, starred=star, search=text, day=day)
    return {"entries": [asdict(e) for e in entries], "smart": False}


def h_search(srv, rq):
    """Smart search state for Library ("Indexing 120 of 800"); starts indexing new pictures."""
    if not search.available():
        return {"available": False}
    search.start_indexing()
    return {"available": True, **search.status()}


def h_entry(srv, rq):
    return asdict(history.get(int(rq.args[0])))


def h_entry_image(srv, rq):
    return _file(Path(history.get(int(rq.args[0])).image))


def h_entry_thumb(srv, rq, side: int = thumbs.THUMB):
    entry_id = int(rq.args[0])
    p = Path(history.get(entry_id).image)
    if not p.is_file():
        thumbs.drop(entry_id)  # deleted in Explorer: no copy of it stays in the data folder
        raise _not_found(GONE)
    return _Bytes(thumbs.get(entry_id, p, side), "image/jpeg")  # still no-store: see _send


def h_entry_preview(srv, rq):
    """The Library panel's picture, screen-sized: not the full PNG on every arrow key."""
    return h_entry_thumb(srv, rq, thumbs.PREVIEW)


def h_entry_star(srv, rq):
    return asdict(history.star(int(rq.args[0]), _only(rq.body, "starred", bool)))


def h_entry_delete(srv, rq):
    history.delete(int(rq.args[0]))
    return {"ok": True}


def h_entry_reuse(srv, rq):
    return history.reuse(int(rq.args[0])).to_dict()


def h_settings(srv, rq):
    return asdict(settings.load())


def h_settings_set(srv, rq):
    return asdict(srv.update_settings(rq.body))


def h_diagnostics(srv, rq):
    return {"text": srv.diagnostics()}


def h_open(srv, rq):
    what = rq.body.get("what")
    if what == "file" and set(rq.body) == {"what", "entry_id"}:
        entry_id = rq.body["entry_id"]
        if not isinstance(entry_id, int) or isinstance(entry_id, bool):
            raise _bad()
        p = Path(history.get(entry_id).image)
        if not p.is_file():
            raise _not_found(GONE)
        _show_file(p)
    elif what in ("output", "loras", "data") and set(rq.body) == {"what"}:
        folder = {"output": _output_dir, "loras": paths.loras_dir, "data": paths.data_dir}[what]
        _open_folder(folder())
    else:
        raise _bad()
    return {"ok": True}


def h_phone(srv, rq):
    ip, reason = phone.tailscale_ip()
    host = srv.phone_host()
    return {
        "enabled": host is not None,
        "available": ip is not None,
        "address": f"http://{host}/" if host else "",
        "reason": reason,
        "phones": phone.count(),
    }


def h_phone_code(srv, rq):
    host = srv.phone_host()
    if host is None:
        raise LatentroomError("not_available", "Turn on Use from my phone first.", 409)
    code, expires = srv.pairing.new_code()
    # the phone page reads #code= and pairs by itself; a fragment never reaches a server
    url = f"http://{host}/#code={code}"
    return {"code": code, "expires": expires, "qr_svg": phone.qr_svg(url)}


def h_phone_pair(srv, rq):
    code = _only(rq.body, "code", str)
    if len(code) > 20:
        raise _bad(phone.WRONG_CODE)
    token = srv.pairing.pair(code, rq.client)
    cookie = f"{phone.COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=34560000"
    return {"ok": True}, [("Set-Cookie", cookie)]


def h_phone_forget(srv, rq):
    phone.forget_all()
    return {"ok": True}


ID = r"([A-Za-z0-9_.-]{1,100})"
ENTRY = r"/api/history/(\d{1,12})"
# (method, path regex, handler, flags) flags: desktop = refused for phone clients,
# public = no token needed, phone = phone listener only, upload = raw image body
_ROUTES = [
    (m, re.compile(p), fn, set(flags.split()))
    for m, p, fn, flags in [
        ("GET", "/api/version", h_version, ""),
        ("GET", "/api/update", h_update, "desktop"),
        ("GET", "/api/hardware", h_hardware, ""),
        ("GET", "/api/catalogue", h_catalogue, ""),
        ("GET", "/api/fit", h_fit, ""),
        ("GET", "/api/downloads", h_downloads, ""),
        ("POST", "/api/downloads", h_download_start, "desktop"),
        ("POST", f"/api/downloads/{ID}/(pause|resume|cancel)", h_download_action, "desktop"),
        ("GET", "/api/engine", h_engine, ""),
        ("POST", "/api/engine/install", h_engine_install, "desktop"),
        ("GET", "/api/models", h_models, ""),
        ("POST", "/api/models/import", h_models_import, "desktop"),
        ("DELETE", f"/api/models/{ID}", h_model_delete, "desktop"),
        ("GET", "/api/loras", h_loras, ""),
        ("POST", "/api/uploads", h_upload, "upload"),
        ("GET", rf"/api/uploads/({UPLOAD_NAME})", h_upload_get, ""),
        ("POST", "/api/jobs", h_jobs_submit, ""),
        ("GET", "/api/jobs", h_jobs, ""),
        ("POST", f"/api/jobs/{ID}/cancel", h_job_cancel, ""),
        ("POST", "/api/prompt/enhance", h_prompt_enhance, ""),
        ("GET", "/api/events", h_events, ""),
        ("GET", "/api/history", h_history, ""),
        ("GET", "/api/search", h_search, ""),
        ("GET", ENTRY, h_entry, ""),
        ("GET", ENTRY + "/image", h_entry_image, ""),
        ("GET", ENTRY + "/thumb", h_entry_thumb, ""),
        ("GET", ENTRY + "/preview", h_entry_preview, ""),
        ("POST", ENTRY + "/star", h_entry_star, ""),
        ("DELETE", ENTRY, h_entry_delete, ""),
        ("GET", ENTRY + "/reuse", h_entry_reuse, ""),
        ("GET", "/api/settings", h_settings, ""),
        ("POST", "/api/settings", h_settings_set, "desktop"),
        ("GET", "/api/diagnostics", h_diagnostics, "desktop"),
        ("POST", "/api/open", h_open, "desktop"),
        ("GET", "/api/phone", h_phone, ""),
        ("POST", "/api/phone/code", h_phone_code, "desktop"),
        ("POST", "/api/phone/pair", h_phone_pair, "phone public"),
        ("POST", "/api/phone/forget", h_phone_forget, "desktop"),
    ]
]


def _route(method: str, path: str):
    for m, rx, fn, flags in _ROUTES:
        match = rx.fullmatch(path)
        if match and m == method:
            return fn, flags, match
    raise _not_found()


# ---- HTTP plumbing ----


class _Handler(BaseHTTPRequestHandler):
    server_version = "Latentroom"
    sys_version = ""
    # a stalled client, or a kept-alive socket left idle (a closed tab, a phone gone to sleep),
    # gives its thread back after this many seconds without a byte
    timeout = 15
    # keep-alive: a page load reuses a few sockets instead of one per request; on HTTP/1.0 each
    # closed socket sat in TIME_WAIT for minutes and parallel runs used up the ports
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def log_message(self, format, *args) -> None:
        pass  # request lines carry the token and search text: never logged

    def _handle(self, method: str) -> None:
        url = urlsplit(self.path)
        api = url.path.startswith("/api/")
        self._unread = True  # the request body, if any; _body reads it
        try:
            try:
                self._check_caller(api)
                if api:
                    self._api(method, url)
                elif method == "GET":
                    self._static(url)
                else:
                    raise _not_found()
            except LatentroomError as e:
                self.server.app.note(e)
                self._error(e, api)
                self._linger()
            except Exception as e:
                if isinstance(e, ConnectionError):
                    raise
                log.exception("internal error on %s %s", method, url.path)
                self.server.app.note(e, f"{method} {url.path}")
                self._error(LatentroomError("internal", INTERNAL, 500), api)
                self._linger()
        except ConnectionError:
            self.close_connection = True  # the window went away mid-answer: nothing to do

    def _leftover(self) -> bool:
        """A request body not read: on a kept-alive socket it would pass for the next request."""
        h = self.headers
        cl = h.get("Content-Length", "0").strip()
        return self._unread and ("Transfer-Encoding" in h or cl not in ("", "0"))

    def _linger(self) -> None:
        # a body left unread makes closing the socket send RST, not FIN, and an RST that lands
        # before the client reads throws the answer away (a 60 MB drop would show a network error,
        # not "too big"). So, the answer being out: half-close, then swallow what the client still
        # sends until it closes, for a second at most (RFC 9112 9.6)
        if not self._leftover():
            return
        self.close_connection = True
        try:
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
            self.connection.settimeout(1)
            end = time.monotonic() + 1
            while time.monotonic() < end and self.connection.recv(1 << 16):
                pass
        except OSError:
            pass  # timed out or gone: close as it is

    def _check_caller(self, api: bool) -> None:
        host = self.server.host
        if self.headers.get("Host") != host:
            raise _forbidden()
        origin = self.headers.get("Origin")
        if origin is not None and origin != f"http://{host}":
            raise _forbidden()
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            raise _forbidden()

    def _api(self, method: str, url) -> None:
        app = self.server.app
        fn, flags, match = _route(method, url.path)
        phone_client = self.server.phone
        if phone_client and "desktop" in flags:
            raise _forbidden(DESKTOP_ONLY)
        if "phone" in flags and not phone_client:
            raise _forbidden()
        if "public" not in flags and not app.authorized(
            self.headers.get("Cookie", ""), phone_client
        ):
            raise LatentroomError("not_paired", NOT_PAIRED, 403) if phone_client else _forbidden()
        raw, body, ctype = self._body(method, "upload" in flags)
        if phone_client:
            body = app.phone_paths.show(body)
        query = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
        result = fn(app, _Req(query, body, raw, match.groups(), ctype, self.client_address[0]))
        headers = ()
        if isinstance(result, tuple):
            result, headers = result
        if isinstance(result, _Bytes):
            self._send(200, result.data, result.mime, headers)
        else:
            if phone_client:
                result = app.phone_paths.hide(result)
            self._send(200, json.dumps(result, default=str).encode(), MIME[".json"], headers)

    def _body(self, method: str, upload: bool) -> tuple[bytes, dict, str]:
        if method != "POST":
            return b"", {}, ""
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0:
            raise _bad()
        if n > (MAX_UPLOAD if upload else MAX_JSON):
            self.close_connection = True  # do not read it
            raise _bad("That file is too big: 50 MB at most." if upload else BAD)
        if upload and ctype not in UPLOAD_TYPES:
            raise _bad("Only PNG, JPEG and WebP pictures can be used.")
        if not upload and not (ctype == "application/json" or (n == 0 and ctype == "")):
            raise _bad()  # also stops cross-site HTML forms (they cannot send JSON)
        try:
            raw = self.rfile.read(n) if n else b""
        except TimeoutError:  # the sender went quiet for `timeout` s: a phone out of signal
            self.close_connection = True  # the rest may still come: never read it as a request
            raise LatentroomError("timeout", STALLED, 408) from None
        self._unread = False
        if len(raw) != n:
            raise _bad()
        if upload or not raw:
            return raw, {}, ctype
        try:
            body = json.loads(raw)
        except ValueError:
            raise _bad() from None
        if not isinstance(body, dict):
            raise _bad()
        return raw, body, ctype

    def _static(self, url) -> None:
        query = parse_qs(url.query)
        if url.path == "/" and "token" in query:
            token = self.server.app.token
            if self.server.phone or not hmac.compare_digest(
                query["token"][0].encode(), token.encode()
            ):
                raise _forbidden()
            cookie = f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/"
            self._send(303, b"", MIME[".txt"], [("Location", "/"), ("Set-Cookie", cookie)])
            return
        base = STATIC.resolve()
        p = (base / (unquote(url.path).lstrip("/") or "index.html")).resolve()
        if not p.is_relative_to(base) or not p.is_file():
            raise _not_found()
        self._send(200, p.read_bytes(), MIME.get(p.suffix.lower(), "application/octet-stream"))

    def _error(self, e: LatentroomError, api: bool) -> None:
        if api:
            body = json.dumps({"error": {"code": e.code, "message": e.message}}).encode()
            self._send(e.status, body, MIME[".json"])
        else:
            self._send(e.status, e.message.encode(), MIME[".txt"])

    def _send(self, status: int, data: bytes, mime: str, headers=()) -> None:
        if self._leftover():
            self.close_connection = True  # a GET/DELETE body or a refused POST: never parsed
        self.send_response(status)
        for k, v in (
            ("Content-Type", mime),
            ("Content-Length", str(len(data))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            ("X-Frame-Options", "DENY"),
            ("Content-Security-Policy", CSP),
            *((("Connection", "close"),) if self.close_connection else ()),
            *headers,
        ):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)


class _Listener(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # on Windows SO_REUSEADDR lets another program share the port
    request_queue_size = 64  # a page load opens ~20 requests at once; Windows refuses past 5

    def __init__(self, app: "Server", ip: str, port: int, phone: bool) -> None:
        super().__init__((ip, port), _Handler)
        self.app, self.phone = app, phone
        self.host = f"{ip}:{self.server_address[1]}"
        self.thread = threading.Thread(
            target=self.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True, name=self.host
        )
        self.thread.start()

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)  # skip HTTPServer's slow reverse-DNS lookup
        self.server_name, self.server_port = self.server_address[:2]

    def close(self) -> None:
        self.shutdown()
        self.server_close()


class Server:
    url: str  # http://127.0.0.1:<port>/?token=<per-launch secret>; open this in the window

    def __init__(self, port: int) -> None:
        self.token = secrets.token_urlsafe(32)
        self._lock = threading.RLock()
        self._queue = None
        self._errors: deque[str] = deque(maxlen=10)
        self._stopped = False
        self.pairing = phone.Pairing()
        self.phone_paths = _PhonePaths()
        self._phone: _Listener | None = None
        self._desktop = _Listener(self, "127.0.0.1", port, phone=False)
        self.url = f"http://{self._desktop.host}/?token={self.token}"
        self.sync_phone()

    def phone_host(self) -> str | None:
        listener = self._phone
        return listener.host if listener else None

    def sync_phone(self) -> None:
        """Open or close the Tailscale listener to match settings.phone_enabled.
        Binds the Tailscale IPv4 only, never 0.0.0.0 or the LAN."""
        with self._lock:
            want = settings.load().phone_enabled and not self._stopped
            if want and self._phone is None:
                ip, _ = phone.tailscale_ip()
                if ip is None:
                    return
                try:
                    self._phone = _Listener(self, ip, phone.PORT, phone=True)
                except OSError:
                    self._phone = _Listener(self, ip, 0, phone=True)  # fixed port taken
            elif not want and self._phone is not None:
                self._phone.close()
                self._phone = None

    def queue(self) -> runner.Queue:
        """The one job queue, made on first use (so the server starts even if the engine
        side cannot, and the UI can show why)."""
        with self._lock:
            if self._queue is None:
                self._queue = runner.Queue(output_dir=_output_dir)
            return self._queue

    def authorized(self, cookie_header: str, phone_client: bool) -> bool:
        cookies = SimpleCookie()
        try:
            cookies.load(cookie_header)
        except CookieError:
            return False
        if phone_client:
            morsel = cookies.get(phone.COOKIE)
            return morsel is not None and phone.known(morsel.value)
        morsel = cookies.get(COOKIE)
        return morsel is not None and hmac.compare_digest(
            morsel.value.encode(), self.token.encode()
        )

    def update_settings(self, changes: dict) -> settings.Settings:
        known = {f.name: f for f in fields(settings.Settings)}
        defaults = settings.Settings()
        for key, value in list(changes.items()):
            if key not in known:
                raise _bad(f"Unknown setting: {key}.")
            default = getattr(defaults, key)
            if not settings._type_ok(value, default):
                raise _bad(f"The setting {key} has the wrong kind of value.")
            if key in ("data_dir", "output_dir") and value:
                p = Path(value)
                try:
                    if not p.is_absolute():
                        raise OSError
                    if key == "data_dir":  # another app's folder gets a Latentroom folder inside
                        p = paths.data_folder_for(p)
                        changes[key] = str(p)
                    p.mkdir(parents=True, exist_ok=True)
                except OSError:
                    raise _bad("Latentroom cannot use that folder. Pick another one.") from None
            if key == "max_vram_gb" and not 0 <= value <= 256:
                raise _bad("Max video memory must be between 0 and 256 GB (0 = automatic).")
            if key == "model_id" and len(value) > 200:
                raise _bad()
        if changes.get("phone_enabled") is True and phone.tailscale_ip()[0] is None:
            raise LatentroomError("not_available", phone.NO_TAILSCALE, 409)
        with self._lock:
            new = replace(settings.load(), **changes)
            settings.save(new)
        if "data_dir" in changes:  # the new folder lists Latentroom's files before any download
            try:
                install.write_own_files()
            except OSError:
                log.warning("cannot write the list of Latentroom's own files", exc_info=True)
        if "phone_enabled" in changes:
            self.sync_phone()
        return new

    def note(self, e: Exception, where: str = "") -> None:
        """Remember the last errors for the diagnostic report (never prompts or paths)."""
        if isinstance(e, LatentroomError):
            if e.code in ("bad_request", "forbidden", "not_found"):
                return
            text = f"{e.code}: {e.message}"
        else:
            tb = traceback.extract_tb(e.__traceback__)
            frames = [f for f in tb if "latentroom" in f.filename]
            at = f"{Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else "?"
            text = f"internal: {type(e).__name__} at {at} ({where})"
        self._errors.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}")

    def diagnostics(self) -> str:
        lines = [
            f"Latentroom {__version__}",
            f"Engine release: {install.ENGINE_RELEASE}",
            f"Windows: {platform.platform()}",
            f"Python: {platform.python_version()}",
        ]

        def add(title, fn):
            try:
                value = fn()
            except Exception as e:  # the report must always render
                value = f"unavailable ({type(e).__name__})"
            lines.append(f"{title}: {value}")

        add("Hardware", lambda: json.dumps(asdict(hardware.detect())))  # "gpus": NVIDIA cards
        add("Data folder file system", lambda: hardware.drive(paths.data_dir())[0] or "unknown")
        add(
            "Data folder name has non-ASCII",
            lambda: "no" if str(paths.data_dir()).isascii() else "yes",
        )
        add("Engine installed", lambda: install.engine_exe() is not None)
        add("Installed files", lambda: ", ".join(sorted(install.installed())) or "none")
        shown = asdict(settings.load())
        for key in ("data_dir", "output_dir"):
            shown[key] = "custom" if shown[key] else "default"
        lines.append(f"Settings: {json.dumps(shown)}")
        lines.append("Recent errors:")
        lines += list(self._errors) or ["none"]
        logs = sorted(paths.logs_dir().glob("engine-*.log"), key=lambda p: p.stat().st_mtime)
        # latentroom.log holds no prompt or token (request lines are never logged, engine
        # output goes to its own redacted log); _PATH below scrubs its paths like the rest
        for p in [*logs[-3:], paths.logs_dir() / paths.APP_LOG]:
            try:
                tail = p.read_text(encoding="utf-8", errors="replace").splitlines()[-50:]
            except OSError:
                continue  # not written yet, or rotating right now
            lines.append(f"--- {p.name} (last 50 lines)")
            lines += tail
        return _PATH.sub("[path]", "\n".join(lines))

    def stop(self) -> None:
        """Stop serving, cancel any running job (engine process killed) and stop the
        llama-servers of Smart search and Enhance prompt. Safe twice."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            queue, self._queue = self._queue, None
            listener, self._phone = self._phone, None
        self._desktop.close()
        if listener is not None:
            listener.close()
        if queue is not None:
            queue.shutdown()
        search.stop()
        prompt_rewrite.stop()  # an Enhance prompt in flight answers enhance_failed


def start(port: int = 0) -> Server:
    """Serve on 127.0.0.1:<port> (0 = random free port) in a background thread."""
    return Server(port)
