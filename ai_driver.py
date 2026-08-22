#!/usr/bin/env python3
"""
ai_driver.py — multi-AI vision driver: Copilot -> ChatGPT -> DeepSeek fallback.

Two tabs:
  - TAB A (task): the web page you are automating (opens --url). Screenshots
    are taken here and commands (click/fill/...) are executed here.
  - TAB B (ai): the chat tab. Each round the current screenshot is attached
    and ONE provider is asked; if its reply is empty/unparseable, the next
    provider is tried automatically.

Provider order (configurable): copilot -> chatgpt -> deepseek
  - deepseek needs its "vision" tab switched on before attaching an image
    (handled automatically).

USAGE:
  .venv/bin/python3 ai_driver.py --url "https://survey-site.com" \
      "i want to make money with this web by doing a survey, what do i do next?"
  .venv/bin/python3 ai_driver.py --url "..." --image first_shot.png "task"
  .venv/bin/python3 ai_driver.py --url "..." --providers chatgpt,deepseek "task"
  .venv/bin/python3 ai_driver.py --url "..." --steps 10 "task"

Cookies for all three providers are imported from your real Firefox (be
signed in there: copilot.microsoft.com, chatgpt.com, chat.deepseek.com).
"""

import argparse
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent
SHOTS = BASE / "screenshots"
SHOTS.mkdir(exist_ok=True)
PROFILE = BASE / "pw-profile"
REAL_PROFILE_COPY = BASE / "pw-real-profile"
PROFILE_MAX_AGE = 600  # seconds — reuse the profile copy if fresh enough

DEFAULT_STEPS = 5
DEFAULT_PROVIDERS = ["copilot", "chatgpt", "deepseek"]

INSTRUCTION = """You are controlling my browser via Playwright. I just attached a screenshot of the current page.

Reply with ONLY ONE line, one of these formats:
click 'button text'
fill 'field' with 'value'
type 'text'
wait '3'
scroll 'down' or 'up'
goto 'https://url'
done

Choose the single next best action for: {task}
When the task is fully done, reply exactly: done"""

ATTACH_OPENERS = ["Add context", "Attach", "Attach files", "Attach media"]

REPLY_SELECTORS = {
    "copilot": ('[data-content="assistant-message"]',),
    "chatgpt": ('[data-message-author-role="assistant"]',),
    "deepseek": ('.ds-markdown',),
}
SIGNIN_MARKERS = ["Log in", "Sign in", "登录"]


# ---------------------------------------------------------------- generic helpers
def visible(loc, timeout=2000):
    try:
        return loc.is_visible(timeout=timeout)
    except Exception:
        return False


def find_textbox(page):
    selectors = ('[role="textbox"]', "textarea", '[contenteditable="true"]',
                 '[aria-label*="Ask"]', '[aria-label*="prompt"]', '[aria-label*="message"]')
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count():
                return loc.last
        except Exception:
            continue
    return None


def quoted(s):
    m = re.search(r"""['"]([^'"]*)['"]""", s)
    return m.group(1) if m else s


def parse_command(reply, exclude=""):
    for line in reversed(reply.strip().splitlines()):
        line = line.strip().lstrip("*-`# ")
        if not line:
            continue
        if exclude and line in exclude:
            continue
        if re.match(r"^(click|fill|type|wait|scroll|goto|done)\b", line, re.I):
            return line
    return None


def click_text(page, text):
    """Click an element by text with a cascade of fallbacks.
    Strips trailing prices ('Start survey $0.25' -> 'Start survey' -> ...)
    and tries button, link, then raw text roles. Returns True on success."""
    candidates = [text]
    t = text
    while True:
        t2 = re.sub(r"\s+\$?[\d.,]+$", "", t).strip()  # strip trailing price/number
        if not t2 or t2 == t:
            break
        candidates.append(t2)
        t = t2
    for cand in candidates:
        for role in ("button", "link"):
            try:
                loc = page.get_by_role(role, name=cand, exact=False).first
                loc.click(timeout=3000)
                print(f"[exec] click '{cand}' (role={role})")
                return True
            except Exception:
                continue
        try:
            page.get_by_text(cand, exact=False).first.click(timeout=3000)
            print(f"[exec] click '{cand}' (by text)")
            return True
        except Exception:
            continue
    print(f"[!] could not click '{text}' (tried: {candidates})")
    return False


