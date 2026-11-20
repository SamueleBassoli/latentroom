# Licences of the parts Latentroom uses

Latentroom includes no model. Setup downloads each part from its publisher, and each comes with
its own licence. Qwen-Image models: research or evaluation use only, no commercial use. See
[README > Licences](../README.md#licences). Whether the pictures you make may be used
commercially, Qwen has not clearly answered
([open question](https://huggingface.co/Qwen/Qwen-Image-2.1/discussions/52)): ask Qwen before
any commercial use. This is not legal advice.

## Bundled with Latentroom, not under GPL-3.0

This file ships inside Latentroom and its installer, but it is not under Latentroom's GPL-3.0: if
you share or change Latentroom, it keeps its own licence. The Python libraries and fonts inside
the app are listed at the end of this page.

| Part | Source | Licence |
|---|---|---|
| Instructions for Enhance prompt: `latentroom/data/prompt_rewrite/system_prompt_t2i.txt`, Qwen's official text-to-image rewrite prompt, unchanged | [QwenLM/Qwen-Image-2.1](https://github.com/QwenLM/Qwen-Image-2.1), commit `7307809` | Qwen Research License, non-commercial, not GPL-3.0. Copy and notice next to it: [LICENSE](../latentroom/data/prompt_rewrite/LICENSE), [NOTICE](../latentroom/data/prompt_rewrite/NOTICE) |

## Downloaded by setup or by you

| Part | Source | Licence |
|---|---|---|
| Image engine: stable-diffusion.cpp, CUDA 12 build `master-994-e779d4f` | [leejet/stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp) | MIT |
| CUDA runtime, shipped with that engine release | same release | NVIDIA CUDA Toolkit EULA (redistributable runtime) |
| Qwen-Image 2.1 models (GGUF) | [leejet/Qwen-Image-2.1-GGUF](https://huggingface.co/leejet/Qwen-Image-2.1-GGUF) | Qwen Research License, non-commercial |
| Uncensored fine-tune (GGUF), 18+, quantised by abenzerps, mirrored by KasugaiSakura | [KasugaiSakura/Qwen-Image-2.1-Uncensored-Abenzerps-GGUF](https://huggingface.co/KasugaiSakura/Qwen-Image-2.1-Uncensored-Abenzerps-GGUF) | Qwen Research License, non-commercial |
| Turbo models (GGUF), the Fast levels in Models | [unsloth/Qwen-Image-2.1-Turbo-GGUF](https://huggingface.co/unsloth/Qwen-Image-2.1-Turbo-GGUF) | Qwen Research License, non-commercial |
| Turbo add-on | [Comfy-Org/Qwen-Image-2.1](https://huggingface.co/Comfy-Org/Qwen-Image-2.1) | Qwen Research License, non-commercial |
| Decoder (VAE), Texture-Fix | [madebyollin/texture-fix-vae-for-qwen-image-2.1](https://huggingface.co/madebyollin/texture-fix-vae-for-qwen-image-2.1) | Qwen Research License, non-commercial |
| Text and vision encoder: Qwen3-VL-8B-Instruct (GGUF), also used by Enhance prompt | [Qwen/Qwen3-VL-8B-Instruct-GGUF](https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct-GGUF) | Apache-2.0 |
| Upscaler: Real-ESRGAN x4plus | [xinntao/Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN) | BSD-3-Clause |
| Face detector, optional (YOLOv8n) | [exeterminal/adetailer-yolov8-safetensors](https://huggingface.co/exeterminal/adetailer-yolov8-safetensors) | AGPL-3.0, downloaded only if you choose, never bundled |
| llama.cpp, Windows Vulkan build `b11850`, for Smart search and Enhance prompt | [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp) | MIT |
| Smart search, optional: EmbeddingGemma 2 (GGUF) | [ggml-org/embeddinggemma-2-GGUF](https://huggingface.co/ggml-org/embeddinggemma-2-GGUF) | Apache-2.0 |

## Python and libraries in the app

Their full licence texts are in `THIRD-PARTY-NOTICES.txt`, next to `Latentroom.exe` and
`LICENSE` in the app folder. The build makes it from what it actually bundles
(`packaging/notices.py`) and stops when a part has no licence text.

| Part | Licence |
|---|---|
| Python 3.14 | PSF License |
| Inside Python: OpenSSL | Apache-2.0 |
| Inside Python: expat, libffi, mimalloc, zlib-ng, bzip2, libmpdec, Zstandard | MIT, zlib, BSD-style |
| Inside Python: SQLite / xz (liblzma) | public domain / 0BSD |
| Microsoft Visual C++ runtime (`VCRUNTIME140.dll`), shipped with Python | Microsoft Distributable Code, terms in Python's licence |
| [pywebview](https://github.com/r0x0r/pywebview) 6.2.1 | BSD-3-Clause |
| Inside pywebview: Microsoft WebView2 SDK 1.0.3856.49 (`WebView2Loader.dll`, `Microsoft.Web.WebView2.*.dll`) | Microsoft, BSD-3-Clause style |
| [Pillow](https://github.com/python-pillow/Pillow) 13.0.0, with FreeType and the other image libraries in its licence file | MIT-CMU, and theirs |
| [nvidia-ml-py](https://pypi.org/project/nvidia-ml-py/) 13.615.71 | BSD |
| [segno](https://github.com/heuer/segno) 1.6.6 | BSD |
| Their dependencies: bottle, pythonnet, clr_loader | MIT |
| Their dependency: proxy_tools 0.1.0 (code from Werkzeug) | BSD, as its code says; PyPI lists MIT |
| Their dependencies: cffi / pycparser / typing_extensions | MIT-0 / BSD-3-Clause / PSF-2.0 |
| Pulled in by cffi: setuptools (and the libraries it carries) / packaging | MIT / Apache-2.0 or BSD-2-Clause |
| [Geist and Geist Mono](https://github.com/vercel/geist-font) fonts | SIL Open Font License 1.1 |
