"""History of finished images (sqlite at paths.db_path()).
Prompts live only here, inside the user's data folder; PNG files stay clean."""

import json
import logging
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass, fields
from pathlib import Path

from latentroom.engine import paths, thumbs
from latentroom.engine.jobs import JobRequest
from latentroom.errors import LatentroomError

log = logging.getLogger(__name__)

# _MIGRATIONS[i] upgrades a history from PRAGMA user_version i to i + 1. Append, never edit.
_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (  # v1, the first schema. IF NOT EXISTS: a history made before versions has it already
        """CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL NOT NULL, kind TEXT NOT NULL, prompt TEXT NOT NULL, request TEXT NOT NULL,
    seed INTEGER NOT NULL, model_id TEXT NOT NULL, image TEXT NOT NULL UNIQUE,
    width INTEGER NOT NULL, height INTEGER NOT NULL, starred INTEGER NOT NULL DEFAULT 0)""",
        "CREATE INDEX IF NOT EXISTS entries_created ON entries (created)",
    ),
    (  # v2, Smart search (engine/search.py): one picture vector per entry
        """CREATE TABLE embeddings (
    entry_id INTEGER PRIMARY KEY, model TEXT NOT NULL, vec BLOB NOT NULL)""",
        """CREATE TRIGGER entries_drop_embedding AFTER DELETE ON entries
    BEGIN DELETE FROM embeddings WHERE entry_id = OLD.id; END""",
    ),
)
_COLUMNS = "id, created, kind, prompt, request, seed, model_id, image, width, height, starred"


@dataclass
class Entry:
    id: int
    created: float  # unix seconds
    kind: str  # JobRequest.kind
    prompt: str
    request: dict  # JobRequest.to_dict() with the real seed filled in
    seed: int
    model_id: str
    image: str  # absolute path of the PNG in the output folder
    width: int
    height: int
    starred: bool = False


def _db() -> sqlite3.Connection:
    """One short connection per call: the queue thread and the web threads share the file."""
    path = paths.db_path()
    if path not in _checked:
        with _check_lock:
            if path not in _checked:
                _set_aside_if_damaged(path)
                _checked.add(path)
    con = sqlite3.connect(path, timeout=10)
    try:
        _migrate(con)
    except BaseException:
        con.close()
        raise
    return con


# each history file is checked once per run, at its first use (the app start sweeps)
_checked: set[Path] = set()
_check_lock = threading.Lock()
_DAMAGE = (11, 26)  # SQLITE_CORRUPT, SQLITE_NOTADB; "database is locked" is not damage


def _damaged(path: Path) -> bool:
    """True when the history file is damaged (a crash mid-write, a failing disk). A damaged
    page can open fine and fail later: only a quick check of every page sees it."""
    try:
        with closing(sqlite3.connect(path, timeout=10)) as con:
            return con.execute("PRAGMA quick_check").fetchall() != [("ok",)]
    except sqlite3.DatabaseError as e:
        if (getattr(e, "sqlite_errorcode", 0) & 0xFF) in _DAMAGE:
            return True
        raise


def _set_aside_if_damaged(path: Path) -> None:
    """A damaged history is kept as db.sqlite.broken-<date> (its journal along) and a new one
    starts: the Library works again, empty, and nothing is deleted."""
    if not path.exists() or not _damaged(path):
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for suffix in ("", "-journal", "-wal", "-shm"):
        part = path.with_name(path.name + suffix)
        if part.exists():
            paths.replace(part, path.with_name(f"{path.name}.broken-{stamp}{suffix}"))
    log.warning("history was damaged: kept as %s.broken-%s, a new one started", path.name, stamp)


def _version(con: sqlite3.Connection) -> int:
    return con.execute("PRAGMA user_version").fetchone()[0]


def _migrate(con: sqlite3.Connection) -> None:
    """Bring the history to the newest schema: every missing step, all or nothing."""
    if _version(con) == len(_MIGRATIONS):
        return
    with con:  # commit, or roll back on any error
        con.execute("BEGIN IMMEDIATE")  # one writer: two threads never run a step twice
        version = _version(con)
        if version > len(_MIGRATIONS):
            raise LatentroomError(
                "not_available",
                "Your history was made by a newer Latentroom. Update Latentroom to open it.",
                409,
            )
        for step in _MIGRATIONS[version:]:
            for sql in step:
                con.execute(sql)
        con.execute(f"PRAGMA user_version = {len(_MIGRATIONS)}")


