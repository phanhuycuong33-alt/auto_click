# auto-click-copilot

GUI automation ("vision computer" style): opens Firefox at
**copilot.microsoft.com**, finds the "+" button by **screen capture +
template matching** (OpenCV), attaches a screenshot to the chat, types
`can you explain what image`, and hits Send.

No browser automation tools — just screen capture, image matching, and mouse
control, exactly like a person clicking.

## How it knows where buttons are

1. The script captures the whole screen (`pyautogui.screenshot()`).
2. It runs OpenCV template matching (`cv2.matchTemplate`) against small
   reference images of each button stored in `templates/*.png`.
3. The best match gives screen pixel coordinates → mouse moves there → click.
4. Multi-scale matching tolerates zoom/DPI differences; OCR (tesseract) is a
   text fallback for buttons like "Add image".

## Requirements (what each file is for)

| file                 | purpose                                                           |
|----------------------|-------------------------------------------------------------------|
| `requirements.txt`   | **Python packages only** (installed by `pip install -r ...`)      |
| `install.sh`         | Ubuntu system packages (`scrot`, `tesseract-ocr`) **+** pip deps, both in one shot |
| `auto_click_copilot.py` | the main script                                                |
| `capture_template.py`   | helper to crop button reference images                        |
| `templates/`         | your button PNGs go here (see `templates/README.md`)              |

## Option B: keyboard / accessibility version (no templates at all)

`copilot_keyboard.py` does the same flow but **never needs template crops**.
It reads the buttons directly from Linux's accessibility tree (AT-SPI) and
activates them with the keyboard; if that is unavailable it falls back to
blind Tab-walking (tune `TAB_*` constants at the top of the file).

```bash
python3 copilot_keyboard.py --dump    # first: discover real button names
python3 copilot_keyboard.py           # run the flow
```

If a button is not found, run `--dump`, check the printed names, and update
the `*_NAMES` lists at the top of `copilot_keyboard.py`.

## The two scripts compared

| script | how it finds buttons | needs templates? |
|---|---|---|
| `auto_click_copilot.py` | screen capture + OpenCV matching | yes (crop once) |
| `copilot_keyboard.py` | accessibility tree names + keyboard | no |

## Option C: Playwright + Firefox (recommended — most reliable)

`copilot_playwright.py` drives the **real Firefox** in a visible window and
attaches the image **directly** — no templates, no screen capture, no native
file dialog, no tab-walking. It cannot get stuck on the dialog because there
is no dialog: the file is handed straight to the upload control.

```bash
# one-time install (besides ./install.sh):
pip install playwright
playwright install firefox
playwright install-deps firefox

# run:
python3 copilot_playwright.py                     # screenshot + "what is this"
python3 copilot_playwright.py --image photo.png   # attach a specific file
```

First run: sign in to Copilot in the opened window. The session is saved in
`./pw-profile`, so later runs start already signed in.

> **Microsoft "This browser or app may not be secure" block:** Microsoft
> refuses sign-in from automated browsers. The script avoids that entirely:
> it copies your **real Firefox** session cookies (you are already signed in
> there) into the automated window, so it never visits the sign-in page.
> Requirement: be signed in at copilot.microsoft.com in your normal Firefox.
> Use `--no-cookies` to skip the import (e.g. to run as guest).

## The three scripts compared

| script | how it finds buttons | needs templates? | file dialog? | reliability |
|---|---|---|---|---|
| `auto_click_copilot.py` | screen capture + OpenCV | yes (crop once) | native (typed path) | medium |
| `copilot_keyboard.py` | accessibility tree names + keyboard / Tab | no | native (typed path) | medium-low |
| `copilot_playwright.py` | real UI lookups by name | no | **none** (fed directly) | **high** |

## Quickstart (host PC)

```bash
# 1. clone
git clone https://github.com/phanhuycuong33-alt/auto_click.git
cd auto-click-copilot

# 2. install everything (system packages + Python packages)
./install.sh

# 3. create button templates (one command per button)
python3 capture_template.py plus_button
python3 capture_template.py attach_button
python3 capture_template.py add_image_button
python3 capture_template.py send_button
python3 capture_template.py message_box

# 4. verify button positions WITHOUT clicking
python3 auto_click_copilot.py --dry-run

# 5. real run (it pauses so you can sign in to Copilot if asked)
python3 auto_click_copilot.py
```

> ⚠️ **Session:** use an **"Ubuntu on Xorg"** login session, not Wayland —
> pyautogui cannot control the mouse under Wayland.

> ⚠️ **Safety:** fail-safe is ON — slam the mouse to the top-left corner of the
> screen at any time to abort.

## Options

```bash
python3 auto_click_copilot.py --dry-run            # locate + annotate, no clicks
python3 auto_click_copilot.py --step add_image     # resume from a step
python3 auto_click_copilot.py --image photo.png    # attach a specific file
python3 auto_click_copilot.py --threshold 0.6      # looser matching
python3 auto_click_copilot.py --no-firefox         # Firefox already open
```

## Troubleshooting

- **Button not found** → re-capture the template with `capture_template.py`
  (tighter crop, same theme/zoom), or lower `--threshold`.
- **Clicks land a few px off** → re-capture the template.
- **OCR not working** → `sudo apt install tesseract-ocr` (done by `install.sh`).
- **Mouse won't move** → you're on Wayland; log in as "Ubuntu on Xorg".
