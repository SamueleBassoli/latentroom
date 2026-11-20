"""THIRD-PARTY-NOTICES.txt: the licence texts of everything PyInstaller bundles.

packaging/latentroom.spec calls notices() before it builds and writes the result next to
Latentroom.exe, so the installer puts it next to LICENSE. Sources, in this order: the build
Python's LICENSE.txt, each bundled package's own licence files (dist-info), and
packaging/licenses/ for the texts those lack (its README says where each came from). A
bundled package with no text, or a Python version with no folder there, stops the build.
"""

import importlib.metadata
import platform
import re
import sys
from pathlib import Path

EXTRA = Path(__file__).parent / "licenses"
LICENCE_FILE = re.compile(r"(?i)^(licen[cs]e|copying|notice|authors)")
RULE = "=" * 78


def bundled(dests) -> set[str]:
    """Distribution names behind PyInstaller TOC destinations ("PIL/_imaging.pyd" -> pillow)."""
    owners = importlib.metadata.packages_distributions()
    tops = {re.split(r"[\\/.]", d)[0] for d in dests}
    return {name for top in tops for name in owners.get(top, [])} - {"latentroom"}


def _texts(paths) -> str:
    return "\n\n".join(f"--- {n}\n\n{p.read_text(encoding='utf-8').strip()}" for n, p in paths)


def notices(dists, base_prefix: str = sys.base_prefix, extra: Path = EXTRA) -> str:
    v = sys.version_info
    pyver = extra / f"python-{v.major}.{v.minor}"
    py = [("LICENSE.txt", Path(base_prefix, "LICENSE.txt"))]
    py += [(p.name, p) for p in sorted(pyver.glob("*"))]
    parts = [(f"Python {platform.python_version()}", py)]
    missing = [] if pyver.is_dir() else [pyver.name]
    for name in sorted(dists, key=str.lower):
        dist = importlib.metadata.distribution(name)
        own = [
            f
            for f in dist.files or []
            if ".dist-info" in str(f) and ("licenses" in f.parts or LICENCE_FILE.match(f.name))
        ]
        files = [(f.name, Path(dist.locate_file(f))) for f in own]
        files += [(p.name, p) for p in sorted((extra / name).glob("*"))]
        if not files:
            missing.append(name)
        parts.append((f"{name} {dist.version}", files))
    # Parts inside a package that its own licence files do not cover (WebView2 in pywebview).
    for folder in sorted(p for p in extra.iterdir() if p.is_dir()):
        if folder.name not in dists and not folder.name.startswith("python-"):
            parts.append((folder.name, [(p.name, p) for p in sorted(folder.glob("*"))]))
    if missing:
        raise SystemExit(f"No licence text for {missing}: add it to packaging/licenses/ (README)")
    head = (
        "Third-party software in Latentroom\n\n"
        "Latentroom itself is GPL-3.0: see LICENSE, next to this file. It contains the software\n"
        "below, each under its own licence, whose full text follows. Also inside, not listed\n"
        "here: Qwen's prompt-rewrite instructions, Qwen Research License (non-commercial), with\n"
        "their LICENSE and NOTICE in _internal\\latentroom\\data\\prompt_rewrite\\. Listed too,\n"
        "not inside: the NOTICE of the Texture-Fix decoder (VAE) that setup downloads.\n"
        "Overview: docs/LICENCES.md in the source code."
    )
    body = "\n\n".join(f"{RULE}\n{title}\n{RULE}\n\n{_texts(files)}" for title, files in parts)
    return f"{head}\n\n{body}\n"
