import hashlib
import json
import os
import zipfile
from dataclasses import asdict
from pathlib import Path

import pytest

from latentroom.engine import downloads, install, paths
from latentroom.engine.catalogue import FileItem
from latentroom.errors import LatentroomError

A, V, E = b"A" * 3000, b"V" * 4000, b"E" * 5000  # model, vae, upscaler contents


def item(id, name, data, kind="diffusion", sha=True):
    sha256 = hashlib.sha256(data).hexdigest() if sha else None
    return FileItem(id, name, f"https://example.invalid/{name}", len(data), sha256, "test", kind)


def make_zip(path, members):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return item(path.stem, path.name, path.read_bytes(), "engine")


@pytest.fixture
def zips(monkeypatch):
    """Both engine zips downloaded into engine/, pinned to their real size and sha256."""
    root = paths.engine_dir()
    files = (
        make_zip(root / "sd.zip", {"sd-cli.exe": b"MZ", "stable-diffusion.dll": b"dll"}),
        make_zip(root / "cudart.zip", {"cudart64_12.dll": b"cuda"}),
    )
    monkeypatch.setattr(install, "ENGINE_FILES", files)
    return files


@pytest.fixture
def cat(monkeypatch):
    """A three-file catalogue: a model, a VAE, and an upscaler without a published sha256."""
    files = {
        "m1": item("m1", "model-a.gguf", A),
        "vae": item("vae", "vae.safetensors", V, "vae"),
        "esr": item("esr", "RealESRGAN_x4plus.pth", E, "upscaler", sha=False),
    }
    monkeypatch.setattr(install.catalogue, "files", lambda: dict(files))
    monkeypatch.setattr(install, "ENGINE_FILES", ())
    return files


def test_install_engine_unzips_and_cleans_up(zips):
    root = paths.engine_dir()
    (root / "master-1-old").mkdir()
    (root / "master-1-old" / "sd-cli.exe").write_bytes(b"old")
    (root / install.MANIFEST).write_text('{"releases": ["master-1-old"]}', encoding="utf-8")
    assert install.engine_exe() is None

    exe = install.install_engine()
    assert exe == root / install.ENGINE_RELEASE / "sd-cli.exe" == install.engine_exe()
    assert (exe.parent / "cudart64_12.dll").read_bytes() == b"cuda"
    assert sorted(p.name for p in root.iterdir()) == [install.MANIFEST, install.ENGINE_RELEASE]
    assert {"sd", "cudart"} <= install.installed()
    assert install.install_engine() == exe  # again, zips gone: still installed


def test_an_engine_with_a_file_gone_is_not_installed(zips):
    exe = install.install_engine()
    dll = exe.parent / "cudart64_12.dll"
    dll.unlink()  # an antivirus quarantined it after the install
    assert install.engine_exe() is None
    with pytest.raises(LatentroomError) as e:  # zips gone: the setup downloads them again
        install.install_engine()
    assert (e.value.code, e.value.message) == ("engine_missing", install.ENGINE_MISSING)
    dll.write_bytes(b"cuda")
    assert install.engine_exe() == exe
    (exe.parent / install.FILE_LIST).unlink()  # unzipped by an older Latentroom: no list
    assert install.engine_exe() == exe


def test_install_deletes_only_what_latentroom_created(zips):
    """The data folder is the user's choice; an engine/ folder already there survives."""
    root = paths.engine_dir()
    (root / "my-notes.txt").write_text("mine")
    (root / "backup").mkdir()
    (root / "backup" / "keep.txt").write_text("mine")
    (root / "master-0-mine").mkdir()  # looks like a release, but Latentroom never made it
    (root / install.MANIFEST).write_text(
        json.dumps({"releases": ["..", "../backup", "C:/Windows", "backup", 7]}), encoding="utf-8"
    )
    install.install_engine()
    assert sorted(p.name for p in root.iterdir()) == sorted(
        ["backup", "my-notes.txt", "master-0-mine", install.ENGINE_RELEASE, install.MANIFEST]
    )
    assert (root / "backup" / "keep.txt").read_text() == "mine"
    assert json.loads((root / install.MANIFEST).read_text()) == {
        "releases": [install.ENGINE_RELEASE]
    }


