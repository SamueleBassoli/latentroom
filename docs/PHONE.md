# Use Latentroom from your phone

Latentroom runs on your PC. With *Use from my phone* you open it in your phone's browser, at
home or away. It works through Tailscale, which makes a private, encrypted link between your
own devices. Latentroom is never on the open internet: only devices on your own Tailscale
network can reach it, and only phones you pair can use it. Pictures are still made on the PC,
so the PC must stay on, with Latentroom open.

## What you need

- A free Tailscale account. Tailscale is a separate service, run by another company. The
  account and its login belong to Tailscale, not to Latentroom. Personal use is free, for up to
  6 people.
- A Google, Microsoft, Apple or GitHub account to sign in to Tailscale with. Tailscale has no
  email-and-password sign-up of its own.
- Your PC with Latentroom, and your phone (Android or iPhone).

## Step 1: Tailscale on the PC

1. Go to [tailscale.com/download](https://tailscale.com/download) and download Tailscale for
   Windows.
2. Run the installer. A Tailscale icon appears next to the clock (if you do not see it, click
   the small up arrow there).
3. Right-click the Tailscale icon → **Log in**. Your browser opens.
4. Sign in with your Google, Microsoft, Apple or GitHub account. If Tailscale asks what it is
   for, choose **Personal use**.

## Step 2: Tailscale on the phone

1. Install **Tailscale** from Google Play (Android) or the App Store (iPhone).
2. Open it and tap **Get Started**.
3. Allow the VPN configuration when the phone asks: this is how Tailscale makes the private
   link. Allow notifications too: Tailscale uses them to tell you when to sign in again.
4. Sign in with **the same account** you used on the PC:
   - Android: tap **Sign in with Google** for the phone's Google account, or **Sign in with
     other** for any other account.
   - iPhone: tap **Log in**.
5. Check that Tailscale is switched on (connected) in the app.

## Step 3: Latentroom

1. On the PC, open Latentroom, then **Settings** (the gear at the top right).
2. Under **Phone**, turn on **Use from my phone**.
3. Press **Pair a phone**. A QR code and a 6-digit code appear.
4. On the phone, point the camera at the QR code and open the link. The phone pairs by itself,
   and the PC says "Your phone is paired."
5. No camera? Open the **Address** shown on the PC in the phone's browser, exactly as written,
   type the 6-digit code under **Pairing code**, then tap **Pair this phone**.

A code works once, for 5 minutes. You pair each phone once: next time, just open the same
address. Add it to your bookmarks or your home screen.

## If it does not work

- **The switch is greyed out, and Settings says "No Tailscale address found on this PC."**
  Tailscale is not installed on the PC, or not signed in. Do Step 1, then press **Check
  again**.
- **The page does not open on the phone.**
  - Tailscale is switched off on the phone: switch it on.
  - The phone and the PC are signed in to different Tailscale accounts. To check, open
    [console.tailscale.com/admin/machines](https://console.tailscale.com/admin/machines) and
    sign in: the PC and the phone must both be in the list. If one is missing, sign in on it
    again with the right account.
  - The PC is asleep or Latentroom is closed. Latentroom keeps the PC awake only while it
    makes a picture or downloads a model. Wake the PC and open Latentroom.
  - *Use from my phone* is off on the PC: turn it on.
  - The address changed. This happens when another program uses Latentroom's usual port.
    Open Settings on the PC and use the **Address** shown there.
- **"This code is too old. Make a new one."** on the PC, or **"That code is wrong or too
  old."** on the phone: press **New code** (or **Make a new code**) on the PC and use the new
  code.
- **"Too many wrong codes."** Wait a minute and try again.
- **The phone shows "Pair this phone" again.** It was forgotten on the PC, its browser data was
  cleared, or its pairing ran out (after 400 days). Pair it again with a new code.
- **Tailscale asks you to sign in again.** By default it does this about every 6 months, for
  your security. Sign in again on that device.

## Turning it off

- **Settings → Use from my phone**: turn the switch off. Phones can no longer open
  Latentroom. Paired phones are remembered and can connect again when you turn it back on.
- **Forget all phones** (shown while the switch is on and a phone is paired): every paired
  phone loses access at once. To use a phone again, pair it with a new code. Your pictures
  stay on the PC.
- Tailscale stays on your devices. To remove it, uninstall it like any other app.