def find_input(page, hint):
    sels = [f'input[placeholder*="{hint}"]', f'textarea[placeholder*="{hint}"]',
            f'[aria-label*="{hint}"]', "input", "textarea"]
    for sel in sels:
        try:
            loc = page.locator(sel).first
            if loc.count():
                return loc
        except Exception:
            continue
    return None


def execute_command(page, line):
    m = re.match(r"^(\w+)\b(.*)$", line.strip(), re.S)
    cmd, rest = m.group(1).lower(), m.group(2).strip()

    if cmd == "click":
        click_text(page, quoted(rest))
    elif cmd == "fill":
        fm = re.match(r"""['"]([^'"]*)['"]\s+with\s+['"]([^'"]*)['"]""", rest, re.I)
        if not fm:
            print(f"[!] bad fill command: {line}")
            return True
        field, value = fm.group(1), fm.group(2)
        loc = find_input(page, field)
        if loc is None:
            print(f"[!] no input found for '{field}'")
            return True
        try:
            loc.fill(value)
        except Exception:
            loc.click()
            page.keyboard.type(value, delay=15)
        print(f"[exec] fill '{field}' with '{value}'")
    elif cmd == "type":
        page.keyboard.type(quoted(rest), delay=20)
        print(f"[exec] type '{quoted(rest)}'")
    elif cmd == "wait":
        m2 = re.search(r"(\d+(?:\.\d+)?)", rest)
        secs = float(m2.group(1)) if m2 else 2.0
        print(f"[exec] wait {secs}s")
        page.wait_for_timeout(int(secs * 1000))
    elif cmd == "scroll":
        page.mouse.wheel(0, 800 if "up" not in rest else -800)
        print(f"[exec] scroll {rest}")
    elif cmd == "goto":
        try:
            page.goto(quoted(rest))
            page.wait_for_load_state("domcontentloaded")
            print(f"[exec] goto {quoted(rest)}")
        except Exception as e:
            print(f"[!] goto failed: {e}")
    elif cmd == "done":
        print("[exec] done — task complete")
        return False
    else:
        print(f"[!] unknown command: {line}")
    return True


def load_firefox_cookies(extra_domains=()):
    """AI-provider cookies + extra domains (e.g. your task site) from real Firefox."""
    patterns = ["%microsoft%", "%live.com", "%openai%", "%deepseek%"]
    for d in extra_domains:
        d = d.strip().lower()
        if d:
            patterns.append(f"%{d}")
    where = " OR ".join(f"host LIKE '{p}'" for p in patterns)
    candidates = []
    for base in (Path.home() / ".mozilla" / "firefox",
                 Path.home() / "snap" / "firefox" / "common" / ".mozilla" / "firefox"):
        if base.is_dir():
            candidates += sorted(base.glob("*/cookies.sqlite"))
    if not candidates:
        return None
    db = max(candidates, key=lambda p: p.stat().st_mtime)
    tmp = Path(tempfile.mkdtemp()) / "cookies.sqlite"
    shutil.copy2(db, tmp)
    for suffix in ("-wal", "-shm"):
        src = Path(str(db) + suffix)
        if src.exists():
            shutil.copy2(src, Path(str(tmp) + suffix))
    con = sqlite3.connect(f"file:{tmp}?immutable=1", uri=True)
    rows = con.execute(
        "SELECT name, value, host, path, expiry, isSecure, isHttpOnly, sameSite "
        f"FROM moz_cookies WHERE {where}"
    ).fetchall()
    con.close()
    ss_map = {0: "None", 1: "Lax", 2: "Strict"}
    cookies = []
    for name, value, host, path, expiry, isSecure, isHttpOnly, same_site in rows:
        if not value:
            continue
        ss = ss_map.get(same_site, "Lax")
        if ss == "None" and not isSecure:  # SameSite=None requires Secure
            isSecure = 1
        cookies.append({"name": name, "value": value,
                        "domain": host if host.startswith(".") else "." + host,
                        "path": path or "/",
                        "expires": int(expiry) if expiry and expiry > 0 else -1,
                        "secure": bool(isSecure), "httpOnly": bool(isHttpOnly),
                        "sameSite": ss})
    return cookies or None