def test_install_without_zips_is_engine_missing(zips):
    (paths.engine_dir() / "sd.zip").unlink()
    with pytest.raises(LatentroomError) as e:
        install.install_engine()
    assert (e.value.code, e.value.status) == ("engine_missing", 409)


def test_damaged_zip_is_deleted_so_it_downloads_again(zips):
    z = paths.engine_dir() / "cudart.zip"
    z.write_bytes(b"\0" * z.stat().st_size)  # right size, wrong bytes
    with pytest.raises(LatentroomError) as e:
        install.install_engine()
    assert e.value.code == "engine_missing" and not z.exists()
    assert install.engine_exe() is None
    assert not (paths.engine_dir() / (install.ENGINE_RELEASE + ".tmp")).exists()


def test_zips_without_sd_cli_install_nothing(monkeypatch):
    root = paths.engine_dir()
    files = (make_zip(root / "x.zip", {"readme.txt": b"hi"}),)
    monkeypatch.setattr(install, "ENGINE_FILES", files)
    with pytest.raises(LatentroomError) as e:
        install.install_engine()
    assert e.value.code == "engine_missing"
    assert [p.name for p in root.iterdir()] == ["x.zip"]


def test_unzip_checks_disk_space_first(zips, monkeypatch):
    monkeypatch.setattr(install.hardware, "free_disk", lambda p: 0)
    with pytest.raises(LatentroomError) as e:
        install.install_engine()
    assert (e.value.code, e.value.status) == ("no_space", 507)
    assert "Not enough disk space: need 0.2 GB" in e.value.message


@pytest.fixture
def source(tmp_path):
    """A user's folder: a renamed model, a decoy of the same size, files at various depths."""
    src = tmp_path / "my-models"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "renamed.gguf").write_bytes(A)
    (src / "decoy.gguf").write_bytes(b"B" * len(A))  # same size, wrong hash
    (src / "RealESRGAN_x4plus.pth").write_bytes(E)
    deep = src / "1" / "2" / "3" / "4" / "5"  # IMPORT_DEPTH (4) levels of subfolders are searched
    deep.mkdir(parents=True)
    (deep / "vae.safetensors").write_bytes(V)  # one level too deep: not found
    return src


def snapshot(folder):
    return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.rglob("*") if p.is_file()}


def test_import_finds_renamed_files_and_never_touches_the_source(cat, source):
    before = snapshot(source)
    assert sorted(install.import_models(source)) == ["esr", "m1"]
    assert snapshot(source) == before
    dest = paths.models_dir() / "model-a.gguf"
    assert dest.read_bytes() == A
    assert os.path.samefile(dest, source / "sub" / "renamed.gguf")  # same drive: hardlink
    assert install.installed() == {"m1", "esr"}
    assert install.import_models(source) == []  # nothing new
    assert not list(paths.models_dir().glob("*.import"))


def test_upscaler_without_sha_needs_its_own_name(cat, source):
    (source / "RealESRGAN_x4plus.pth").rename(source / "upscaler.pth")
    assert install.import_models(source) == ["m1"]


def test_import_copies_when_a_hardlink_is_impossible(cat, source, monkeypatch):
    def no_link(src, dst):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(install.os, "link", no_link)
    assert sorted(install.import_models(source)) == ["esr", "m1"]
    dest = paths.models_dir() / "model-a.gguf"
    assert dest.read_bytes() == A and not os.path.samefile(dest, source / "sub" / "renamed.gguf")


