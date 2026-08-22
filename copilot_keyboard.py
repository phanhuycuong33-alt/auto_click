#!/usr/bin/env python3
"""
copilot_keyboard.py — template-free alternative to auto_click_copilot.py.

NO image matching, NO template crops. Buttons are located two ways:

  A) AT-SPI accessibility tree (dogtail/pyatspi): Linux exposes Firefox's
     UI as a tree of named nodes. We find the '+' / attach / send buttons
     by their ACCESSIBLE NAME and activate them with the KEYBOARD
     (focus + Enter). Robust, no crops.

  B) Tab-walk fallback: if the AT tree is unavailable (snap Firefox quirk),
     we press Tab N times and Enter. Blind but simple — tune the TAB_*
     constants below.

Flow: open Firefox -> '+' button -> attach menu -> "Upload from this
device" -> native file dialog (Ctrl+L + typed path) -> type question ->
Send.

USAGE:
  python3 copilot_keyboard.py              # full flow
  python3 copilot_keyboard.py --dump       # print all button names (helps tune NAME lists)
  python3 copilot_keyboard.py --step plus  # resume from a step
  python3 copilot_keyboard.py --image photo.png

DEPENDENCIES (added by install.sh):
  sudo apt install python3-dogtail at-spi2-core
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pyautogui

# ---------------------------------------------------------------- config
BASE = Path(__file__).resolve().parent
SHOTS = BASE / "screenshots"
SHOTS.mkdir(exist_ok=True)

URL = "https://copilot.microsoft.com/"
QUESTION = "can you explain what image"
ATTACH_IMAGE = None  # optional --image override

pyautogui.FAILSAFE = True  # slam mouse to top-left corner to abort
pyautogui.PAUSE = 0.2

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("copilot-kb")

# --- accessible names to look for (edit these if your UI differs) -------
# To discover the real names: run `python3 copilot_keyboard.py --dump`
PLUS_NAMES   = ["Add context", "add context", "Attach", "attach", "+"]
ATTACH_NAMES = ["Upload from this device", "Upload from device", "Upload file",
                "Attach image/document", "Attach", "Upload"]
ADD_IMG_NAMES = ["Add image", "Add", "Choose file", "Select file", "Open"]
SEND_NAMES   = ["Send message", "Send prompt", "Send"]
COMPOSE_ROLES = ["multi line text", "text", "entry"]

# --- blind tab-walk counts (fallback strategy) --------------------------
TAB_PLUS     = 8    # tabs from page focus to the '+' button
TAB_ATTACH   = 6    # tabs from '+' menu to the upload entry
TAB_ADD_IMG  = 4    # tabs inside the dialog to 'Add image'
TAB_COMPOSE  = 6    # tabs back to the message input
TAB_STEP     = 0.25  # seconds between Tab presses

DRY = False  # locate + report only, no activation


# ---------------------------------------------------------------- AT-SPI helpers
def get_firefox_app():
    """Return the dogtail Node of the Firefox application, or None."""
    try:
        from dogtail.tree import root
    except ImportError:
        log.warning("dogtail not installed — AT mode unavailable (pip install dogtail)")
        return None
    try:
        apps = [a for a in root.children if "firefox" in (a.name or "").lower()]
        if not apps:
            log.warning("no Firefox window found in the accessibility tree")
            log.warning("tip: AT mode needs Firefox started BY this script (a11y forced on);")
            log.warning("otherwise the flow falls back to tab-walk (window focus is handled automatically)")
            return None
        return apps[-1]  # most recently opened
    except Exception as e:
        log.warning("AT-SPI query failed: %s", e)
        return None


def find_node(names, roles=None, app=None):
    """Find a node by accessible name (+ optional role). Returns node or None."""
    app = app or get_firefox_app()
    if app is None:
        return None
    from dogtail.tree import SearchError
    for role in roles or [None]:
        for name in names:
            try:
                node = app.child(name=name, roleName=role, recursive=True)
                log.info("AT found '%s' (role=%s)", name, role or "any")
                return node
            except SearchError:
                continue
            except Exception as e:
                log.warning("AT search error for '%s': %s", name, e)
    return None


def activate_node(node):
    """Keyboard activation: focus the node, press Enter."""
    try:
        node.grabFocus()
        time.sleep(0.4)
        pyautogui.press("enter")
        log.info("activated '%s' via focus+Enter", node.name)
        time.sleep(1.2)
        return True
    except Exception as e:
        log.warning("focus+Enter failed for '%s': %s — trying AT click action", node.name, e)
        try:
            node.doAction("click")
            log.info("activated '%s' via AT click action", node.name)
            time.sleep(1.2)
            return True
        except Exception as e2:
            log.warning("AT click also failed: %s", e2)
            return False


def dump_buttons(app=None):
    """Print every named button in the Firefox tree (discovery tool)."""
    app = app or get_firefox_app()
    if app is None:
        log.warning("nothing to dump — is Firefox open with accessibility on?")
        return
    try:
        from dogtail import predicate
        buttons = app.findChildren(predicate.GenericPredicate(roleName="push button"))
        names = sorted({b.name for b in buttons if b.name})
        log.info("-- visible push buttons (%d) --", len(names))
        for n in names:
            log.info("   %s", n)
        texts = app.findChildren(predicate.GenericPredicate(roleName="multi line text"))
        log.info("-- text areas (%d) --", len(texts))
        for t in texts:
            log.info("   role=%s name=%r", t.roleName, t.name)
    except Exception as e:
        log.warning("dump failed: %s", e)


# ---------------------------------------------------------------- keyboard helpers
def firefox_running():
    try:
        return subprocess.run(["pgrep", "-x", "firefox"],
                              capture_output=True).returncode == 0
    except Exception:
        return True  # cannot tell — do not block


def focus_firefox():
    """Bring Firefox to the front so keyboard input lands on the page,
    NOT on the terminal where the script is running."""
    if shutil.which("xdotool"):
        try:
            subprocess.run(["xdotool", "search", "--onlyvisible", "--class", "firefox",
                            "windowactivate", "--sync"],
                           capture_output=True, timeout=10)
            time.sleep(0.8)
            log.info("focused Firefox window (xdotool)")
            return True
        except Exception as e:
            log.warning("xdotool windowactivate failed: %s", e)
    log.warning("xdotool not available — keyboard input may go to the wrong window!")
    return False


def tab_walk(presses, label):
    focus_firefox()  # keys must land on the web page, not the terminal
    log.info("tab-walk: Tab x%d then Enter for '%s'", presses, label)
    for _ in range(presses):
        pyautogui.press("tab")
        time.sleep(TAB_STEP)
    pyautogui.press("enter")
    time.sleep(1.2)


def find_compose_input():
    """Pick the largest multi-line text area in Firefox (= the message box)."""
    app = get_firefox_app()
    if app is None:
        return None
    from dogtail import predicate
    best, best_area = None, 0
    for role in COMPOSE_ROLES:
        try:
            for n in app.findChildren(predicate.GenericPredicate(roleName=role)):
                try:
                    w, h = n.size
                    area = w * h
                except Exception:
                    area = 0
                if area > best_area:
                    best, best_area = n, area
        except Exception:
            continue
    if best:
        log.info("AT found message input (role=%s)", best.roleName)
    return best


def type_text(text):
    pyautogui.write(text, interval=0.05)
    log.info("typed: %s", text)


def press_enter():
    pyautogui.press("enter")
    time.sleep(0.8)


# ---------------------------------------------------------------- steps
def step_open():
    env = dict(os.environ, MOZ_ACCESSIBILITY_ENABLE="1")  # MERGE env + force a11y on
    log.info("opening Firefox -> %s (accessibility forced ON)", URL)
    try:
        subprocess.Popen(["firefox", URL],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    except Exception as e:
        log.warning("auto-launch failed (%s) — please open Firefox manually", e)
    log.info("waiting 12s for Firefox to start...")
    time.sleep(12)
    if not firefox_running():
        log.warning("Firefox does not appear to be running — please open it manually now")
    print("\n>>> If Microsoft Copilot asks you to SIGN IN, sign in now in Firefox.")
    input(">>> When the chat page is ready (or you finished signing in), press Enter to continue. ")
    log.info("user ready, continuing")


def step_plus():
    if DRY:
        log.info("DRY-RUN: would look for %s", PLUS_NAMES)
        return
    node = find_node(PLUS_NAMES, roles=["push button", "toggle button"])
    if node:
        activate_node(node)
    else:
        log.warning("AT did not find '+' — tab-walking (TAB_PLUS=%d)", TAB_PLUS)
        tab_walk(TAB_PLUS, "'+' button")


def step_attach():
    node = find_node(ATTACH_NAMES, roles=["push button", "menu item"])
    if node:
        activate_node(node)
    else:
        log.warning("AT did not find attach — tab-walking (TAB_ATTACH=%d)", TAB_ATTACH)
        tab_walk(TAB_ATTACH, "attach image/document")


def step_add_image():
    node = find_node(ADD_IMG_NAMES, roles=["push button"])
    if node:
        activate_node(node)
    else:
        log.warning("AT did not find 'Add image' — tab-walking (TAB_ADD_IMG=%d)", TAB_ADD_IMG)
        tab_walk(TAB_ADD_IMG, "Add image")


def step_file_dialog():
    """Native file chooser: Ctrl+L -> type full path -> Enter."""
    global ATTACH_IMAGE
    if not ATTACH_IMAGE:
        ATTACH_IMAGE = str(SHOTS / "to_attach.png")
        pyautogui.screenshot().save(ATTACH_IMAGE)
        log.info("captured screen to attach: %s", ATTACH_IMAGE)
    log.info("waiting for the file chooser dialog...")
    time.sleep(2.5)
    log.info("typing file path into dialog: %s", ATTACH_IMAGE)
    pyautogui.hotkey("ctrl", "l")
    time.sleep(0.5)
    pyautogui.write(ATTACH_IMAGE, interval=0.02)
    time.sleep(0.3)
    pyautogui.press("enter")
    time.sleep(2.0)
    pyautogui.press("enter")  # confirm (harmless if already confirmed)
    time.sleep(1.5)


def step_compose():
    node = find_compose_input()
    if node:
        activate_node(node)
    else:
        log.warning("AT did not find the message input — tab-walking (TAB_COMPOSE=%d)", TAB_COMPOSE)
        tab_walk(TAB_COMPOSE, "message input")
    time.sleep(0.5)
    type_text(QUESTION)


def step_send():
    node = find_node(SEND_NAMES, roles=["push button"])
    if node:
        activate_node(node)
    else:
        log.warning("AT did not find Send — focusing Firefox and pressing Enter")
        focus_firefox()
        press_enter()


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
            pyautogui.screenshot().save(SHOTS / f"step_{name}.png")  # debug trail
            STEP_FN[name]()
        except pyautogui.FailSafeException:
            log.error("ABORTED by user (mouse moved to top-left corner)")
            sys.exit(1)
    log.info("done — check the Firefox window!")


def main():
    global DRY, ATTACH_IMAGE
    ap = argparse.ArgumentParser(
        description="Keyboard/accessibility-driven Copilot automation (no templates).")
    ap.add_argument("--dry-run", action="store_true", help="report what would be done, do nothing")
    ap.add_argument("--dump", action="store_true", help="print button names from the AT tree, then exit")
    ap.add_argument("--step", default="open", choices=STEPS, help="start from this step")
    ap.add_argument("--image", default=None, help="image file to attach")
    ap.add_argument("--no-firefox", action="store_true", help="Firefox already open")
    args = ap.parse_args()

    DRY = args.dry_run
    if args.image:
        ATTACH_IMAGE = str(Path(args.image).resolve())
    if args.dump:
        dump_buttons()
        return
    if args.no_firefox and args.step == "open":
        args.step = "plus"
    run(args.step)


if __name__ == "__main__":
    main()