def _entry(row: tuple) -> Entry:
    e = Entry(*row)
    e.request, e.starred = json.loads(e.request), bool(e.starred)
    return e


def add(req: JobRequest, image: Path, seed: int) -> Entry:
    from latentroom.engine import images  # Pillow only when an image is really stored

    width, height = images.image_size(image)
    request = req.to_dict() | {"seed": seed}
    row = (time.time(), req.kind, req.prompt, json.dumps(request), seed, req.model_id)
    with closing(_db()) as con, con:
        cur = con.execute(
            f"INSERT INTO entries ({_COLUMNS[4:]}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (*row, str(image.resolve()), width, height),
        )
        return get(cur.lastrowid, con)


def list_entries(
    page: int = 0,
    per_page: int = 60,
    starred: bool | None = None,
    search: str = "",
    day: str = "",
) -> list[Entry]:
    """Newest first. `search` matches the prompt; `day` is "YYYY-MM-DD" (local time)."""
    where, args = [], []
    if starred is not None:
        where.append("starred = ?")
        args.append(int(starred))
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("prompt LIKE ? ESCAPE '\\'")
        args.append(f"%{escaped}%")
    if day:
        where.append("date(created, 'unixepoch', 'localtime') = ?")
        args.append(day)
    per_page = max(1, min(int(per_page), 500))
    sql = f"SELECT {_COLUMNS} FROM entries"
    sql += f" WHERE {' AND '.join(where)}" if where else ""
    sql += " ORDER BY created DESC, id DESC LIMIT ? OFFSET ?"
    with closing(_db()) as con:
        rows = con.execute(sql, (*args, per_page, max(0, int(page)) * per_page)).fetchall()
    return [_entry(r) for r in rows]


def get(entry_id: int, con: sqlite3.Connection | None = None) -> Entry:
    """Raises LatentroomError("not_found", ..., 404)."""
    sql = f"SELECT {_COLUMNS} FROM entries WHERE id = ?"
    if con is None:
        with closing(_db()) as own:
            row = own.execute(sql, (entry_id,)).fetchone()
    else:
        row = con.execute(sql, (entry_id,)).fetchone()
    if row is None:
        raise LatentroomError("not_found", "This picture is no longer in your history.", 404)
    return _entry(row)


def star(entry_id: int, starred: bool = True) -> Entry:
    with closing(_db()) as con, con:
        con.execute("UPDATE entries SET starred = ? WHERE id = ?", (int(starred), entry_id))
        return get(entry_id, con)


def delete(entry_id: int) -> None:
    """Delete the entry AND its image file. Asks nothing: the UI confirms first.
    Raises LatentroomError("in_use", 409) when another program holds the file; the row is kept."""
    entry = get(entry_id)
    try:
        Path(entry.image).unlink(missing_ok=True)
    except PermissionError as e:
        if getattr(e, "winerror", None) not in (32, 33):  # sharing / lock violation
            raise
        raise LatentroomError(
            "in_use", "This picture is open in another program. Close it and try again.", 409
        ) from e
    with closing(_db()) as con, con:
        con.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    thumbs.drop(entry_id)  # after the row: a preview being read never turns into a 409


def reuse(entry_id: int) -> JobRequest:
    """The request that made this image, for "Reuse settings"."""
    known = {f.name for f in fields(JobRequest)}  # an older Latentroom may have stored more
    return JobRequest.from_dict({k: v for k, v in get(entry_id).request.items() if k in known})


def has_image(path: Path) -> bool:
    """True when `path` is a history image (allowed as a job input)."""
    return seed_of(path) is not None


def seed_of(path: Path) -> int | None:
    """The seed a history image was made with; None when `path` is no history image."""
    with closing(_db()) as con:
        hit = con.execute("SELECT seed FROM entries WHERE image = ?", (str(path.resolve()),))
        row = hit.fetchone()
        return row[0] if row else None
