# DualSense Wired

[Русский](README.md) · **English**

A [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) plugin: a DualSense / DualSense Edge
connected to the Steam Deck over Bluetooth works in Steam and games just like one connected with a cable —
including the controller speaker and HD haptics in games that support them.

Tested on the Steam Deck (SteamOS, Proton 11): Marvel's Spider-Man Remastered, Miles Morales,
Helldivers 2 — speaker, HD haptics and adaptive triggers work without per-game setup.

## Features

- **Wired mode over Bluetooth.** Games and Steam see a USB DualSense: adaptive triggers, lightbar,
  gyro, touchpad and the PS button work as over a cable.
- **Game audio to controller** (on by default). Whatever a game plays to the DualSense speaker and
  HD haptics is sent to the controller over Bluetooth — like on a PC with a cable.
- **All Deck audio to controller** (off by default). Instead of game audio, the controller speaker
  plays everything the Deck plays and haptics are derived from it — for games without DualSense support.
- Speaker volume and haptics strength are set in the plugin panel.
- Reconnecting the controller during a game keeps audio and triggers: the virtual controller waits
  2 minutes for it to come back.
- Controller battery is shown in the plugin panel, with Steam notifications at 20% and 10%: Steam shows
  the battery in its top bar only for wireless controllers, and to Steam this one is wired.
- The panel is in English, or in Russian when the Steam UI is in Russian.

**Steam Input must be turned off** for the game (otherwise the game sees an Xbox controller).

## Installation

Requires [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader).

