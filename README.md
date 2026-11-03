# DeathAdder V2 for Mac

A small menu bar app for the **Razer DeathAdder V2** (USB `1532:0084`) on macOS, with no Razer Synapse needed.
Click the icon in the menu bar for a popover with every setting:

- **Sensor:** DPI (type any value from 100 to 20000), the 1–5 DPI stages the button under the wheel cycles through, and polling rate (125 / 500 / 1000 Hz). It notices when you press the DPI button.
- **Lighting:** logo, scroll wheel or both, with Static, Breathing, Two-tone, Spectrum, Reactive and Random effects, colour swatches plus a custom colour picker, and brightness.
- **Brightness modes:** Off · Manual · **Screen** (follows your MacBook screen's brightness, so it adapts to the room light, with smooth fades) · **Night** (warmer colours and 40% dimmer while Night Shift is on).
- **Buttons:** turn the two thumb buttons and the wheel click into shortcuts (Copy, Paste, Screenshot, Mission Control, any recorded shortcut) or small macros.
- Light and dark themes that follow macOS, hover help (ⓘ) on the advanced settings, and checked for contrast and VoiceOver.

The mouse commands come from the open-source [OpenRazer](https://github.com/openrazer/openrazer) Linux driver, and are
only enabled for this exact model there.

## Requirements

- macOS on Apple Silicon or Intel, with Python 3 (tested with Python 3.15 and macOS 27)
- A Razer DeathAdder V2 connected over USB

## Run it

```bash
python3 -m venv .venv
.venv/bin/pip install hidapi pyobjc-framework-Cocoa pyobjc-framework-Quartz pyobjc-framework-WebKit py2app
.venv/bin/python app.py
```

macOS asks for **Input Monitoring** permission (to talk to the mouse) and, if you turn on custom button actions,
**Accessibility** permission (to remap buttons).

## Build the .app

```bash
./make_signing_cert.sh   # once: a local code-signing certificate, so permissions survive rebuilds
./build.sh               # builds, signs, installs to /Applications and starts it
```

Without the certificate, every rebuild looks like a new app to macOS, and you'd have to grant the permissions again.
Only run one `./build.sh` at a time.

## Logo

The repo has no logo. Put your own `logo.png` (a single-colour shape on a transparent background) and an optional
`icon.icns` next to `app.py` before building, and the app uses them. Without them it shows a mouse symbol.

## Files

| File | What it does |
|---|---|
| `app.py` | Menu bar app: status item, popover, mouse state, settings |
| `ui.html` | The popover's interface (runs in a WKWebView) |
| `razer_da_v2.py` | Talks to the mouse over USB HID; also a small command-line tool (`python razer_da_v2.py status`) |
| `remap.py` | Button remapping and macros (event tap) |
| `ambient.py` | Reads the Mac's screen brightness and Night Shift state |
| `build.sh`, `make_signing_cert.sh`, `setup.py` | Building and signing the .app |

Settings are saved in `~/Library/Application Support/RazerDAV2/config.json`.

## Notes

- The mouse can't report which lighting effect it's running, so the app shows the one it last set.
- Screen brightness and Night Shift are read through private macOS frameworks, so a future macOS update could break
  them. If it does, those modes stop changing anything, but the rest of the app keeps working.

Not affiliated with or endorsed by Razer Inc. "Razer" and "DeathAdder" are trademarks of Razer Inc.

License: MIT (see `LICENSE`). The icons in `ui.html` are from [Tabler Icons](https://tabler.io/icons),
© 2020-2024 Paweł Kuna, also MIT.