def test_short_copy_is_an_error_and_not_listed(cat, source, monkeypatch):
    """A copy that comes out short is never reported as imported."""
    (source / "RealESRGAN_x4plus.pth").unlink()
    monkeypatch.setattr(install.os, "link", lambda s, d: (_ for _ in ()).throw(OSError(18, "x")))
    monkeypatch.setattr(
        install.shutil, "copyfile", lambda s, d: Path(d).write_bytes(Path(s).read_bytes()[:1000])
    )
    with pytest.raises(LatentroomError) as e:
        install.import_models(source)
    assert (e.value.code, e.value.status) == ("not_available", 409)
    assert e.value.message == (
        "The copy of renamed.gguf is incomplete. Check the disk and try again."
    )
    assert install.installed() == set() and not list(paths.models_dir().iterdir())


def test_import_copy_checks_disk_space(cat, source, monkeypatch):
    monkeypatch.setattr(install.os, "link", lambda s, d: (_ for _ in ()).throw(OSError(18, "x")))
    monkeypatch.setattr(install.hardware, "free_disk", lambda p: 0)
    with pytest.raises(LatentroomError) as e:
        install.import_models(source)
    assert e.value.code == "no_space"


def test_import_from_a_missing_folder(cat, tmp_path):
    with pytest.raises(LatentroomError) as e:
        install.import_models(tmp_path / "nope")
    assert (e.value.code, e.value.status) == ("bad_request", 400)


@pytest.fixture
def llama(monkeypatch):
    """Smart search's llama.cpp zip downloaded into models/ (pinned to its real size)."""
    z = make_zip(paths.models_dir() / "llama.zip", {"llama-server.exe": b"MZ", "llama.dll": b"dll"})
    z = FileItem(z.id, z.name, z.url, z.size, z.sha256, z.licence, "llama")
    monkeypatch.setattr(install, "LLAMA_ZIP", z)
    monkeypatch.setattr(install.catalogue, "files", lambda: {z.id: z})
    monkeypatch.setattr(install, "ENGINE_FILES", ())
    return z


def test_install_llama_unzips_once_and_keeps_the_zip(llama):
    assert install.llama_exe() is None
    exe = install.install_llama()
    assert exe == paths.engine_dir() / f"llama-{install.LLAMA_RELEASE}-vulkan" / "llama-server.exe"
    assert exe == install.llama_exe() and (exe.parent / "llama.dll").read_bytes() == b"dll"
    assert install.installed() == {llama.id}  # the zip stays: it is the file on disk
    (paths.models_dir() / llama.name).unlink()
    assert install.install_llama() == exe  # unzipped already: no zip needed
    assert [p.name for p in paths.engine_dir().iterdir()] == [exe.parent.name]  # no manifest


def test_llama_with_a_file_gone_unzips_again(llama):
    exe = install.install_llama()
    (exe.parent / "llama.dll").unlink()  # quarantined
    assert install.llama_exe() is None
    assert install.install_llama() == exe and (exe.parent / "llama.dll").read_bytes() == b"dll"


def test_install_llama_without_the_zip_or_with_a_damaged_one(llama):
    z = paths.models_dir() / llama.name
    z.write_bytes(b"\0" * llama.size)  # right size, wrong bytes
    for _ in "damaged", "deleted, so it downloads again":
        with pytest.raises(LatentroomError) as e:
            install.install_llama()
        assert (e.value.code, e.value.status) == ("model_missing", 404)
        assert "Smart search" in e.value.message and not z.exists()
    assert install.llama_exe() is None and list(paths.engine_dir().iterdir()) == []


