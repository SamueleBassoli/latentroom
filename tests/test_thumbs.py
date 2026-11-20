"""Library previews saved on disk (the phone's no-store header: test_server)."""

import io
import os
import threading
from pathlib import Path

import pytest
from PIL import Image

from latentroom.engine import history, images, paths, thumbs
from latentroom.engine.jobs import JobRequest
from latentroom.errors import LatentroomError


def png(path: Path, color: str = "gray") -> Path:
    Image.new("RGB", (96, 64), color).save(path)
    return path


def saved(entry_id: int) -> list[str]:
    return sorted(p.name for p in paths.thumbs_dir().iterdir() if p.name.startswith(f"{entry_id}-"))


@pytest.fixture
def made(monkeypatch) -> list[Path]:
    """The pictures images.thumbnail was really asked to shrink (a miss each)."""
    calls, real = [], images.thumbnail
    monkeypatch.setattr(images, "thumbnail", lambda src, *a: calls.append(src) or real(src, *a))
    return calls


def test_a_preview_is_made_once_then_read_from_disk(tmp_path, made):
    pic = png(tmp_path / "a.png")
    first = thumbs.get(7, pic)
    assert first[:2] == b"\xff\xd8" and made == [pic]  # a JPEG
    st = pic.stat()
    assert saved(7) == [f"7-{st.st_mtime_ns}-{st.st_size}.jpg"]
    assert thumbs.get(7, pic) == first and made == [pic]  # a hit: no decode


def test_the_panel_preview_is_screen_sized_and_kept_beside_the_thumb(tmp_path, made):
    """The Library panel loaded the full PNG (~5k px after an upscale) on every arrow key."""
    pic = tmp_path / "big.png"
    Image.new("RGB", (3000, 2000), "gray").save(pic)
    small, big = thumbs.get(7, pic), thumbs.get(7, pic, thumbs.PREVIEW)
    side = lambda jpeg: max(Image.open(io.BytesIO(jpeg)).size)  # noqa: E731
    assert side(small) == 384 and side(big) == 1280
    st = pic.stat()
    stem = f"7-{st.st_mtime_ns}-{st.st_size}"
    assert saved(7) == [f"{stem}.1280.jpg", f"{stem}.jpg"]  # one does not delete the other
    assert thumbs.get(7, pic) == small and thumbs.get(7, pic, thumbs.PREVIEW) == big
    assert made == [pic, pic]  # both read from the disk the 2nd time


def test_a_changed_picture_gets_a_new_preview_and_the_old_one_goes(tmp_path, made):
    pic = png(tmp_path / "a.png")
    old = thumbs.get(7, pic)
    png(pic, "red")
    t = pic.stat().st_mtime + 1
    os.utime(pic, (t, t))  # edited later (and not within one tick of the clock)
    assert thumbs.get(7, pic) != old and len(made) == 2 and len(saved(7)) == 1


def test_an_id_that_names_another_picture_never_shows_the_old_preview(tmp_path):
    a, b = png(tmp_path / "a.png", "red"), png(tmp_path / "b.png", "blue")
    t = a.stat().st_mtime - 3600
    os.utime(b, (t, t))  # b is OLDER: "newer than its picture" would have kept a's preview
    thumbs.get(1, a)  # then an old db.sqlite + pictures restored: id 1 is b now
    assert thumbs.get(1, b) == images.thumbnail(b)[0] != images.thumbnail(a)[0]


def test_a_preview_that_cannot_be_saved_is_still_served(tmp_path, monkeypatch):
    def locked(src, dst):
        raise PermissionError(5, "Access is denied")  # Windows: dst is being read

    monkeypatch.setattr(os, "replace", locked)
    pic = png(tmp_path / "a.png")
    assert thumbs.get(7, pic) == images.thumbnail(pic)[0]
    assert saved(7) == []  # nothing saved, no temp file left


def test_two_first_requests_at_once_both_get_it_and_one_file_stays(tmp_path, monkeypatch):
    both, real = threading.Barrier(2, timeout=10), images.thumbnail

    def slow(src, *a):
        both.wait()  # both requests missed on disk before either one saves
        return real(src, *a)

    monkeypatch.setattr(images, "thumbnail", slow)
    pic, out = png(tmp_path / "a.png"), []
    runs = [threading.Thread(target=lambda: out.append(thumbs.get(7, pic))) for _ in "ab"]
    for t in runs:
        t.start()
    for t in runs:
        t.join(10)
    assert len(out) == 2 and out[0] == out[1] == real(pic)[0] and len(saved(7)) == 1


def test_deleting_an_entry_deletes_its_preview_only(tmp_path):
    a = history.add(JobRequest(prompt="a lighthouse at dusk"), png(tmp_path / "a.png"), 1)
    b = history.add(JobRequest(prompt="a lighthouse at dusk"), png(tmp_path / "b.png"), 2)
    thumbs.get(a.id, Path(a.image))
    thumbs.get(b.id, Path(b.image))
    thumbs.get(a.id * 10 + 1, Path(b.image))  # an id that starts with a's digits
    history.delete(a.id)
    assert saved(a.id) == [] and len(saved(b.id)) == 1 and len(saved(a.id * 10 + 1)) == 1


@pytest.mark.skipif(os.name != "nt", reason="only Windows refuses to delete an open file")
def test_delete_while_its_preview_is_being_read(tmp_path):  # no 409 "in use"
    e = history.add(JobRequest(prompt="a lighthouse at dusk"), png(tmp_path / "a.png"), 1)
    thumbs.get(e.id, Path(e.image))
    held = paths.thumbs_dir() / saved(e.id)[0]
    with open(held, "rb"):  # a /thumb request reading it: unlink gives WinError 32
        history.delete(e.id)
    assert not Path(e.image).exists() and held.exists()  # the shortcut in thumbs.drop
    with pytest.raises(LatentroomError) as gone:
        history.get(e.id)
    assert gone.value.code == "not_found"
    assert thumbs.sweep() == 1 and not held.exists()  # the next app start


def test_the_start_sweep_removes_only_previews_of_deleted_pictures(tmp_path):
    e = history.add(JobRequest(prompt="a lighthouse at dusk"), png(tmp_path / "a.png"), 1)
    thumbs.get(e.id, Path(e.image))
    d, live = paths.thumbs_dir(), saved(e.id)
    left = [f"{e.id + 1}-1-1.jpg", f"{e.id * 10}-1-1.jpg", f"{live[0]}.ab12cd34.tmp"]  # + a crash's
    for name in left:
        (d / name).write_bytes(b"x")
    assert thumbs.sweep() == 3 and sorted(p.name for p in d.iterdir()) == live
    assert thumbs.sweep() == 0