# ---------------------------------------------------------------- provider actions
def extract_reply(page, provider, timeout=180):
    """Wait for the newest provider message to finish streaming, return its text.
    Tracks the 'Stop generating' button to know when generation finished."""
    selectors = REPLY_SELECTORS.get(provider, ())
    last_text, stable_since, started = "", time.time(), False
    stop_seen = False
    deadline = time.time() + timeout

    def stop_visible():
        try:
            loc = page.locator('button:has-text("Stop")').first
            return bool(loc.count()) and loc.is_visible()
        except Exception:
            return False

    def current_text():
        t = ""
        for sel in selectors:
            try:
                loc = page.locator(sel)
                n = loc.count()
                if n:
                    t = loc.nth(n - 1).inner_text()
                    break
            except Exception:
                continue
        if not t:  # generic catch-all
            for sel in ('[data-content]', '[data-message-author-role="assistant"]', '.ds-markdown'):
                try:
                    loc = page.locator(sel)
                    if loc.count():
                        t = loc.nth(loc.count() - 1).inner_text()
                        break
                except Exception:
                    continue
        if t and t.strip() in ("Copilot said", "ChatGPT said", "Copilot said\n"):
            return ""  # label-only stub, content still streaming
        return t

    while time.time() < deadline:
        text = current_text()
        if stop_visible():
            stop_seen = True
        if text != last_text:
            if text:
                started = True
            last_text, stable_since = text, time.time()
        elif stop_seen and not stop_visible():
            page.wait_for_timeout(2500)  # generation finished — settle
            final = current_text()
            if final:
                return final
            return last_text
        elif started and text and time.time() - stable_since > 6:
            return text
        page.wait_for_timeout(1000)
    return last_text


def provider_textbox(page, provider):
    if provider == "chatgpt":
        try:
            loc = page.locator("#prompt-textarea")
            if loc.count():
                return loc.first
        except Exception:
            pass
    return find_textbox(page)


