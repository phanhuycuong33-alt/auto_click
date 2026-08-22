#!/usr/bin/env python3
"""
auto_click_copilot.py — GUI automation ("vision computer" style) that:

  1. Opens Firefox (like clicking the app icon) at https://copilot.microsoft.com/
  2. Captures the screen, finds the "+" button via template matching, clicks it
  3. Clicks the "attach image/document" button (appears after "+")
  4. Clicks "Add image" in the dialog
  5. Attaches a screenshot to the Copilot message
  6. Types "can you explain what image" and clicks Send

HOW BUTTON POSITIONS ARE FOUND (your question):
  - We capture the whole screen with pyautogui.screenshot()
  - We run OpenCV template matching (cv2.matchTemplate) against small
    reference images of each button stored in templates/*.png
  - The best match returns pixel coordinates ON the screen -> move mouse -> click
  - Multi-scale matching tolerates zoom / DPI differences; OCR (pytesseract)
    is a fallback for text buttons.

SETUP (Ubuntu, X11/Xorg session — NOT Wayland):
  sudo apt install python3-pip scrot tesseract-ocr
  pip install -r requirements.txt

USAGE:
  python3 auto_click_copilot.py                 # full flow
  python3 auto_click_copilot.py --dry-run       # locate + annotate, NO clicks
  python3 auto_click_copilot.py --step add_image
  python3 auto_click_copilot.py --image /path/to/pic.png
  python3 auto_click_copilot.py --threshold 0.75

SAFETY: pyautogui FAILSAFE is ON — slam the mouse to the top-left corner
of the screen at any time to abort.
"""

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import pyautogui

# ---------------------------------------------------------------- config
BASE = Path(__file__).resolve().parent
TEMPLATES = BASE / "templates"
SHOTS = BASE / "screenshots"
SHOTS.mkdir(exist_ok=True)

URL = "https://copilot.microsoft.com/"
QUESTION = "can you explain what image"
DEFAULT_THRESHOLD = 0.80
SCALES = (0.8, 0.9, 1.0, 1.1, 1.25)  # tolerate zoom / DPI differences

pyautogui.FAILSAFE = True  # move mouse to top-left corner to abort
pyautogui.PAUSE = 0.25

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("copilot")

DRY = False
THRESHOLD = DEFAULT_THRESHOLD
ATTACH_IMAGE = None  # optional --image override


# ---------------------------------------------------------------- capture / vision
def capture(name=None):
    """Grab the whole screen. Saves a copy if name is given."""
    img = pyautogui.screenshot()
    if name:
        p = SHOTS / f"{name}.png"
        img.save(p)
        log.info("saved screenshot: %s", p)
    return img


