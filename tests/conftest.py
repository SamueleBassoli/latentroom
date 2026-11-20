import sys
from pathlib import Path

import pytest

FAKE_SD_CLI = Path(__file__).parent / "fake_sd_cli.py"


@pytest.fixture(autouse=True)
def latentroom_home(tmp_path, monkeypatch) -> Path:
    """Every test gets its own empty data folder; nothing touches the real %LOCALAPPDATA%."""
    home = tmp_path / "latentroom-home"
    home.mkdir()
    monkeypatch.setenv("LATENTROOM_HOME", str(home))
    return home


@pytest.fixture
def fake_engine() -> list[str]:
    """Command that stands in for sd-cli.exe (prepend to jobs.build() arguments)."""
    return [sys.executable, str(FAKE_SD_CLI)]
