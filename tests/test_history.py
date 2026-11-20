import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest
from PIL import Image

from latentroom.engine import history, paths
from latentroom.engine.jobs import JobRequest
from latentroom.errors import LatentroomError


def png(path, size=(64, 32)):
    Image.new("RGB", size).save(path)
    return path


def test_add_list_filter_star_reuse_delete(tmp_path, monkeypatch):
    a = history.add(
        JobRequest(prompt="a red fox", model_id="m", seed=5), png(tmp_path / "a.png"), 5
    )
    b = history.add(JobRequest(prompt="100% blue_sky"), png(tmp_path / "b.png", (32, 64)), 9)
    assert (a.width, a.height, b.width, b.height) == (64, 32, 32, 64)
    assert a.request["seed"] == 5 and b.request["seed"] == 9
    assert [e.id for e in history.list_entries()] == [b.id, a.id]  # newest first
    assert [e.id for e in history.list_entries(search="FOX")] == [a.id]
    assert [e.id for e in history.list_entries(search="100%")] == [b.id]  # % is literal
    assert history.list_entries(search="_") == [b]
    assert history.list_entries(per_page=1, page=1) == [a]
    today = time.strftime("%Y-%m-%d")
    assert len(history.list_entries(day=today)) == 2 and not history.list_entries(day="2001-01-01")
    assert history.star(a.id).starred
    assert history.list_entries(starred=True) == [history.get(a.id)]
    reused = history.reuse(a.id)
    assert (reused.prompt, reused.seed, reused.model_id) == ("a red fox", 5, "m")
    history.delete(a.id)
    assert not (tmp_path / "a.png").exists()
    with pytest.raises(LatentroomError) as e:
        history.get(a.id)
    assert (e.value.code, e.value.status) == ("not_found", 404)
    assert history.has_image(tmp_path / "b.png") and not history.has_image(tmp_path / "a.png")


@pytest.mark.skipif(os.name != "nt", reason="only Windows refuses to delete an open file")
def test_delete_a_picture_open_in_another_program(tmp_path):
    entry = history.add(JobRequest(prompt="x"), png(tmp_path / "a.png"), 1)
    with open(entry.image, "rb"):  # like a viewer: no delete sharing, WinError 32
        with pytest.raises(LatentroomError) as e:
            history.delete(entry.id)
    assert (e.value.code, e.value.status) == ("in_use", 409)
    assert e.value.message == "This picture is open in another program. Close it and try again."
    assert history.get(entry.id) and Path(entry.image).exists()  # nothing lost
    history.delete(entry.id)  # closed: now it goes
    assert not Path(entry.image).exists()


def version() -> int:
    with closing(sqlite3.connect(paths.db_path())) as con:
        return con.execute("PRAGMA user_version").fetchone()[0]


def test_schema_is_versioned_and_an_unversioned_history_upgrades(tmp_path):
    assert not history.list_entries() and version() == 2  # v2: the Smart search embeddings table
    paths.db_path().unlink()
    with closing(sqlite3.connect(paths.db_path())) as con, con:  # an unversioned history: v0
        con.executescript(OLD_SCHEMA)
        con.execute(OLD_ROW, (str(png(tmp_path / "old.png")),))
    assert [e.prompt for e in history.list_entries()] == ["an old fox"] and version() == 2
    with closing(sqlite3.connect(paths.db_path())) as con:
        assert con.execute("SELECT count(*) FROM embeddings").fetchone() == (0,)
    with closing(sqlite3.connect(paths.db_path())) as con, con:
        con.execute("PRAGMA user_version = 99")  # made by a newer Latentroom
    with pytest.raises(LatentroomError) as e:
        history.list_entries()
    assert e.value.code == "not_available" and version() == 99


def _garbage(p: Path) -> None:
    p.write_bytes(b"not a database " * 300)  # SQLITE_NOTADB


def _truncated(p: Path) -> None:
    p.write_bytes(p.read_bytes()[: 4096 * 3])  # SQLITE_CORRUPT on the first read


def _bad_page(p: Path) -> None:  # opens fine; only a full check sees it
    raw = bytearray(p.read_bytes())
    raw[-4096:] = b"\xa5" * 4096
    p.write_bytes(raw)


@pytest.mark.parametrize("damage", [_garbage, _truncated, _bad_page])
def test_a_damaged_history_is_put_aside_and_a_new_one_starts(tmp_path, monkeypatch, caplog, damage):
    """A damaged db.sqlite (a crash mid-write, a failing disk) is renamed
    db.sqlite.broken-<date> with its journal, a new history starts and the Library works,
    empty. Before, every Library call failed. Checked once per file per run (a new start)."""
    for n in range(40):  # a few pages, so the last one holds entries
        history.add(JobRequest(prompt="a lighthouse at dusk " * 20), png(tmp_path / f"{n}.png"), n)
    db = paths.db_path()
    db.with_name("db.sqlite-journal").write_bytes(b"")  # a crash left it: it goes along
    damage(db)
    raw = db.read_bytes()
    monkeypatch.setattr(history, "_checked", set())  # the next start
    assert history.list_entries() == []
    history.add(JobRequest(prompt="x"), png(tmp_path / "new.png"), 1)
    assert len(history.list_entries()) == 1
    broken = [p.name for p in db.parent.glob("db.sqlite.broken-*")]
    stamp = time.strftime("%Y%m%d")
    assert len(broken) == 2 and all(n.startswith(f"db.sqlite.broken-{stamp}") for n in broken)
    kept = next(p for p in db.parent.glob("db.sqlite.broken-*") if not p.name.endswith("journal"))
    assert kept.read_bytes() == raw and "history was damaged" in caplog.text


def test_a_busy_history_is_never_put_aside(monkeypatch):
    """A history another program holds locked fails with "database is locked", a DatabaseError
    too: that is not damage, nothing is renamed."""
    history.list_entries()
    monkeypatch.setattr(history, "_checked", set())
    real = sqlite3.connect
    monkeypatch.setattr(history.sqlite3, "connect", lambda p, timeout: real(p, timeout=0))
    with closing(real(paths.db_path())) as holder:
        holder.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite3.OperationalError):
            history.list_entries()
    assert history.list_entries() == []
    assert not list(paths.db_path().parent.glob("db.sqlite.broken-*"))


OLD_SCHEMA = """CREATE TABLE entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL NOT NULL, kind TEXT NOT NULL, prompt TEXT NOT NULL, request TEXT NOT NULL,
    seed INTEGER NOT NULL, model_id TEXT NOT NULL, image TEXT NOT NULL UNIQUE,
    width INTEGER NOT NULL, height INTEGER NOT NULL, starred INTEGER NOT NULL DEFAULT 0);
CREATE INDEX entries_created ON entries (created);"""
OLD_ROW = """INSERT INTO entries (created, kind, prompt, request, seed, model_id, image, width,
    height) VALUES (1, 'txt2img', 'an old fox', '{"prompt": "an old fox"}', 1, 'm', ?, 64, 32)"""