def wait_for_ready(page, provider, timeout=20):
    """Wait until the provider page is usable. Returns True if logged in.
    A visible textbox = logged in; a visible login button = sign-in required."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        tb = provider_textbox(page, provider)
        if tb is not None and tb.count():
            try:
                if tb.is_visible():
                    return True
            except Exception:
                return True
        for name in SIGNIN_MARKERS:
            if visible(page.get_by_role("button", name=name, exact=False).first, 1000):
                print(f"[!] {provider}: sign-in page detected — sign in on your normal Firefox and re-run")
                return False
        page.wait_for_timeout(1500)
    print(f"[!] {provider}: page not ready after {timeout}s")
    return False


def send_message(page, provider, text):
    textbox = None
    if provider == "chatgpt":
        try:
            loc = page.locator("#prompt-textarea")
            if loc.count():
                textbox = loc.first
        except Exception:
            pass
    if textbox is None:
        textbox = find_textbox(page)
    if textbox is None:
        print(f"[!] {provider}: message input not found")
        return False
    textbox.click()
    try:
        textbox.fill(text)
    except Exception:
        textbox.click()
        page.keyboard.type(text, delay=15)

    sent = False
    if provider == "chatgpt":
        for sel in ('[data-testid="send-button"]', 'button[aria-label="Send prompt"]'):
            try:
                loc = page.locator(sel).first
                if visible(loc, 1500):
                    loc.click()
                    sent = True
                    break
            except Exception:
                continue
    if not sent:
        for name in ["Send message", "Send", "Send prompt"]:
            btn = page.get_by_role("button", name=name, exact=False).first
            if visible(btn, 1500):
                btn.click()
                sent = True
                break
    if not sent:
        page.keyboard.press("Enter")
    return True


def attach_image(page, provider, image):
    page.keyboard.press("Escape")
    page.wait_for_timeout(600)
    if provider == "copilot":
        opener = None
        for name in ATTACH_OPENERS:
            loc = page.get_by_role("button", name=name, exact=False).first
            if visible(loc):
                opener = loc
                break
        if opener is None:
            for sel in ('button[aria-label*="Add context"]', 'button[aria-label*="Attach"]'):
                loc = page.locator(sel).first
                if visible(loc, 1500):
                    opener = loc
                    break
        if opener is None:
            print("[!] copilot: attach button not found")
            return False
        opener.click()
        page.wait_for_timeout(800)
    elif provider == "chatgpt":
        clicked = False
        for sel in ('button[aria-label="Attach files"]', 'button[aria-label*="Attach"]',
                    'input[type="file"]'):
            loc = page.locator(sel).first
            try:
                if loc.count() and sel.startswith("input"):
                    pass  # handled by set_input_files below
                elif visible(loc, 1500):
                    loc.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            print("[!] chatgpt: attach button not found (paperclip)")
            return False
        page.wait_for_timeout(800)
    elif provider == "deepseek":
        # deepseek: switch the vision tab on BEFORE attaching
        for name in ["Vision", "vision", "图片", "视觉"]:
            try:
                loc = page.get_by_text(name, exact=False).first
                if visible(loc, 1200):
                    loc.click()
                    print(f"[i] deepseek: switched vision tab ('{name}')")
                    page.wait_for_timeout(1200)
                    break
            except Exception:
                continue
        clicked = False
        for sel in ('button[aria-label*="Upload"]', 'button[aria-label*="attach"]',
                    'button[aria-label*="Attach"]', 'input[type="file"]'):
            loc = page.locator(sel).first
            try:
                if visible(loc, 1500):
                    loc.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            print("[!] deepseek: upload/attach button not found")
            return False
        page.wait_for_timeout(800)

    try:
        page.set_input_files("input[type=file]", str(image), timeout=10000)
        page.wait_for_timeout(1500)
        return True
    except Exception as e:
        print(f"[!] {provider}: set_input_files failed: {e}")
        return False


def ask_provider(page, provider, image, task, prompt):
    """One round on one provider. Returns reply text or None."""
    urls = {"copilot": "https://copilot.microsoft.com/",
            "chatgpt": "https://chatgpt.com/",
            "deepseek": "https://chat.deepseek.com/"}
    print(f"\n[ai] asking {provider} ...")
    try:
        page.goto(urls[provider])
        page.wait_for_load_state("domcontentloaded")
    except Exception as e:
        print(f"[!] {provider}: navigation failed: {e}")
        return None
    page.wait_for_timeout(1500)
    if not wait_for_ready(page, provider):
        return None
    if not attach_image(page, provider, image):
        return None
    if not send_message(page, provider, prompt):
        return None
    print(f"[i] waiting for {provider} reply...")
    return extract_reply(page, provider)


def load_firefox_localstorage(host):
    """localStorage/sessionStorage for the host from Firefox's webappsstore2."""
    try:
        candidates = []
        for base in (Path.home() / ".mozilla" / "firefox",
                     Path.home() / "snap" / "firefox" / "common" / ".mozilla" / "firefox"):
            if base.is_dir():
                candidates += sorted(base.glob("*/cookies.sqlite"))
        if not candidates:
            print("[i] no Firefox profile found for localStorage")
            return []
        prof = max(candidates, key=lambda p: p.stat().st_mtime).parent
        db = prof / "webappsstore.sqlite"
        if not db.exists():
            print("[i] no webappsstore.sqlite in profile")
            return []
        tmp = Path(tempfile.mkdtemp()) / "webappsstore.sqlite"
        shutil.copy2(db, tmp)
        for suffix in ("-wal", "-shm"):
            src = Path(str(db) + suffix)
            if src.exists():
                shutil.copy2(src, Path(str(tmp) + suffix))
        con = sqlite3.connect(f"file:{tmp}?immutable=1", uri=True)
        rows = con.execute(
            "SELECT key, value FROM webappsstore2 WHERE scope LIKE ?",
            (f"%{host}%",)).fetchall()
        if not rows:  # fallback: registrable domain
            parts = host.split(".")
            if len(parts) > 2:
                rows = con.execute(
                    "SELECT key, value FROM webappsstore2 WHERE scope LIKE ?",
                    (f"%{'.'.join(parts[-2:])}%",)).fetchall()
        con.close()
        kv = [(k, v) for k, v in rows if k and v is not None]
        if not kv:
            print(f"[i] webappsstore2 has no entries for {host} "
                  "(site may keep session only in cookies)")
        return kv
    except Exception as e:
        print(f"[!] localStorage read failed: {e}")
        return []