def test_delete_llama_takes_its_folder_all_or_nothing(llama, monkeypatch):
    exe = install.install_llama()
    listed = (exe.parent / install.FILE_LIST).stat().st_size  # the unzipped-files list
    assert install.disk_use() == {llama.id: llama.size + 2 + 3 + listed}  # zip + unzipped
    real = os.replace

    def busy(src, dst):  # Windows: llama-server.exe runs from that folder
        raise PermissionError(13, "in use")

    monkeypatch.setattr(install.os, "replace", busy)
    monkeypatch.setattr(paths, "FOLDER_WAIT", 0.2)  # held for good: the wait ends, then this
    with pytest.raises(LatentroomError) as e:
        install.delete_model(llama.id)
    assert (e.value.code, e.value.status) == ("not_available", 409)
    assert e.value.message == install.SEARCH_IN_USE
    assert exe.is_file() and (exe.parent / "llama.dll").is_file()  # nothing half-deleted
    monkeypatch.setattr(install.os, "replace", real)
    install.delete_model(llama.id)
    assert list(paths.engine_dir().iterdir()) == [] and install.installed() == set()
    install.delete_model(llama.id)  # already gone: fine


def test_delete_model(cat, monkeypatch):
    f = paths.models_dir() / "model-a.gguf"
    f.write_bytes(A)
    install.delete_model("m1")
    assert not f.exists()
    install.delete_model("m1")  # already gone: fine
    with pytest.raises(LatentroomError) as e:
        install.delete_model("nope")
    assert e.value.code == "not_found"
    monkeypatch.setattr(install, "ENGINE_FILES", ())
    real = install.catalogue.files
    monkeypatch.setattr(
        install.catalogue, "files", lambda: {**real(), "eng": item("eng", "e.zip", b"z", "engine")}
    )
    with pytest.raises(LatentroomError) as e:
        install.delete_model("eng")
    assert e.value.code == "bad_request"


def test_delete_model_cancels_its_download_and_frees_the_part(cat):
    """Deleting a model with a paused download frees the GBs of its .part too."""
    m1 = cat["m1"]
    part = paths.models_dir() / "model-a.gguf.part"
    part.write_bytes(b"x" * 1000)
    st = downloads.DownloadState("m1", m1.name, "paused", 1000, m1.size, 0.0, None, None)
    job = {"item": asdict(m1), "folder": str(part.parent), "state": asdict(st)}
    paths.downloads_state_path().write_text(json.dumps({"downloads": [job]}), encoding="utf-8")
    assert install.disk_use() == {"m1": 1000}

    install.delete_model("m1")
    assert not part.exists() and install.disk_use() == {}
    assert [s.status for s in downloads.progress()] == ["cancelled"]
    part.write_bytes(b"x" * 10)  # a stray .part no download knows about
    install.delete_model("m1")
    assert not part.exists()


def test_disk_use_counts_part_files_and_the_engine(cat):
    (paths.models_dir() / "model-a.gguf").write_bytes(A)
    (paths.models_dir() / "vae.safetensors.part").write_bytes(b"x" * 100)
    eng = paths.engine_dir() / install.ENGINE_RELEASE
    eng.mkdir()
    (eng / "sd-cli.exe").write_bytes(b"x" * 10)
    assert install.disk_use() == {"m1": len(A), "vae": 100, "engine": 10}


def test_list_loras():
    d = paths.loras_dir()
    for name in ("b-style.safetensors", "A-face.SafeTensors", "notes.txt", "c.gguf"):
        (d / name).write_bytes(b"x")
    (d / "folder.safetensors").mkdir()
    assert install.list_loras() == ["A-face", "b-style"]


def test_own_files_list_names_files_never_a_whole_shared_folder(cat, zips):
    """The uninstaller deletes only these lines: each Latentroom file and its .part,
    Latentroom's own engine folders, never models\\ or engine\\ themselves (another app's too)."""
    install.write_own_files()
    lines = (paths.data_dir() / paths.OWN_FILES).read_text(encoding="ascii").splitlines()
    assert lines[0].startswith("#")
    assert {"models\\model-a.gguf", "models\\vae.safetensors.part", "engine\\sd.zip"} <= set(lines)
    assert f"engine\\{install.ENGINE_RELEASE}\\" in lines and "downloads.json" in lines
    for line in lines[1:]:
        assert line.split("\\")[0] in ("models", "engine", "downloads.json"), line
        assert ".." not in line and ":" not in line and line not in ("models\\", "engine\\")
