#!/usr/bin/env python3
"""
capture_template.py — crop a reference image of a button for template matching.

Usage:
    python3 capture_template.py plus_button

1. Move the mouse to the TOP-LEFT corner of the button, press Enter in the terminal.
2. Move the mouse to the BOTTOM-RIGHT corner of the button, press Enter again.
3. The cropped image is saved to templates/<name>.png

Tips:
  - Capture at 100% zoom, with the same theme you will run the flow in.
  - Include a few pixels of margin around the button, but nothing else
    (no overlapping windows, no cursor, no other UI in the crop).
  - Smaller crop = faster + more precise matching.
"""

import sys
from pathlib import Path

import pyautogui

BASE = Path(__file__).resolve().parent
TEMPLATES = BASE / "templates"


def main():
    if len(sys.argv) > 1:
        name = sys.argv[1]
    else:
        name = input("template name (e.g. plus_button): ").strip()
    if not name:
        sys.exit("no name given")

    print("Move mouse to TOP-LEFT corner of the button, then press Enter here...")
    input()
    x1, y1 = pyautogui.position()

    print("Now move mouse to BOTTOM-RIGHT corner of the button, then press Enter here...")
    input()
    x2, y2 = pyautogui.position()

    x, y = min(x1, x2), min(y1, y2)
    w, h = abs(x2 - x1), abs(y2 - y1)
    if w < 5 or h < 5:
        sys.exit("crop too small")

    TEMPLATES.mkdir(exist_ok=True)
    img = pyautogui.screenshot(region=(x, y, w, h))
    out = TEMPLATES / f"{name}.png"
    img.save(out)
    print(f"saved {out}  ({w}x{h} px)")


if __name__ == "__main__":
    main()