def to_cv(img):
    """PIL screenshot -> OpenCV BGR numpy array."""
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def find_template(img, tpl_name, threshold=None, scales=None):
    """
    Find templates/<tpl_name>.png anywhere on the screen.
    Returns dict(x, y, w, h, cx, cy, score) in SCREEN coordinates, or None.
    """
    threshold = threshold if threshold is not None else THRESHOLD
    scales = scales or SCALES
    tpl_path = TEMPLATES / f"{tpl_name}.png"
    if not tpl_path.exists():
        log.warning("template missing: %s", tpl_path)
        log.warning("create it with: python3 capture_template.py %s", tpl_name)
        return None
    tpl = cv2.imread(str(tpl_path))
    if tpl is None:
        log.warning("cannot read template: %s", tpl_path)
        return None

    screen = to_cv(img)
    sh, sw = screen.shape[:2]
    th, tw = tpl.shape[:2]

    best = None  # (score, x, y, w, h)
    for s in scales:
        w, h = int(tw * s), int(th * s)
        if w < 8 or h < 8 or w > sw or h > sh:
            continue
        resized = cv2.resize(tpl, (w, h))
        res = cv2.matchTemplate(screen, resized, cv2.TM_CCOEFF_NORMED)
        _, maxv, _, maxloc = cv2.minMaxLoc(res)
        if best is None or maxv > best[0]:
            best = (maxv, maxloc[0], maxloc[1], w, h)

    if best is None or best[0] < threshold:
        return None
    score, x, y, w, h = best
    return {"x": x, "y": y, "w": w, "h": h,
            "cx": x + w // 2, "cy": y + h // 2, "score": score}


def find_text(img, needle):
    """OCR fallback: locate a text label on screen. Returns (cx, cy, conf) or None."""
    try:
        import pytesseract
    except ImportError:
        return None
    try:
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    except Exception as e:
        log.warning("OCR failed: %s", e)
        return None
    for i, word in enumerate(data["text"]):
        if needle.lower() in word.lower():
            x, y = data["left"][i], data["top"][i]
            w, h = data["width"][i], data["height"][i]
            return (x + w // 2, y + h // 2, data["conf"][i])
    return None


def locate(tpl_name, img=None, ocr_words=()):
    """Template match first, OCR text search as fallback. Returns a hit dict or None."""
    img = img or capture()
    hit = find_template(img, tpl_name)
    if hit:
        log.info("found '%s' via template @ (%d, %d) score=%.2f",
                 tpl_name, hit["cx"], hit["cy"], hit["score"])
        return hit
    for word in ocr_words:
        r = find_text(img, word)
        if r:
            cx, cy, conf = r
            log.info("found '%s' via OCR text '%s' @ (%d, %d) conf=%s",
                     tpl_name, word, cx, cy, conf)
            return {"cx": cx, "cy": cy, "w": 0, "h": 0, "score": conf, "ocr": True}
    return None


def wait_for(tpl_name, timeout=30, ocr_words=(), interval=1.0):
    """Keep capturing until the button appears. Returns hit dict or None."""
    deadline = time.time() + timeout
    last_log = 0.0
    while time.time() < deadline:
        hit = locate(tpl_name, img=capture(), ocr_words=ocr_words)
        if hit:
            return hit
        now = time.time()
        if now - last_log >= 10:
            log.info("still looking for '%s' (%ds left)...", tpl_name, int(deadline - now))
            last_log = now
        time.sleep(interval)
    log.warning("timeout waiting for '%s' (%ss)", tpl_name, timeout)
    return None


# ---------------------------------------------------------------- actions
def click_at(x, y, label=""):
    log.info("click %s @ (%d, %d)", label or "target", x, y)
    pyautogui.moveTo(x, y, duration=0.4)
    time.sleep(0.2)
    pyautogui.click()
    time.sleep(0.5)


def annotate(img, hit, out_name):
    """Draw a box/circle on the match and save it (dry-run verification)."""
    img2 = to_cv(img)
    if hit.get("w") and hit.get("h"):
        cv2.rectangle(img2, (hit["x"], hit["y"]),
                      (hit["x"] + hit["w"], hit["y"] + hit["h"]), (0, 255, 0), 3)
        cv2.putText(img2, f"{hit['score']:.2f}", (hit["x"], hit["y"] - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    else:  # OCR hit — just a point
        cv2.circle(img2, (hit["cx"], hit["cy"]), 12, (0, 0, 255), -1)
    p = SHOTS / out_name
    cv2.imwrite(str(p), img2)
    log.info("dry-run annotation saved: %s", p)


# ---------------------------------------------------------------- steps
def step_open():
    log.info("opening Firefox -> %s", URL)
    # silence snap-Firefox GTK warnings (harmless but noisy)
    subprocess.Popen(["firefox", URL],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log.info("waiting 12s for Firefox to start...")
    time.sleep(12)
    print("\n>>> If Microsoft Copilot asks you to SIGN IN, sign in now in Firefox.")
    input(">>> When the chat page is ready (or you finished signing in), press Enter to continue. ")
    log.info("user ready, continuing")


def step_plus():
    hit = wait_for("plus_button", timeout=60)
    if not hit:
        log.warning("'+' not found; continuing anyway (will try attach button directly)")
        return
    if DRY:
        annotate(capture(), hit, "dryrun_plus_button.png")
        return
    click_at(hit["cx"], hit["cy"], "'+' button")


def step_attach():
    hit = wait_for("attach_button", timeout=30,
                   ocr_words=("Upload from this device", "Upload file", "Attach"))
    if not hit:
        log.warning("attach button not found; continuing")
        return
    if DRY:
        annotate(capture(), hit, "dryrun_attach_button.png")
        return
    click_at(hit["cx"], hit["cy"], "attach image/document")


def step_add_image():
    global ATTACH_IMAGE
    # Capture the image we will attach BEFORE the file dialog covers the screen.
    if not ATTACH_IMAGE:
        ATTACH_IMAGE = str(SHOTS / "to_attach.png")
        capture("to_attach")
        log.info("captured screen to attach: %s", ATTACH_IMAGE)

    hit = wait_for("add_image_button", timeout=30, ocr_words=("Add image", "Add"))
    if not hit:
        log.warning("'Add image' not found; pressing Enter to accept dialog default")
        if not DRY:
            pyautogui.press("enter")
            time.sleep(2)
        return
    if DRY:
        annotate(capture(), hit, "dryrun_add_image.png")
        return
    click_at(hit["cx"], hit["cy"], "Add image")


def step_file_dialog():
    """Native file chooser: type the full path via Ctrl+L, confirm with Open/Enter."""
    log.info("waiting for the file chooser dialog...")
    time.sleep(2.5)
    path = ATTACH_IMAGE or str(SHOTS / "to_attach.png")
    log.info("typing file path into dialog: %s", path)
    if not DRY:
        pyautogui.hotkey("ctrl", "l")          # GTK file chooser location bar
        time.sleep(0.5)
        pyautogui.write(path, interval=0.02)
        time.sleep(0.3)
        pyautogui.press("enter")               # navigate / select the file
        time.sleep(2.0)

    # If the user provided an open_button template, click it; else press Enter.
    hit = find_template(capture(), "open_button")
    if hit:
        if DRY:
            annotate(capture(), hit, "dryrun_open_button.png")
            return
        click_at(hit["cx"], hit["cy"], "Open button")
        return
    log.info("no open_button template; pressing Enter to confirm")
    if not DRY:
        pyautogui.press("enter")
        time.sleep(1.5)


def step_compose():
    hit = wait_for("message_box", timeout=20)
    if hit:
        if DRY:
            annotate(capture(), hit, "dryrun_message_box.png")
            return
        click_at(hit["cx"], hit["cy"], "message box")
    else:
        log.warning("message box template not found; clicking lower-center as fallback")
        w, h = pyautogui.size()
        click_at(w // 2, h - 140, "message box (fallback)")
    time.sleep(0.5)
    if not DRY:
        pyautogui.write(QUESTION, interval=0.05)
        log.info("typed question: %s", QUESTION)


def step_send():
    hit = wait_for("send_button", timeout=15)
    if hit:
        if DRY:
            annotate(capture(), hit, "dryrun_send_button.png")
            return
        click_at(hit["cx"], hit["cy"], "Send")
        return
    log.warning("send button not found; pressing Enter to send")
    if not DRY:
        pyautogui.press("enter")


# ---------------------------------------------------------------- runner
STEPS = ["open", "plus", "attach", "add_image", "file_dialog", "compose", "send"]
STEP_FN = {
    "open": step_open,
    "plus": step_plus,
    "attach": step_attach,
    "add_image": step_add_image,
    "file_dialog": step_file_dialog,
    "compose": step_compose,
    "send": step_send,
}


def run(start_step):
    if start_step not in STEP_FN:
        sys.exit(f"unknown step {start_step!r}; choose from {STEPS}")
    for name in STEPS[STEPS.index(start_step):]:
        log.info("=== step: %s ===", name)
        try:
            STEP_FN[name]()
        except pyautogui.FailSafeException:
            log.error("ABORTED by user (mouse moved to top-left corner)")
            sys.exit(1)
    log.info("done — check the Firefox window!")


def main():
    global DRY, THRESHOLD, ATTACH_IMAGE
    ap = argparse.ArgumentParser(
        description="Attach a screenshot to Microsoft Copilot and ask about it.")
    ap.add_argument("--dry-run", action="store_true",
                    help="locate every button, save annotated screenshots, do NOT click")
    ap.add_argument("--step", default="open", choices=STEPS, help="start from this step")
    ap.add_argument("--image", default=None, help="image file to attach (default: fresh screenshot)")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help="template match threshold 0..1 (lower = more permissive)")
    ap.add_argument("--no-firefox", action="store_true",
                    help="skip launching Firefox (assume it is already open)")
    args = ap.parse_args()

    DRY, THRESHOLD = args.dry_run, args.threshold
    if args.image:
        ATTACH_IMAGE = str(Path(args.image).resolve())
    if args.no_firefox and args.step == "open":
        args.step = "plus"
    run(args.step)


if __name__ == "__main__":
    main()