def inject_localstorage(page, host):
    """Inject real Firefox localStorage + sessionStorage before the page loads."""
    import json
    kv = load_firefox_localstorage(host)
    if not kv:
        return
    js = "\n".join(
        f"localStorage.setItem({json.dumps(k)}, {json.dumps(v)});\n"
        f"sessionStorage.setItem({json.dumps(k)}, {json.dumps(v)});"
        for k, v in kv)
    page.add_init_script(js)
    print(f"[i] injected {len(kv)} storage keys for {host} (local + session)")


def auto_login(page, username, password):
    """Best-effort login: fill the first username/password form and submit."""
    pwd = page.locator('input[type="password"]').first
    try:
        if not pwd.count():
            print("[i] no login form detected — assuming already logged in")
            return
    except Exception:
        return
    print("[i] login form detected — filling credentials")
    user = page.locator('input[type="text"], input[type="email"], '
                        'input[name*="user" i], input[name*="email" i], '
                        'input[id*="user" i], input[id*="email" i]').first
    try:
        if user.count():
            user.fill(username)
        pwd.fill(password)
    except Exception as e:
        print(f"[!] auto-login fill failed: {e}")
        return
    try:
        btn = page.locator('button:has-text("Log in"), button:has-text("Sign in"), '
                           'button:has-text("Login"), input[type="submit"]').first
        if btn.count():
            btn.click()
        else:
            pwd.press("Enter")
    except Exception:
        pwd.press("Enter")
    page.wait_for_timeout(4000)
    print("[i] login submitted — waited 4s")


