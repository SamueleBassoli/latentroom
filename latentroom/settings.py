"""User settings, stored as JSON in paths.settings_path().

`data_dir` is also read directly by paths.data_dir(): keep that key name and meaning."""

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from latentroom.engine import paths

log = logging.getLogger(__name__)


@dataclass
class Settings:
    data_dir: str = ""  # "" = paths.app_dir(); engine, models, LoRAs and history live here
    output_dir: str = ""  # "" = paths.default_output_dir(); finished PNGs go here
    max_vram_gb: float = 0  # 0 = let the engine decide (--auto-fit); > 0 = --max-vram cap
    adult_ok: bool = False  # 18+ notice for uncensored models acknowledged
    first_run_done: bool = False  # wizard finished
    model_id: str = ""  # catalogue Model.id used by default on the main screen
    phone_enabled: bool = False  # v2 "Use from my phone"


def _type_ok(value: object, default: object) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, type(default))


def load() -> Settings:
    """Read settings.json; missing or unreadable file -> defaults (never raises).
    A key with the wrong type (hand-edited file) falls back to its default."""
    saved = paths.read_json(paths.settings_path())
    if not isinstance(saved, dict):
        return Settings()
    defaults = Settings()
    good = {
        f.name: saved[f.name]
        for f in fields(Settings)
        if f.name in saved and _type_ok(saved[f.name], getattr(defaults, f.name))
    }
    return Settings(**good)


def save(settings: Settings) -> None:
    """Write settings.json atomically (temp file + os.replace)."""
    write_json_atomic(paths.settings_path(), asdict(settings))


def write_json_atomic(target: Path, data: object) -> None:
    """A crash mid-write leaves the old file intact, never a half-written one."""
    fd, tmp = tempfile.mkstemp(prefix=target.stem + "-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        paths.replace(tmp, target)  # a reader or a virus scan holding the target: waited out
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            log.warning("could not remove a temporary settings file")
        raise