1. In Decky: ⚙ **Settings → General** → turn on **Developer mode**.
2. On the new **Developer** tab choose **Install Plugin from URL** and paste the link to the archive from
   the [latest release](https://github.com/mrFunkill/decky-dualsense-wired/releases/latest), e.g.
   `https://github.com/mrFunkill/decky-dualsense-wired/releases/download/v0.5.1/decky-dualsense-wired-v0.5.1.zip`.
   Or download the archive to the Deck and choose **Install Plugin from ZIP File**.
3. Confirm the installation. The plugin runs as root — this is needed for the virtual USB controller.
4. Connect the DualSense over Bluetooth. In the game's properties **turn off Steam Input**.

Uninstall it normally through Decky: it hands the controller back to Steam and removes its service
from the Proton prefixes.

## How it works

The backend (`main.py`, `py_modules/`) runs as root (`root` flag in `plugin.json`).

1. **Virtual USB controller** (`dualsense_gadget_bridge.py`, `dualsense_gadget.py`). Each Bluetooth
   DualSense is exposed as a USB device: a USB gadget (configfs, `f_hid`) on the kernel's virtual bus
   `dummy_hcd`, with its HID on interface 3 like a real DualSense. Reports are translated between the
   Bluetooth (`0x31`) and USB (`0x01`/`0x02`) formats; service requests are forwarded to the controller.
2. **Controller audio device.** A 4-channel PipeWire sink `dualsense_gadget_<port>` is attached to the
   same USB device (`sysfs.path`), so Wine gives it and the HID the same Container ID. Channels 1–2 are
   the speaker/headset, 3–4 the two haptic actuators, as on a wired DualSense.
3. **Service in Proton prefixes** (`proton_prefixes.py`, `bin/dualsense_wired_helper.exe`, source in
   `native/helper`). Games find the controller's audio device through `SWD\MMDEVAPI\…` device nodes with
   the shared Container ID, which Wine does not create. The service creates them when the prefix starts.
   Every minute the plugin installs it into all Proton prefixes that are not running.
4. **Streaming to the controller** (`dualsense_audio.py`). Audio is captured from the sink and encoded
   with Opus, haptics are sent at 3 kHz — in the DualSense's extended Bluetooth reports (`0x36`/`0x39`)
   described in `research/FINDINGS.md`. Game output reports (triggers, lightbar) ride in the same stream.
   Nothing is streamed while there is no sound: the Deck's Bluetooth shares the radio with Wi-Fi, and
   when the link congests BlueZ drops the input device.

Every Bluetooth DualSense gets its own virtual USB port (`dummy_hcd` is loaded with `num=4`): its own
bus, Container ID and `dualsense_gadget_<port>` sink, so games pair the audio with the right
controller. A fifth and further controllers, and a DualSense connected with a cable, use the older mode
(`uhid`, `dualsense_bridge.py`): no game audio, harmful commands filtered. The plugin also falls back to
this mode if the virtual bus cannot be created.

## What the plugin changes on the system

| What | Where | Removed |
|---|---|---|
| udev rule hiding the original Bluetooth controller from Steam | `/etc/udev/rules.d/72-decky-dualsense-wired.rules` | when emulation is turned off and when the plugin unloads |
| Kernel modules `dummy_hcd`, `libcomposite`, `usb_f_hid`, `usb_f_ss_lb`, `usb_f_uac2` | loaded when a virtual controller is created | stay loaded until reboot |
| PipeWire sink `dualsense_gadget_<port>` | session of user `deck` | together with the virtual controller |
| Service `DualSenseWiredHelper` and `dualsense_wired_helper.exe` | every prefix `steamapps/compatdata/*/pfx` | when the plugin is uninstalled (prefixes running at that moment are skipped) |

Prefix registries are only changed while the prefix is not running, keeping the file owner.

## Limitations

- Game audio and HD haptics for up to four controllers at once; only tested with one so far.
- Headphones in the controller jack are not supported: over Bluetooth the sound always goes to the
  controller speaker (the audio control byte does not switch the output, see `research/FINDINGS.md`).
- The controller microphone is not supported: the protocol for audio from the controller over
  Bluetooth is unknown (DSX does not have it either).
- A newly installed game needs one launch to create its prefix; controller audio works from the
  second launch.
- Some games use the DualSense only as an XInput controller (e.g. Hogwarts Legacy — on PC too);
  DualSense features are not available in them.
- No controller battery icon in Steam's top bar (see Features).

## Building

Requires Node.js 16.14+ and pnpm v9:

```bash
pnpm i
pnpm run build
```

The prefix service (`bin/dualsense_wired_helper.exe`) is built with zig (`python -m pip install ziglang`):

```bash
python -m ziglang cc -target x86_64-windows-gnu -O2 -s -municode \
  -o bin/dualsense_wired_helper.exe native/helper/dualsense_wired_helper.c -lole32 -ladvapi32
```

## Deploying to the Steam Deck over SSH (Windows)

On the Deck, once: set a password (`passwd`) and enable SSH (`sudo systemctl enable --now sshd`).

```powershell
./deploy.ps1 -Setup -DeckHost <deck-ip>   # once: SSH key and install helper on the Deck
./deploy.ps1                              # build and upload; Decky reloads the plugin itself
./deploy.ps1 -Logs                        # follow the plugin log
./deploy.ps1 -Zip                         # only build out/<name>-v<version>.zip
```

The Deck address is stored in `deck.json` (not in git), the key in `~/.ssh/steamdeck_ed25519`.
Reloading the plugin re-creates the virtual controller — better not during a game.

## Research tools

`py_modules/dualsense_audio.py` is a command-line tool (run as root on the Deck):
`info`, `haptics`, `tone`, `stream [--source system|controller]`, `buttons`, `inputcheck`, `levels`,
`routing` (listening test for speaker/headset routing),
`gadget` (virtual controller experiment with the plugin paused), `gadget-stop`.
`native/cfgmgr32` — a prototype `CM_Get_Parent` replacement for Wine (`python native/cfgmgr32/build.py`)
and the `devtree.c` / `dsinfo.c` tools for comparing Wine's and Windows' device trees
(`python -m ziglang cc -target x86_64-windows-gnu -municode <file>.c -lcfgmgr32 -lsetupapi -lole32 -lpropsys`).
Protocol research notes (in Russian) are in `../research/FINDINGS.md`.