def find_real_profile_dir():
    """Locate the user's real Firefox profile (snap or apt)."""
    candidates = []
    for base in (Path.home() / ".mozilla" / "firefox",
                 Path.home() / "snap" / "firefox" / "common" / ".mozilla" / "firefox"):
        if base.is_dir():
            candidates += sorted(base.glob("*/cookies.sqlite"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime).parent


def prepare_real_profile(force=False):
    """Copy the real Firefox profile so Playwright sees the EXACT same
    session (cookies + localStorage + IndexedDB + everything). Returns the
    copy's path, or None if no profile was found.
    Reuses a recent copy (<PROFILE_MAX_AGE) unless force=True."""
    src = find_real_profile_dir()
    if src is None:
        print("[!] no real Firefox profile found")
        return None
    dst = REAL_PROFILE_COPY
    if not force and dst.exists():
        age = time.time() - dst.stat().st_mtime
        if age < PROFILE_MAX_AGE:
            print(f"[i] profile copy is fresh ({int(age)}s old) — reusing {dst}")
            return dst
    if dst.exists():
        shutil.rmtree(dst)
    ignore = shutil.ignore_patterns(
        "cache2", "startupCache", "OfflineCache", "minidumps", "safebrowsing",
        "datareporting", "crashes", "shader-cache", "telemetry",
        "parent.lock", "lock", "*.tmp")
    print(f"[i] copying real Firefox profile -> {dst} ...")
    shutil.copytree(src, dst, ignore=ignore)
    print("[i] profile ready — sessions for ALL sites included")
    return dst


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Drive the browser via Copilot/ChatGPT/DeepSeek vision")
    ap.add_argument("task", help="the task/goal for the AI (plain words)")
    ap.add_argument("--url", default=None, help="task page to open in the automation tab")
    ap.add_argument("--image", default=None, help="image for round 1 (default: screenshot of task tab)")
    ap.add_argument("--steps", type=int, default=DEFAULT_STEPS, help="max loop rounds")
    ap.add_argument("--providers", default=",".join(DEFAULT_PROVIDERS),
                    help="fallback order, comma separated")
    ap.add_argument("--no-cookies", action="store_true", help="skip real-Firefox cookie import")
    ap.add_argument("--cookie-domains", default="",
                    help="extra login domains to import, comma separated, e.g. multipolls.com "
                         "(the --url domain is added automatically)")
    ap.add_argument("--username", default=None, help="login username (auto-fill if login form shows)")
    ap.add_argument("--password", default=None, help="login password (with --username)")
    ap.add_argument("--no-real-profile", action="store_true",
                    help="use cookie/localStorage injection instead of copying your real Firefox profile "
                         "(slower to be robust; only for testing)")
    ap.add_argument("--refresh-profile", action="store_true",
                    help="force a fresh copy of the real Firefox profile")
    args = ap.parse_args()

    if re.match(r"^\s*(task\s*:|i am automating this browser)", args.task, re.I):
        print("[!] you pasted the instruction template as the task!")
        print("    pass your REAL goal, e.g.:")
        print('    .venv/bin/python3 ai_driver.py --url "https://survey-site.com" "i want to make money with this web by doing a survey"')
        return

    providers = [p.strip().lower() for p in args.providers.split(",") if p.strip()]
    prompt = INSTRUCTION.format(task=args.task)

    # domains to import cookies for: the task site (from --url) + extras
    extra_domains = []
    if args.url:
        host = urlsplit(args.url).hostname
        if host:
            extra_domains.append(host)
            parts = host.split(".")
            if len(parts) > 2:  # also the registrable domain, e.g. multipolls.com
                extra_domains.append(".".join(parts[-2:]))
    if args.cookie_domains:
        extra_domains += [d.strip() for d in args.cookie_domains.split(",") if d.strip()]

    with sync_playwright() as p:
        if args.no_real_profile:
            user_dir = PROFILE
        else:
            user_dir = prepare_real_profile(force=args.refresh_profile) or PROFILE
        ctx = p.firefox.launch_persistent_context(
            user_data_dir=str(user_dir), headless=False,
            viewport={"width": 1400, "height": 900})

        if not args.no_real_profile:
            print("[i] using full real-profile copy — all sites already signed in")
        elif not args.no_cookies:
            cookies = load_firefox_cookies(extra_domains)
            if cookies:
                try:
                    ctx.add_cookies(cookies)
                    doms = sorted({c["domain"].lstrip(".") for c in cookies})
                    print(f"[i] imported {len(cookies)} cookies from: {', '.join(doms[:12])}")
                    mp = [c for c in cookies if "multipolls" in c["domain"].lower()]
                    if mp:
                        print(f"[i] of those, {len(mp)} multipolls.com cookies")
                except Exception as e:
                    print(f"[!] cookie import failed: {e}")
            else:
                print("[!] no cookies found — sign in to the providers/task site in your normal Firefox first")

        ai_page = ctx.pages[0] if ctx.pages else ctx.new_page()
        task_page = ctx.new_page()
        if args.url:
            host = urlsplit(args.url).hostname or ""
            if host and args.no_real_profile and not args.no_cookies:
                inject_localstorage(task_page, host)
            task_page.goto(args.url)
            task_page.wait_for_load_state("domcontentloaded")
            if args.username:
                auto_login(task_page, args.username, args.password or "")
            task_page.wait_for_timeout(2500)
            task_page.screenshot(path=str(SHOTS / "task_page.png"))
            print(f"[i] task page screenshot: {SHOTS / 'task_page.png'}")
            print(f"[i] task page opened: {args.url}")
        else:
            print("[i] no --url given — commands that need the task page may fail;")
            print("    use --url to open the page you want automated")

        for step in range(1, args.steps + 1):
            print(f"\n=== round {step}/{args.steps} ===")
            if step == 1 and args.image:
                shot = Path(args.image).resolve()
            else:
                shot = SHOTS / f"round_{step}.png"
                task_page.bring_to_front()
                task_page.screenshot(path=str(shot))
            print(f"[i] screenshot: {shot}")

            done_round = False
            for provider in providers:
                reply = ask_provider(ai_page, provider, shot, args.task, prompt)
                if reply and reply.strip():
                    print(f"[{provider}] {reply!r}")
                    cmd = parse_command(reply, exclude=prompt)
                    if cmd:
                        print(f"[cmd] {cmd}")
                        try:
                            cont = execute_command(task_page, cmd)
                        except Exception as e:
                            print(f"[!] command raised: {e} — continuing (will re-ask next round)")
                            task_page.screenshot(path=str(SHOTS / f"debug_exec_{step}.png"))
                            cont = True
                        if not cont:
                            print("\n[done] task complete")
                            ctx.close()
                            return
                        done_round = True
                        break
                    else:
                        print(f"[!] {provider} replied but no command found — trying next provider")
                else:
                    print(f"[!] {provider} returned nothing — trying next provider")
            if not done_round:
                shot_dbg = SHOTS / f"debug_round_{step}.png"
                ai_page.screenshot(path=str(shot_dbg))
                print(f"[!] all providers failed this round — debug: {shot_dbg}")
                break
            task_page.wait_for_timeout(1500)

        print("\n[done] loop finished — check the browser / screenshots/")


if __name__ == "__main__":
    main()
