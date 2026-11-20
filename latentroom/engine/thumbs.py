"""Library previews saved on disk.
The window runs WebView2 in private mode (pywebview's default), so no preview survives an app
start in the browser; each one cost a full PNG decode (50-70 ms). Saved here instead, as
paths.thumbs_dir() / "<id>-<mtime_ns>-<size>.jpg": keyed by the picture FILE too, so an id
that names another picture (an old history restored) never shows the old preview.
The Library panel's picture is one too, "<id>-<mtime_ns>-<size>.1280.jpg" (PREVIEW px long
side: a phone's full width at 3x, the desktop panel at 2x), not the full PNG (~5k px after an
upscale) decoded and sent again on every arrow key. The full PNG stays for Save to phone.
A preview never fails a request or a delete: Windows refuses to replace or delete a file
while it is being read, and then the preview is just not saved or not removed."""

import contextlib
import os
import secrets
from pathlib import Path

from latentroom.engine import paths

THUMB, PREVIEW = 384, 1280  # long side in px: the contact sheet, the Library panel


def drop(entry_id: int, keep: str = "") -> None:
    """Delete the saved previews of `entry_id` (all but those whose name starts with `keep`).
    Never raises.
    A preview held open at that moment stays: sweep() removes it at the next app start."""
    for p in paths.thumbs_dir().glob(f"{int(entry_id)}-*.jpg"):
        if not (keep and p.name.startswith(keep)):
            with contextlib.suppress(OSError):
                p.unlink()


def sweep() -> int:
    """Delete every saved preview whose picture is no longer in the history, and any temp file
    a crash left (a deleted picture leaves no copy in the data folder).
    Call at app start, before the server: then nothing is being written. Returns the count.
    Never raises on a file: one still held stays until the next start."""
    from latentroom.engine import history  # history imports this module

    with contextlib.closing(history._db()) as con:
        keep = {str(i) for (i,) in con.execute("SELECT id FROM entries")}
    gone = 0
    for p in paths.thumbs_dir().iterdir():
        if p.name.endswith(".tmp") or p.name.split("-", 1)[0] not in keep:
            with contextlib.suppress(OSError):
                p.unlink()
                gone += 1
    return gone


def get(entry_id: int, src: Path, side: int = THUMB) -> bytes:
    """JPEG preview (long side at most `side`, THUMB or PREVIEW) of history entry `entry_id`,
    whose picture is `src`: the saved one, else a new one, saved for the next time.
    Raises OSError when `src` cannot be read."""
    from latentroom.engine import images  # Pillow only on a miss

    st = src.stat()
    stem = f"{int(entry_id)}-{st.st_mtime_ns}-{st.st_size}"
    dst = paths.thumbs_dir() / (f"{stem}.jpg" if side == THUMB else f"{stem}.{int(side)}.jpg")
    with contextlib.suppress(OSError):
        return dst.read_bytes()
    data, _jpeg = images.thumbnail(src, side)
    drop(entry_id, keep=f"{stem}.")  # the previews of an older file under this id
    tmp = dst.with_name(f"{dst.name}.{secrets.token_hex(4)}.tmp")  # one per request
    try:
        tmp.write_bytes(data)
        os.replace(tmp, dst)  # readers see the whole file or none
    except OSError:  # a reader holds dst (WinError 5), disk full, ...: serve it unsaved
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
    return data
