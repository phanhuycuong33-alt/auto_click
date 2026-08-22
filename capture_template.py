#!/usr/bin/env python3
"""
capture_template.py — crop a reference image of a button for template matching.

Usage:
    python3 capture_template.py plus_button

The ORDER matters — move the mouse FIRST, then press Enter:

  Step 1: move the mouse to the TOP-LEFT corner of the button.
          THEN press Enter in the terminal.
  Step 2: move the mouse to the BOTTOM-RIGHT corner of the button.
          THEN press Enter again.

The script measures the distance between the two mouse positions.
If it's tiny, the crop is useless -> it retries with a clear message.

The cropped image is saved to templates/<name>.png
"""

import sys
from pathlib import Path

import pyautogui

BASE = Path(__file__).resolve().parent
TEMPLATES = BASE / "templates"

MIN_SIZE = 3  # px — below this the crop is useless


def capture_once(name):
    print("Step 1: move the mouse to the TOP-LEFT corner of the button.")
    print("         (the button must be visible on screen)")
    input("         press Enter here ONCE the mouse is in place... ")
    x1, y1 = pyautogui.position()

    print("Step 2: move the mouse to the BOTTOM-RIGHT corner of the button.")
    print("         (cover the WHOLE button, a few px of margin is good)")
    input("         press Enter here ONCE the mouse is in place... ")
    x2, y2 = pyautogui.position()

    x, y = min(x1, x2), min(y1, y2)
    w, h = abs(x2 - x1), abs(y2 - y1)
    print(f"captured region: {w} x {h} px")

    if w < MIN_SIZE or h < MIN_SIZE:
        print(f"crop too small ({w}x{h}) — the mouse did not move far enough")
        print("between the two Enter presses. Move the mouse FIRST,")
        print("press Enter AFTER, from one corner to the opposite corner.")
        return False

    if w < 8 or h < 8:
        print("warning: very small crop — matching may be unreliable;")
        print("include a few pixels of margin around the button.")

    TEMPLATES.mkdir(exist_ok=True)
    img = pyautogui.screenshot(region=(x, y, w, h))
    out = TEMPLATES / f"{name}.png"
    img.save(out)
    print(f"saved {out}  ({w}x{h} px)")
    return True


def main():
    if len(sys.argv) > 1:
        name = sys.argv[1]
    else:
        name = input("template name (e.g. plus_button): ").strip()
    if not name:
        sys.exit("no name given")

    for attempt in range(1, 6):
        print(f"--- capture attempt {attempt}/5 ---")
        if capture_once(name):
            return
        print()
    sys.exit("gave up after 5 attempts — check the instructions above")


if __name__ == "__main__":
    main()
