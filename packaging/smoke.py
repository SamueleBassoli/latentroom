"""Smoke check for a built Latentroom.exe: start it with --server-only, GET / must answer 200
with the HTML page, then stop it. Used by scripts/build.ps1.

Usage: python packaging/smoke.py <path to Latentroom.exe>      exit code 0 = OK
LATENTROOM_HOME must point to a scratch folder, so a check never touches real user data."""

import os
import subprocess
import sys
import threading
import urllib.request


def main(exe: str) -> int:
    if not os.environ.get("LATENTROOM_HOME"):
        print("smoke: set LATENTROOM_HOME to a scratch folder first", file=sys.stderr)
        return 2
    proc = subprocess.Popen([exe, "--server-only"], stdout=subprocess.PIPE, text=True)
    try:
        out = []
        reader = threading.Thread(target=lambda: out.append(proc.stdout.readline()), daemon=True)
        reader.start()
        reader.join(60)
        url = out[0].strip() if out else ""
        if not url.startswith("http://127.0.0.1:"):
            print(f"smoke: no address printed ({url!r}, exit {proc.poll()})", file=sys.stderr)
            return 1
        base = url.split("?")[0]
        with urllib.request.urlopen(base, timeout=10) as r:
            status, ctype = r.status, r.headers.get("Content-Type", "")
        print(f"smoke: GET {base} -> {status} {ctype}")
        return 0 if status == 200 and ctype.startswith("text/html") else 1
    finally:
        proc.kill()  # onedir = one process; the data folder is a scratch one
        proc.wait(10)
        proc.stdout.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
