# Changelog

All notable changes to Latentroom are listed here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions: [SemVer](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-21

Download `Latentroom-Setup-0.1.0.exe` below. Windows will warn that it is not signed: see
[Installing Latentroom](https://github.com/SamueleBassoli/latentroom/blob/main/docs/INSTALL.md).

First version. Needs Windows 10 or 11 (64-bit) and an NVIDIA GeForce RTX card from the 2000 to
the 5000 series. Tested on an RTX 2060 SUPER (8 GB) and an RTX 4070 SUPER (12 GB).

### Added
- Installer for your user only, no administrator password. Uninstalling asks before deleting
  the models and the engine, and never deletes your pictures.
- Setup in five steps: checks your PC, picks the data folder, shows which model fits your card,
  downloads everything once (pause and resume, even after a restart; every file is checked),
  then makes a test picture. If no model fits your card, setup says so and lets you skip it.
  Going back to pick another model pauses the old one's download.
- Text to picture: prompt, size, seed, steps, guidance, negative prompt, several pictures per
  run.
- Pictures are made one at a time, in a queue. One that fails does not stop the rest.
- Pictures are saved in a Latentroom folder inside your Pictures folder, also when OneDrive has
  moved it (then OneDrive uploads them). Change it in Settings → Output folder.
- Edit with references: give one or more pictures and an instruction.
- Pictures you add: photos straight from a phone camera work (MPO files too). Pictures over
  100 megapixels are refused. Latentroom opens a picture on a network path (`\\server\...`)
  only when it is inside your own output folder.
- Enhance prompt: turns a short idea into a full prompt. Undo brings yours back.
- Turbo: faster, a bit less detail, 8 steps. Setup downloads the Turbo add-on too, so Turbo
  works right away.
- Fast models (marked Experimental): Qwen-Image 2.1 Turbo model files, 8 steps, guidance 1.
- LoRA styles: your own `.safetensors` files, several at once, each with a weight.
- Touch up a finished picture, or start From a picture: Upscale ×4, Add detail, Fix faces,
  Repaint a part, Rework, Use as reference. Fix faces needs the optional face detector (Models).
- Library: pictures by day, search, star, delete, and Re-develop with these settings.
- Smart search (optional download): find pictures by what is in them.
- Models: each model is labelled for your card (Full GPU, Partial GPU offload, Too big for this
  PC). Download, delete, or import model files you already have. Every download, including the
  upscaler, is checked against its known SHA-256 fingerprint.
- Use from your phone (optional, off by default): needs Tailscale on both devices. Turn on Use
  from my phone in Settings.
- A step-by-step phone guide, `docs/PHONE.md`. Without Tailscale, Settings shows 3 steps and
  links to get Tailscale and to the guide.
- If the window loses its connection to Latentroom, it says so, keeps trying, and tells you
  when it is back.
- After a crash: the engines Latentroom started close with it, and the next start deletes the
  half-done job files. If the Library's database is damaged, it is kept aside as
  `db.sqlite.broken-<date>` and a new, empty Library starts. Your picture files are not touched.
- Pictures waiting in the queue survive a crash or power cut: the next start queues them again.
  The picture that was being made is tried once more; if Latentroom crashes on it again, it is
  dropped.
- Settings → Check for updates: asks GitHub for the newest version, only when you press it.
- Copy diagnostic report: versions, graphics card and recent errors, never prompts or pictures,
  to paste into a GitHub issue.
- Every saved picture carries an invisible, standard "made with AI" tag (IPTC Digital Source
  Type in XMP) and "Software: Latentroom". No prompt, settings or personal data in the file.
- Clear messages when the engine cannot run: a file blocked by Windows security or an
  antivirus, an engine install with a missing file, not enough video memory, a card the engine
  does not support, or a LoRA that cannot be loaded (the message names it).
- Also explained: a damaged or incomplete model file (download it again), an NVIDIA driver too
  old for the engine, and a graphics card the engine cannot find.
- A picture that comes out as one flat colour is not saved; the job says so.
- Editing a Library picture with the seed it was made with uses the next seed, so the edit
  does not come out oversaturated. The seed actually used is shown.
- Setup warns, without blocking, about a graphics card that is not an RTX, and about a data
  folder inside OneDrive, on a network drive or on a FAT32 drive.
- The PC does not go to sleep while a download or a picture is running.
- Enhance prompt uses the NVIDIA card on PCs with two graphics cards.
- Pictures are decoded with madebyollin's Texture-Fix decoder (VAE), which shows less of the faint
  grid pattern in fine textures.
- Default settings: 40 steps and guidance 2.5 for Standard and Uncensored models; 8 steps and
  guidance 1 for Turbo.
- Latentroom reads the model file in one go before the engine starts, so the model loads quickly.
