"""Print one version's part of CHANGELOG.md: the notes of its GitHub release (release.yml).

    python scripts/release_notes.py 0.2.0 [CHANGELOG.md]

Exits 1 when CHANGELOG.md has no "## [0.2.0]" section or it is empty, so a release stops
instead of going out with empty notes. Runs on GitHub's Ubuntu python3: stdlib only.
"""

import sys
from pathlib import Path


def notes(changelog: str, version: str) -> str:
    """The text under "## [<version>]" up to the next "## [" heading; "" when there is none."""
    lines = changelog.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(f"## [{version}]"):
            body = []
            for line in lines[i + 1 :]:
                if line.startswith("## ["):
                    break
                body.append(line)
            return "\n".join(body).strip()
    return ""


def main(argv: list[str]) -> int:
    version = argv[1]
    path = Path(argv[2] if len(argv) > 2 else "CHANGELOG.md")
    text = notes(path.read_text(encoding="utf-8"), version)
    if not text:
        print(
            f"::error::{path.name} has no notes for {version}: rename '## [Unreleased]' to "
            f"'## [{version}] - <date>' (release step 2, top of .github/workflows/release.yml)",
            file=sys.stderr,
        )
        return 1
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
