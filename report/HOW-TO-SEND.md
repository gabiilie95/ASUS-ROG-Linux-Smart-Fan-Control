# Reporting the crackle

Two drafts, for two audiences. Nothing here has been sent.

## 1. Linux kernel developers: `linux-usb-report.txt`

The useful question for them is whether the USB controller driver can send packets late instead of
dropping them when the chipset stalls, and whether the stall is a known chipset behaviour.

- **Where:** email to `linux-usb@vger.kernel.org`, Cc the xHCI maintainer and `linux-sound@vger.kernel.org`.
  The addresses in the draft were checked against the kernel's MAINTAINERS file on 2026-11-16.
- **Format:** plain text only. The list rejects HTML mail. In Gmail: three-dot menu in the compose
  window → "Plain text mode". No subscription is needed; replies are Cc'd to you.
- **Attachments:** the four files in `evidence/` are a few kilobytes each and can be attached or
  pasted below the signature. Do not attach the full capture (3 MB); offer it, as the draft does.
- **Your name and address** will be public and archived permanently (lore.kernel.org).
- **Before sending:** replace `<your name>`. Read it once; everything in it is a measurement from
  this machine, but it is your name on it.
- **Expect to be asked** to reproduce on a recent mainline kernel without the NVIDIA modules,
  because the kernel is marked tainted. The draft already offers that.

Alternative: https://bugzilla.kernel.org (Product "Drivers", Component "USB"). It accepts the full
capture as an attachment, but USB maintainers mostly work from the mailing list.

## 2. ASUS, or the asus-linux community: `asus-summary.txt`

Plain-language version about the fan curve and the hot board area. Use it for an ASUS support
ticket, or as an issue at https://gitlab.com/asus-linux/asusctl if you would rather ask the people
who maintain the Linux ASUS tools whether the default curve is known to be too low.

## Evidence files (`evidence/`)

- `hardware.txt` – machine, controllers, kernel, device
- `usbmon-excerpt.txt` – 28 lines of the USB capture around one stall, audio payload removed
- `dmesg-excerpts.txt` – kernel messages, hostname replaced
- `measurements.txt` – the numbers behind every claim in the drafts

Redacted: hostname, disk UUID, the audio interface's serial number, and the audio payload.
The full capture is `../data/usbmon-volt-2b5a-0021-30s.txt.gz`; it contains raw audio sample bytes
(game and video sound) but nothing else personal.
