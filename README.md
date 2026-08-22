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

## Quickstart (host PC)

```bash
# 1. clone
git clone https://github.com/<your-user>/auto-click-copilot.git
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
