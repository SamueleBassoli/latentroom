# Licence texts the wheels and the Python install do not carry

`packaging/notices.py` puts these into `THIRD-PARTY-NOTICES.txt` (next to `Latentroom.exe`),
together with Python's own `LICENSE.txt` and the licence files in each bundled package's
metadata. A folder named after a bundled package adds to that package's part; any other
folder (WebView2) gets a part of its own. The build stops when a bundled package has no text
at all. Texts are verbatim; where they came from:

| File | What it covers | Source |
|---|---|---|
| `python-3.14/INCORPORATED.rst` | Code inside Python: OpenSSL, expat, libffi, zlib, libmpdec, mimalloc, the zstd bindings, … | CPython `Doc/license.rst` at tag `v3.14.8`, from the heading *Licenses and Acknowledgements for Incorporated Software* to the end |
| `python-3.14/bzip2.txt`, `liblzma.txt`, `sqlite.txt` | bzip2 1.0.8 (`_bz2.pyd`), xz 5.8.4 (`_lzma.pyd`), SQLite 3.53.1 (`sqlite3.dll`) | `LICENSE.bzip2.txt`, `LICENSE.liblzma.txt`, `LICENSE.sqlite.txt` of [python-build-standalone](https://github.com/astral-sh/python-build-standalone) at tag `20261013`, the build uv installs |
| `python-3.14/zstd.txt` | Zstandard 1.5.7 (`_zstd.pyd`) | `LICENSE` of [facebook/zstd](https://github.com/facebook/zstd) at tag `v1.5.7` |
| `Microsoft.Web.WebView2/` | `Microsoft.Web.WebView2.*.dll` and `WebView2Loader.dll` inside pywebview 6.2.1 (file version 1.0.3856.49) | `LICENSE.txt` and `NOTICE.txt` of the NuGet package `Microsoft.Web.WebView2` 1.0.3856.49 |
| `nvidia-ml-py/LICENSE.txt` | nvidia-ml-py 13.615.71 | The licence header of its `pynvml.py`, comment marks removed |
| `proxy_tools/LICENSE.txt` | proxy_tools 0.1.0 (its code says `:license: BSD, see LICENSE`; PyPI lists MIT, but the package ships no text) | `LICENSE.txt` of [jtushman/proxy_tools](https://github.com/jtushman/proxy_tools), commit `ccd35a5` |
| `texture-fix-vae-for-qwen-image-2.1/NOTICE` | The Texture-Fix decoder (VAE) setup downloads, not bundled: its Qwen Research License notice | `NOTICE` of [madebyollin/texture-fix-vae-for-qwen-image-2.1](https://huggingface.co/madebyollin/texture-fix-vae-for-qwen-image-2.1), commit `9a10df8` |

New Python version (3.15): the build stops until a `python-3.15/` folder exists. Package
updated or removed: refresh or delete its folder.
