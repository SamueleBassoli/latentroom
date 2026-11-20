# Installing Latentroom

Latentroom makes and edits pictures on your own PC, with your own graphics card. Nothing is
sent to the internet except the one-time downloads you start yourself, and the update check
when you press *Check for updates* in Settings. The first time you use Enhance prompt, it
downloads the small llama.cpp engine (about 35 MB) from github.com, if Smart search has not
already.

## What you need

- Windows 10 or 11, 64-bit.
- An NVIDIA GeForce RTX graphics card from the 2000 to the 5000 series (the 5000 series is
  untested), with driver 572.61 or newer.
- Tested on an RTX 2060 SUPER (8 GB) and an RTX 4070 SUPER (12 GB). Cards with less video
  memory are only checked by Latentroom's fit check: it lets the smallest model (a 2.4 GB file)
  run with 2.5 GB of video memory at 512 × 512, and 3.5 GB at 1024 × 1024.
- Free disk space: about 10 GB for the engine and shared parts, plus 2.4 to 7.2 GB per model.
- Microsoft Edge WebView2. Most PCs already have it. If yours does not, the installer
  says so and offers Microsoft's free download.
- Internet for setup. It must reach github.com (the engines) and huggingface.co (the models),
  and the download servers they send the files from: release-assets.githubusercontent.com and
  addresses ending in hf.co. There is no mirror: if your network blocks these, setup cannot
  finish.

## 1. Download

Download `Latentroom-Setup-<version>.exe` from Latentroom's
[Releases page](https://github.com/SamueleBassoli/latentroom/releases/latest). Save it, then
double-click it. The `.sha256` file next to it lets you check the download (see the README).

## 2. "Windows protected your PC"

Windows will probably show a blue box saying it does not know this program.

1. Click **More info**.
2. Click **Run anyway**.

Why this happens: Windows trusts programs that are "signed", meaning their maker paid for a
certificate. Latentroom is not signed, because a certificate costs money every year. The
warning says "unknown", not "dangerous". Only click Run anyway on a file you downloaded from
Latentroom's Releases page.

## 3. Install

Click through the installer. It does not ask for an administrator password: Latentroom is
installed for you only, in `%LOCALAPPDATA%\Programs\Latentroom`. You get a Start menu entry
and a desktop icon (untick the box if you do not want one).

## 4. First run

Open Latentroom from the Start menu. A short setup checks your graphics card, then downloads
the image engine, its shared parts and one model once (about 10 to 16 GB, this takes a while). After that, Latentroom works
offline.

## Blocked by Windows

Latentroom may say: **"Windows blocked the Latentroom engine (Smart App Control or your
antivirus)."** Windows refused to start one of the engine programs Latentroom downloaded:
`sd-cli.exe` makes the pictures, `llama-server.exe` runs Smart search and Enhance prompt. Like
Latentroom itself, they are not signed (see step 2), and two Windows protections can stop them.

**Your antivirus** (Microsoft Defender, or one you installed). A name like
`Trojan:Win32/Wacatac.C!ml` on an engine file is a known false alarm for these engine builds
([llama.cpp issue 26343](https://github.com/ggml-org/llama.cpp/issues/26343)).

1. Open Start → **Windows Security** → **Virus & threat protection** → **Protection history**.
2. Look for an entry that names `sd-cli.exe`, `llama-server.exe` or a file in Latentroom's
   data folder.
3. Select it and choose **Restore** (or **Allow**). Another antivirus has its own list of
   blocked files: allow the same file there.
4. So it does not happen again, add an exclusion for the `engine` folder inside Latentroom's
   data folder ([Where your files are](#where-your-files-are)): in **Windows Security** →
   **Virus & threat protection** → **Manage settings** → **Add or remove exclusions** →
   **Add an exclusion** → **Folder**. Another antivirus has its own exclusions.
5. If the file is gone, press **Run the setup again** in Settings. Then try again in Latentroom.

**Smart App Control** (Windows 11): open Start → **Windows Security** → **App & browser
control**. If Smart App Control is **On**, it may be what blocked the engine.

- It cannot make an exception for one program. Microsoft says so: the only way past it is to
  turn it off, and that lowers your protection against every unknown program, not just
  Latentroom. Do not turn it off lightly.
- On older versions of Windows 11, once turned off it could only be turned back on by resetting
  or reinstalling Windows. Recent updates let you turn it back on in the same place.

Still blocked? Copy the diagnostic report from Settings and
[open an issue](https://github.com/SamueleBassoli/latentroom/issues).

## Updating

Press *Check for updates* in Settings to see if a new version is out. To update, download the
new installer from the Releases page, close Latentroom and run it. Your models, settings and
pictures stay.

## Uninstalling

Close Latentroom first. Then: Start → Settings → Apps → Installed apps → Latentroom → Uninstall.
(Windows 10: Start → Settings → Apps → Apps & features → Latentroom → Uninstall.)

The uninstaller asks one question: **delete the downloaded models and the image engine?**

- **No** (the default): they stay, and a reinstall does not download them again.
- **Yes**: only the files Latentroom downloaded are deleted, and you get several GB back. Other
  files in the same folders (another app's models, for example) are never touched.

Your pictures are **never** deleted. The last message tells you where they are, and where
your settings, Library and own LoRA files were kept.

## Where your files are

| What | Where |
|------|-------|
| The program | `%LOCALAPPDATA%\Programs\Latentroom` |
| Settings and logs | `%LOCALAPPDATA%\Latentroom` |
| The data folder: models, engine, your LoRA files, the Library list | `%LOCALAPPDATA%\Latentroom`, or the folder you picked in setup (a `Latentroom` folder inside it, when it already held other files) |
| Pictures you add: dropped in, picked from your PC, or sent from your phone | `uploads`, inside the data folder |
| Your pictures | A `Latentroom` folder inside your Pictures folder, or the folder you picked in Settings |

- You can paste the `%LOCALAPPDATA%` paths into the address bar of File Explorer.
- In Settings, *Output folder* (your pictures) and *Data folder* each have a folder button that
  opens them in File Explorer.
- Pictures you add are copied into `uploads`; your originals are never changed. Each time
  Latentroom starts, it deletes the copies no Library picture uses. The uninstaller leaves the
  rest.
- Your pictures carry an invisible, standard "made with AI" tag (the IPTC Digital Source Type,
  which photo apps and websites can read), plus "Software: Latentroom". Nothing else: no
  prompt, no settings, no date, no name. A picture made from one of your photos is tagged
  "edited with AI" instead.
- OneDrive: if OneDrive backs up your Pictures folder, it uploads Latentroom's pictures too. To
  keep them off OneDrive, pick another folder: Settings → *Output folder* → **Change…**.
  Pictures you already made stay where they are.
