#!/usr/bin/env python3
"""
ai_driver.py — multi-AI vision driver: Copilot -> ChatGPT -> DeepSeek fallback.

Two tabs:
  - TAB A (task): the web page you are automating (opens --url). Screenshots
    are taken here and commands (click/fill/...) are executed here.
  - TAB B (ai): the chat tab. Each round the current screenshot is attached
    and ONE provider is asked; if its reply is empty/unparseable, the next
    provider is tried automatically.

Provider order (configurable via --providers): deepseek -> chatgpt -> copilot
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
import random
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
from PIL import Image

from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent
SHOTS = BASE / "screenshots"
SHOTS.mkdir(exist_ok=True)
PROFILE = BASE / "pw-profile"
REAL_PROFILE_COPY = BASE / "pw-real-profile"
PROFILE_MAX_AGE = 600  # seconds — reuse the profile copy if fresh enough

DEFAULT_STEPS = None  # None = run forever until 'done' or Ctrl+C
DEFAULT_PROVIDERS = ["deepseek", "chatgpt", "copilot"]  # deepseek first = main AI

INSTRUCTION = """You are controlling my browser via Playwright. I just attached a screenshot of the current page.

Reply with ONLY ONE line, one of these formats:
click 'button text'
fill 'field' with 'value'
select 'field' with 'Option Text'   (dropdowns)
type 'text'
wait '3'
scroll 'down' or 'up'
goto 'https://url'
done

Choose the single next best action for: {task}
When the task is fully done, reply exactly: done"""

SCHEMA_INSTRUCTION = """You are controlling my browser via Playwright. Here is the CURRENT page:

{schema}

{profile}

{history}

Rules:
- For each field, read its question='...' text. PERSONAL INFO questions -> USER PROFILE; SURVEY/OPINION questions -> reasonable knowledge-based answer (hợp lý).
- NEVER copy example values from question text (e.g. 'ví dụ. 1990' is just an example) — use the USER PROFILE (birth year 1988).
- Stay consistent with PREVIOUS STEPS — do not repeat or contradict what was already done.
- Use the USER PROFILE for personal questions; for tricky ones (e.g. 'which district?') reason logically from the profile (e.g. street '14 Phan Van Hon' -> Binh Tan district, Ho Chi Minh City) and give a reasonable answer.
- Only lines starting with [N] are CLICKABLE elements. Lines starting with * are page context (headings/text) — never click them.
- Inspect the HTML structure to know the widget type: native <select>, custom dropdown, date picker (3 selects: day/month/year), radio group, checkbox, slider, autocomplete. Give the input in the format the element needs (e.g. date picker -> select day/month/year; text input -> fill).
- If this is a survey/question form (input fields + a submit button), FILL the empty fields first — never click submit while fields are still empty. Fill with sensible answers.
- If a popup/modal is open, handle it first (close or accept it) before anything else.
- Ignore logo/navigation links (href='/') unless there is nothing else useful.
- Dropdowns/lists: use select [N] with 'Option Text' for <select> elements; for listed option items, click the right one directly with click [N].

Reply with ONLY ONE line, one of these formats:
click 'text'   (or click [N])
select [N] with 'Option Text'   (dropdowns/lists)
fill 'field' with 'value'   (or fill [N] with 'value')
type 'text'
wait '3'
scroll 'down' or 'up'
goto 'https://url'
done

Choose the single next best action for: {task}
When the task is fully done, reply exactly: done"""

CLASSIFY_INSTRUCTION = """You are controlling my browser via Playwright. Here is the CURRENT page:

{schema}

The survey question appears to be: {question}

What kind of page is this? Reply with EXACTLY ONE line, one of:
survey form     (a question/form page with input fields, selects, radio buttons and a submit/next button)
command web     (a normal page with buttons/links to click, not a fill-in form)

Just the one line, nothing else."""

FORM_INSTRUCTION = """You are controlling my browser via Playwright. This is a SURVEY QUESTION FORM. Here is the current page:

{schema}

The survey QUESTION is: {question}

{profile}

{history}

{personal}

Task: {task}

Reply with ALL the commands needed to complete this form, ONE PER LINE, in order, e.g.:
fill [1] with '1990'
fill [3] with 'Ho Chi Minh City'
click [6]

Allowed commands: click [N] / select [N] with 'X' / fill [N] with 'X' / type 'X' / wait '3' / scroll 'down' or 'up' / goto 'https://url' / done

Rules:
- PERSONAL INFO ANSWERS above are the EXACT user values — use them VERBATIM, never invent different ones (the user's birthday is 8/12/1988, never 5/8/1988).
- For each field, read its question='...' text.
- PERSONAL INFO questions (birthday, age, address, income, family, occupation...) -> use the USER PROFILE.
- SURVEY/OPINION questions -> answer from general knowledge with a REASONABLE, sensible answer (hợp lý) — never random, never contradictory.
- Use the USER PROFILE for personal questions (birthday, city, address, income...). The user's BIRTH YEAR is 1988.
- NEVER copy example values from the question text (e.g. 'ví dụ. 1990' is only an example — the real birth year is 1988).
- For tricky questions (e.g. 'which district do you live in?'), REASON from the profile: '14 Phan Van Hon' is in Binh Tan district, Ho Chi Minh City — give a logical, reasonable answer, never a random one.
- Stay consistent with PREVIOUS STEPS — do not re-fill or contradict what was already done.
- Fill every empty required field, then click the submit/next button.
- SKIP fields that already have a value='...' — do not re-fill them.
- One command per line. No explanations, no numbering.
- Do not add 'done' until the whole task is truly finished.
- When the whole task is fully done, reply exactly: done"""

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
    selectors = ('[contenteditable="true"]', '[role="textbox"]', "textarea",
                 '[aria-label*="Ask"]', '[aria-label*="prompt"]', '[aria-label*="message"]')
    for sel in selectors:
        try:
            loc = page.locator(sel).last
            if loc.count():
                try:
                    if loc.is_visible():
                        return loc
                except Exception:
                    return loc
        except Exception:
            continue
    return None


def quoted(s):
    m = re.search(r"""['"]([^'"]*)['"]""", s)
    return m.group(1) if m else s


def images_similar(a_path, b_path, threshold=6.0):
    """True if two screenshots look basically the same (mean pixel diff
    below the threshold, compared at low resolution)."""
    try:
        a = Image.open(a_path).convert("RGB").resize((160, 100))
        b = Image.open(b_path).convert("RGB").resize((160, 100))
        diff = np.abs(np.asarray(a, dtype=np.int16)
                      - np.asarray(b, dtype=np.int16)).mean()
        return diff < threshold
    except Exception:
        return False


SCHEMA_JS = """(startN) => {
    const seen = new Set();
    const out = [];
    const ctx = [];
    const vw = window.innerWidth, vh = window.innerHeight;
    const MAX = startN + 150;
    let n = startN;
    const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
    const consider = (el) => {
        if (n >= MAX) return;
        if (el.tagName === 'IFRAME') return;
        if (el.disabled) return;
        if (el.closest('[aria-hidden="true"]')) return;
        if (el.closest('[data-testid*="toast" i], [class*="toast" i]')) return;
        const r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) return;
        if (r.bottom < -1000 || r.top > vh + 6000 || r.right < -500 || r.left > vw + 500) return;
        const st = getComputedStyle(el);
        if (st.display === 'none' || st.visibility === 'hidden') return;
        const tag = el.tagName.toLowerCase();
        const role = (el.getAttribute('role') || '').toLowerCase();
        if (role === 'presentation' || role === 'none') return;
        const tabbable = el.hasAttribute('tabindex') && parseInt(el.getAttribute('tabindex'), 10) >= 0;
        const isHard = tag === 'a' || tag === 'button' || tag === 'input' || tag === 'textarea' || tag === 'select'
            || el.getAttribute('contenteditable') === 'true' || el.hasAttribute('onclick') || !!role || tabbable;
        const pointer = st.cursor === 'pointer';
        if (!isHard && !pointer && (tag !== 'div' && tag !== 'span')) return;
        const type = el.getAttribute('type') || '';
        const aria = clean(el.getAttribute('aria-label'));
        const ph = clean(el.getAttribute('placeholder'));
        const name = clean(el.getAttribute('name'));
        const href = clean(el.getAttribute('href'));
        const title = clean(el.getAttribute('title'));
        const alt = clean((el.querySelector('img') || {}).alt);
        const val = clean(el.getAttribute('value')).slice(0, 30);
        const testid = clean(el.getAttribute('data-testid'));
        let text = (clean(el.textContent) || clean(el.innerText)).slice(0, 60);
        if (!text && (tag === 'span' || tag === 'div')) {
            // custom radios hide the label: sibling text or CSS pseudo-content
            try {
                const sib = el.nextElementSibling || el.previousElementSibling;
                if (sib) text = clean(sib.textContent).slice(0, 40);
            } catch (e) {}
            if (!text) {
                for (const p of [getComputedStyle(el, '::before'), getComputedStyle(el, '::after')]) {
                    if (p.content && p.content !== 'none' && p.content !== 'normal'
                        && p.content !== '""' && p.content !== "''") {
                        text = p.content.replace(/['"]/g, '').slice(0, 40);
                        break;
                    }
                }
            }
        }
        // react-native-web clickables often have NO role/tabindex/cursor — they
        // are plain divs with text. Accept them unless they wrap a real control.
        const hasInteractiveKid = el.querySelector('button, a, input, textarea, select, [role="button"], [role="link"], [onclick]') !== null;
        if (!isHard && !pointer && !(text.length >= 2 && !hasInteractiveKid)) return;
        let labelTxt = '';
        try {
            if (el.labels && el.labels.length) labelTxt = clean(el.labels[0].innerText).slice(0, 40);
        } catch (e) {}
        const key = tag + '|' + type + '|' + (text || aria || ph || name || type || tag) + '|' + href + '|' + pointer
            + '|' + Math.round(r.top / 60) + '|' + Math.round(r.left / 60);
        if (seen.has(key)) return;
        seen.add(key);
        el.setAttribute('data-ai', String(n));
        let d = '[' + n + '] ' + tag;
        if (type) d += ' type=' + type;
        if (role) d += ' role=' + role;
        if (text) d += " text='" + text + "'";
        if (aria && aria !== text) d += " aria='" + aria + "'";
        if (ph) d += " placeholder='" + ph + "'";
        if (labelTxt) d += " label='" + labelTxt + "'";
        if (name) d += " name='" + name + "'";
        if (val && (tag === 'button' || type === 'submit' || type === 'button')) d += " value='" + val + "'";
        if (title && title !== text) d += " title='" + title + "'";
        if (alt) d += " img_alt='" + alt + "'";
        if (testid) d += " testid='" + testid + "'";
        if (href && href !== '#') d += " href='" + href.slice(0, 60) + "'";
        if (tag === 'select') {
            const opts = Array.from(el.options || []).slice(0, 15)
                .map(o => clean(o.text).slice(0, 30)).filter(Boolean).join(', ');
            if (opts) d += " options='" + opts + "'";
        }
        if ((tag === 'input' || tag === 'textarea') && type !== 'password') {
            const cur = clean(el.value).slice(0, 40);
            if (cur) d += " value='" + cur + "'";
        }
        if (tag === 'input' || tag === 'textarea' || tag === 'select') {
            let q = '';
            try {
                if (el.labels && el.labels.length) q = clean(el.labels[0].innerText).slice(0, 80);
            } catch (e) {}
            if (!q) {
                let p = el.parentElement;
                for (let i = 0; i < 3 && p && !q; i++) {
                    const t = clean(p.innerText);
                    if (t && t.length < 120 && t !== text) q = t;
                    p = p.parentElement;
                }
            }
            if (q) d += " question='" + q + "'";
        }
        out.push(d);
        n++;
    };
    document.querySelectorAll('a, button, input, textarea, select, li, [contenteditable="true"], [onclick], [role]').forEach(consider);
    if (n < MAX) document.querySelectorAll('div, span').forEach(consider);
    for (const h of document.querySelectorAll('h1, h2, h3')) {
        const t = clean(h.innerText).slice(0, 80);
        if (t) ctx.push('* ' + h.tagName.toLowerCase() + " '" + t + "'");
    }
    let bodyText = clean(document.body && document.body.innerText);
    if (bodyText.length > 600) bodyText = bodyText.slice(0, 600) + '...';
    if (bodyText) ctx.push("* page text: '" + bodyText + "'");
    return { title: document.title, url: location.href, elements: out, ctx: ctx, next: n };
}"""


def frame_visible(fr, vw, vh):
    """True if the frame's <iframe> element is on-screen in the parent."""
    try:
        r = fr.frame_element().evaluate(
            "el => { const r = el.getBoundingClientRect(); "
            "return [r.left, r.top, r.right, r.bottom, r.width, r.height]; }")
        left, top, right, bottom, w, h = r
        if w < 2 or h < 2:
            return False
        if right < 0 or bottom < 0 or left > vw or top > vh:
            return False
        return True
    except Exception:
        return False


def _extract_schema_once(page):
    """One pass: collect visible interactive elements (main frame + iframes)."""
    try:
        frames = page.frames
        vw = (page.viewport_size or {}).get("width") or 1400
        vh = (page.viewport_size or {}).get("height") or 900
        all_lines = []
        n = 0
        for idx, fr in enumerate(frames):
            if idx > 0:
                if not fr.url or fr.url.startswith("about:"):
                    continue
                if not frame_visible(fr, vw, vh):
                    continue
            data = fr.evaluate(SCHEMA_JS, n)
            n = data["next"]
            if idx == 0:
                all_lines.append(f"URL: {data['url']}")
                all_lines.append(f"Title: {data['title']}")
            else:
                host = (fr.url.split("/")[2] if "/" in fr.url else fr.url) or "frame"
                all_lines.append(f"--- frame[{idx}] {host} ---")
            all_lines.extend(data.get("ctx", []))
            all_lines.extend(data["elements"])
            if n >= 300:
                break
        return "\n".join(all_lines)
    except Exception as e:
        print(f"[!] schema extraction failed: {e}")
        return "URL: unknown\nTitle: unknown\n(no elements)"


def extract_schema(page):
    """Schema with patience: if the page is mid-redirect/blank (0 clickable
    elements), wait and re-extract a few times before giving up."""
    for attempt in range(1, 7):
        text = _extract_schema_once(page)
        elem_count = sum(1 for ln in text.splitlines() if ln.startswith("["))
        if elem_count > 0 or attempt == 6:
            if elem_count == 0:
                print("[i] schema has 0 clickable elements — page may be a redirect/blank")
            return text
        print(f"[i] no clickable elements yet (loading/redirecting?) — retrying ({attempt}/6)")
        page.wait_for_timeout(3000)
    return text


def resolve_element(page, n):
    """Find data-ai=N in ANY frame (main + iframes)."""
    for fr in page.frames:
        try:
            loc = fr.locator(f'[data-ai="{n}"]').first
            if loc.count():
                return loc
        except Exception:
            continue
    return None


def parse_command(reply, exclude=""):
    """Find the last command-looking line in the AI's reply.
    Only a FULL echo of our own prompt is rejected (substring matching was
    too aggressive: 'scroll \'down\'' appears inside our format examples)."""
    if exclude:
        if reply.strip() == exclude.strip():
            return None
        if reply[:80] == exclude[:80] and len(reply) > 0.8 * len(exclude):
            return None  # the AI echoed our whole prompt back
    for line in reversed(reply.strip().splitlines()):
        line = line.strip().lstrip("*-`# ")
        if not line:
            continue
        if re.match(r"^(click|select|fill|type|wait|scroll|goto|done)\b", line, re.I):
            return line
    return None


def parse_commands(reply, exclude=""):
    """ALL command lines in the AI's reply, in order (multi-command forms)."""
    if exclude:
        if reply.strip() == exclude.strip():
            return []
        if reply[:80] == exclude[:80] and len(reply) > 0.8 * len(exclude):
            return []
    out = []
    for line in reply.strip().splitlines():
        line = line.strip().lstrip("*-`# ")
        if not line:
            continue
        if re.match(r"^(click|select|fill|type|wait|scroll|goto|done)\b", line, re.I):
            out.append(line)
    return out


def extract_question(schema_text):
    """Find the survey question in the schema (first line containing '?')."""
    for ln in schema_text.splitlines():
        if "?" in ln:
            return ln.strip()[:140]
    return ""


ERROR_MARKERS = ("lỗi", "không đúng", "chính xác", "error", "invalid", "wrong",
                 "vui lòng", "định dạng", "ít nhất", "required", "bắt buộc",
                 "không hợp lệ", "mismatch", "thử lại", "failed")


def extract_errors(schema_text):
    """Find error/warning text on the page (validation messages etc.) so the
    AI knows WHY its last action failed."""
    hits = []
    for ln in schema_text.splitlines():
        low = ln.lower()
        if any(m in low for m in ERROR_MARKERS):
            t = ln.strip().strip("'")[:180]
            if t and t not in hits:
                hits.append(t)
    return hits[:4]

QUESTION_EXTRACT_INSTRUCTION = """This is a SURVEY QUESTION FORM. Here is the page:

{schema}

{profile}

Extract the question(s) on this page. Reply ONE LINE PER QUESTION, exactly this format:
Q: <the question text> | K: personal | A: <exact answer from USER PROFILE if it is personal info, else leave A empty>
or for non-personal questions:
Q: <the question text> | K: survey

Rules:
- K is 'personal' when the question asks about birthday, age, address, zip, income, family, gender, occupation...
- For personal questions, put the EXACT profile value in A (e.g. birthday is 8/12/1988, zip 700000).
- For survey/opinion questions, leave A empty.
- No other text."""


def parse_questions(reply):
    """Parse the AI's question extraction into (question, kind, answer) list."""
    out = []
    for line in reply.strip().splitlines():
        m = re.match(r"^\s*Q:\s*(.*?)\s*\|\s*K:\s*(\w+)", line, re.I)
        if not m:
            continue
        q = m.group(1).strip().strip("'\"")
        kind = m.group(2).lower()
        a = ""
        am = re.search(r"\|\s*A:\s*(.*)$", line, re.I)
        if am:
            a = am.group(1).strip().strip("'\"")
        out.append((q, kind, a))
    return out


def match_profile(question, profile):
    """Match a personal question to a profile entry by alias; returns (key, value)."""
    q = question.lower()
    best = None
    for key, entry in profile.items():
        if isinstance(entry, str):
            aliases = [key]
            val = entry
        else:
            aliases = [key] + [a for a in entry.get("aliases", []) if a]
            val = entry.get("value", "")
        for a in aliases:
            if a and a.lower() in q:
                if best is None or len(a) > len(best[0]):
                    best = (a, key, val)
    if best:
        return best[1], best[2]
    return None, ""

def click_text(page, text):
    """Click an element by text with a cascade of fallbacks.
    Strips trailing prices ('Start survey $0.25' -> 'Start survey'), tries
    button/link/text in the main page, then searches iframes too."""
    candidates = [text]
    t = text
    while True:
        t2 = re.sub(r"\s+\$?[\d.,]+$", "", t).strip()  # strip trailing price/number
        if not t2 or t2 == t:
            break
        candidates.append(t2)
        t = t2
    for cand in candidates:
        for role in ("button", "link", "radio", "checkbox"):
            try:
                page.get_by_role(role, name=cand, exact=False).first.click(timeout=3000)
                print(f"[exec] click '{cand}' (role={role})")
                return True
            except Exception:
                continue
        try:
            page.get_by_text(cand, exact=False).first.click(timeout=3000)
            print(f"[exec] click '{cand}' (by text)")
            return True
        except Exception:
            pass
        # iframe fallback (survey content often lives in an iframe)
        for fr in page.frames[1:]:
            host = (fr.url.split("/")[2] if "/" in fr.url else "?")
            for role in ("button", "link"):
                try:
                    fr.get_by_role(role, name=cand, exact=False).first.click(timeout=3000)
                    print(f"[exec] click '{cand}' (iframe {host}, role={role})")
                    return True
                except Exception:
                    continue
            try:
                fr.get_by_text(cand, exact=False).first.click(timeout=3000)
                print(f"[exec] click '{cand}' (iframe {host}, by text)")
                return True
            except Exception:
                continue
    print(f"[!] could not click '{text}' (tried: {candidates})")
    return False


def click_by_ocr(page, text):
    """Fallback for controls whose labels are invisible to the DOM (custom
    radios etc.): find the text on screen via OCR (tesseract) and click its
    position. Screenshot coordinates map directly to page.mouse.click."""
    try:
        import pytesseract
        shot = page.screenshot()
        data = pytesseract.image_to_data(shot, output_type=pytesseract.Output.DICT)
        for i, w in enumerate(data["text"]):
            if w and text.lower() in w.lower():
                x = data["left"][i] + data["width"][i] // 2
                y = data["top"][i] + data["height"][i] // 2
                page.mouse.click(x, y)
                print(f"[ocr] clicked '{w}' at ({x}, {y})")
                return True
        print(f"[ocr] '{text}' not found on screen (OCR)")
        return False
    except Exception as e:
        print(f"[!] OCR click failed: {e}")
        return False


def find_input(page, hint):
    sels = [f'input[placeholder*="{hint}"]', f'textarea[placeholder*="{hint}"]',
            f'select[aria-label*="{hint}"]', f'[aria-label*="{hint}"]', "input", "textarea", "select"]
    for sel in sels:
        try:
            loc = page.locator(sel).first
            if loc.count():
                return loc
        except Exception:
            continue
    return None


PROFILE_FILE = BASE / "profile.json"
MONTH_NAMES = ["jan", "feb", "mar", "apr", "may", "jun",
               "jul", "aug", "sep", "oct", "nov", "dec"]


def load_profile():
    """Load profile.json (auto-answers for common survey questions)."""
    if PROFILE_FILE.exists():
        try:
            return json.loads(PROFILE_FILE.read_text())
        except Exception as e:
            print(f"[!] profile.json parse error: {e}")
    return {}


def fill_value(page, loc, value):
    """Fill an input reliably: clear existing text, set value, VERIFY it
    stuck, and fall back to clear+keyboard typing (react-native-web
    controlled inputs sometimes ignore fill)."""
    try:
        loc.fill("")
    except Exception:
        pass
    try:
        loc.fill(str(value))
    except Exception:
        loc.click(timeout=5000)
        page.keyboard.press("Control+a")  # select any existing text first
        page.keyboard.type(str(value), delay=15)
        return
    try:
        got = (loc.input_value() or "").strip()
    except Exception:
        got = ""
    if got != str(value).strip():
        print(f"[i] fill did not stick ('{got}') — clearing and typing via keyboard")
        loc.click(timeout=5000)
        page.keyboard.press("Control+a")
        page.keyboard.type(str(value), delay=15)


def profile_context(profile):
    """Human-readable summary of profile.json for the AI (ground truth for
    personal questions; the AI reasons logically for tricky ones)."""
    if not profile:
        return ""
    lines = ["USER PROFILE (use these to answer personal questions; reason logically for tricky ones):"]
    for key, entry in profile.items():
        if isinstance(entry, str):
            v = entry
        else:
            v = entry.get("value", "")
        lines.append(f"- {key.replace('_', ' ')}: {v}")
    return "\n".join(lines)


def auto_answer(page, schema_text, profile):
    """Fill common survey questions directly from profile.json — no AI needed.
    Handles text inputs, date inputs, native selects (incl. day/month/year
    date pickers) and clickable options. Returns True if anything was filled."""
    if not profile:
        return False
    rules = []
    for key, entry in profile.items():
        if isinstance(entry, str):
            value, meta = entry, {}
            aliases = [key]
        else:
            value = entry.get("value", "")
            meta = entry
            aliases = [key] + [a for a in entry.get("aliases", [])]
        for a in aliases:
            if a:
                rules.append((a.lower(), key, value, meta))
    rules.sort(key=lambda r: len(r[0]), reverse=True)  # most specific first

    filled_any = False
    for line in schema_text.splitlines():
        if not line.startswith("["):
            continue
        m = re.match(r"^\[(\d+)\]\s+(\w+)", line)
        if not m:
            continue
        n, tag = int(m.group(1)), m.group(2)
        if " value='" in line:
            continue  # field already has a value — don't re-fill
        hint = line.lower()
        hit = next(((a, k, v, meta) for a, k, v, meta in rules if a in hint), None)
        if hit is None:
            continue
        alias, key, value, meta = hit
        loc = resolve_element(page, n)
        if loc is None:
            continue
        try:
            if tag == "select":
                opts = line.split("options='", 1)
                opts_lower = opts[1].split("'", 1)[0].lower() if len(opts) > 1 else ""
                done = False
                if value:
                    try:
                        loc.select_option(label=value)
                        done = True
                    except Exception:
                        try:
                            loc.select_option(value=value)
                            done = True
                        except Exception:
                            pass
                dp = meta.get("date_parts") if isinstance(meta, dict) else None
                if not done and dp:
                    if any(mon in opts_lower for mon in MONTH_NAMES):
                        try:
                            loc.select_option(label=dp["month_name"])
                        except Exception:
                            loc.select_option(label=dp.get("month_vn", dp["month_name"]))
                    elif re.search(r"\b(19|20)\d\d\b", opts_lower):
                        loc.select_option(label=dp["year"])
                    else:
                        loc.select_option(label=dp["day"])
                print(f"[profile] select [{n}] '{key}' = '{value}'")
                filled_any = True
            elif tag == "input":
                itype = ""
                mm = re.search(r"type='([^']*)'", line)
                if mm:
                    itype = mm.group(1)
                val = value
                dp = meta.get("date_parts") if isinstance(meta, dict) else None
                if itype == "date" and dp:
                    val = dp.get("iso", value)
                elif dp and "year" in hint:
                    val = dp.get("year", value)          # birthday_year -> 1988
                elif dp and "month" in hint:
                    val = dp.get("month_name", value)
                elif dp and "day" in hint:
                    val = dp.get("day", value)
                fill_value(page, loc, val)
                print(f"[profile] fill [{n}] '{key}' = '{val}'")
                filled_any = True
            elif tag == "textarea":
                fill_value(page, loc, value)
                print(f"[profile] fill [{n}] '{key}' = '{value}'")
                filled_any = True
            else:
                # radio / checkbox / div / li / span option — click the matching one
                if click_text(page, value):
                    print(f"[profile] click '{value}' for '{key}'")
                    filled_any = True
                elif isinstance(meta, dict) and meta.get("vn") and click_text(page, meta["vn"]):
                    print(f"[profile] click '{meta['vn']}' for '{key}'")
                    filled_any = True
        except Exception as e:
            print(f"[!] profile answer failed for '{key}' on [{n}]: {e}")
    return filled_any


def execute_command(page, line):
    m = re.match(r"^(\w+)\b(.*)$", line.strip(), re.S)
    cmd, rest = m.group(1).lower(), m.group(2).strip()

    if cmd == "click":
        m = re.match(r"^\[(\d+)\]$", rest.strip())
        if m:
            loc = resolve_element(page, int(m.group(1)))
            if loc is not None:
                loc.click(timeout=8000)
                print(f"[exec] click element [{m.group(1)}]")
            else:
                print(f"[!] element [{m.group(1)}] not found — trying OCR text click")
                click_by_ocr(page, quoted(rest))
        else:
            if not click_text(page, quoted(rest)):
                click_by_ocr(page, quoted(rest))
    elif cmd == "select":
        mw = re.search(r"\s+with\s+", rest, re.I)
        if not mw:
            print(f"[!] bad select command: {line}")
            return True
        target = rest[:mw.start()].strip().strip("'\"")
        value = rest[mw.end():].strip().strip("'\"")
        m = re.match(r"^\[(\d+)\]$", target)
        loc = resolve_element(page, int(m.group(1))) if m else find_input(page, target)
        if loc is None:
            print(f"[!] no select found for '{target}'")
            return True
        try:
            loc.select_option(label=value)
            print(f"[exec] select '{value}' in '{target}'")
        except Exception:
            try:
                loc.select_option(value=value)
                print(f"[exec] select value '{value}' in '{target}'")
            except Exception:
                print(f"[!] could not select '{value}' — trying first option")
                try:
                    loc.select_option(index=0)
                except Exception:
                    pass
    elif cmd == "fill":
        mw = re.search(r"\s+with\s+", rest, re.I)
        if not mw:
            print(f"[!] bad fill command: {line}")
            return True
        target = rest[:mw.start()].strip().strip("'\"")
        value = rest[mw.end():].strip().strip("'\"")
        m = re.match(r"^\[(\d+)\]$", target)
        loc = resolve_element(page, int(m.group(1))) if m else find_input(page, target)
        if loc is None:
            print(f"[!] no input found for '{target}'")
            return True
        try:
            fill_value(page, loc, value)
        except Exception:
            pass
        print(f"[exec] fill '{target}' with '{value}'")
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


def type_into(page, textbox, text):
    """Paste multi-line text into a contenteditable via clipboard (fast, no
    Enter keypresses, no slow line-by-line typing). Returns True on success."""
    textbox.click(timeout=5000)
    page.wait_for_timeout(300)
    # method 1: execCommand copy + paste
    try:
        ok = page.evaluate("""(t) => {
            const ta = document.createElement('textarea');
            ta.value = t;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            const ok = document.execCommand('copy');
            ta.remove();
            return ok;
        }""", text)
        if ok:
            page.keyboard.press("Control+v")
            page.wait_for_timeout(600)
            got = (textbox.inner_text() or "").strip()
            if (text[:30] in got) or len(got) > 100:
                print(f"[i] pasted {len(text)} chars via clipboard (execCommand)")
                return True
    except Exception as e:
        print(f"[!] clipboard paste failed: {e}")
    # method 2: navigator.clipboard
    try:
        page.evaluate("(t) => navigator.clipboard.writeText(t)", text)
        page.wait_for_timeout(300)
        page.keyboard.press("Control+v")
        page.wait_for_timeout(600)
        got = (textbox.inner_text() or "").strip()
        if (text[:30] in got) or len(got) > 100:
            print(f"[i] pasted {len(text)} chars via clipboard (navigator)")
            return True
    except Exception as e:
        print(f"[!] navigator.clipboard failed: {e}")
    print("[!] clipboard paste unavailable — cannot fill composer (skipping provider)")
    return False


def send_message(page, provider, text):
    textbox = None
    if provider == "chatgpt":
        # the visible composer is a contenteditable div; the textarea is a hidden a11y fallback
        for sel in ('[contenteditable="true"]', '#prompt-textarea', '[role="textbox"]'):
            try:
                loc = page.locator(sel).last
                if loc.count():
                    try:
                        if loc.is_visible():
                            textbox = loc
                            break
                    except Exception:
                        textbox = loc
                        break
            except Exception:
                continue
    if textbox is None:
        textbox = find_textbox(page)
    if textbox is None:
        print(f"[!] {provider}: message input not found")
        return False

    try:
        tag = textbox.evaluate("el => el.tagName.toLowerCase()")
    except Exception:
        tag = None
    try:
        if tag in ("textarea", "input"):
            textbox.click(timeout=5000)
            textbox.fill(text)
        else:
            # contenteditable — paste via clipboard: newlines stay newlines,
            # NO Enter presses (Enter would send the message prematurely)
            if not type_into(page, textbox, text):
                return False
    except Exception as e:
        print(f"[!] {provider}: composer click/fill failed: {e}")
        try:
            page.screenshot(path=str(SHOTS / f"debug_{provider}_composer.png"))
        except Exception:
            pass
        return False
    page.wait_for_timeout(500)
    # verify text actually landed
    try:
        filled = (textbox.input_value() or "").strip() or (textbox.inner_text() or "").strip()
        print(f"[i] {provider}: composer now has {len(filled)} chars")
        if not filled:
            print(f"[!] {provider}: composer empty after typing — retrying")
            try:
                textbox.click(timeout=5000)
                type_into(page, textbox, text)
            except Exception as e2:
                print(f"[!] {provider}: retype failed: {e2}")
                return False
    except Exception:
        pass
    page.wait_for_timeout(400)

    sent = False
    if provider == "chatgpt":
        for sel in ('[data-testid="send-button"]',
                    'button[aria-label="Send prompt"]',
                    'button[aria-label="Send message"]'):
            try:
                loc = page.locator(sel).first
                if visible(loc, 1500):
                    loc.click()
                    sent = True
                    break
            except Exception:
                continue
    if not sent:
        for name in ["Send", "Send message", "Send prompt"]:
            btn = page.get_by_role("button", name=name, exact=False).first
            if visible(btn, 1200):
                btn.click()
                sent = True
                break
    if not sent:
        page.keyboard.press("Enter")
    return True


def confirm_sent(page, provider, marker):
    """Verify the message was ACTUALLY submitted before we blame the AI for
    not replying. Confirms via: input cleared, our unique marker visible in
    the thread, or a Stop/generating button appeared."""
    deadline = time.time() + 8
    while time.time() < deadline:
        # 1) input cleared = send consumed it
        try:
            tb = provider_textbox(page, provider)
            if tb is not None and tb.count():
                val = (tb.input_value() or "").strip() or (tb.inner_text() or "").strip()
                if not val:
                    return True
        except Exception:
            pass
        # 2) our unique round marker is visible in the thread (user message)
        try:
            for sel in ('[data-message-author-role="user"]',
                        '[data-content="user-message"]', '[data-content="user"]'):
                loc = page.locator(sel)
                n = loc.count()
                if n:
                    last = loc.nth(n - 1).inner_text()
                    if marker and marker in last:
                        return True
        except Exception:
            pass
        # 3) generation started
        try:
            stop = page.locator('button:has-text("Stop")').first
            if stop.count() and stop.is_visible():
                return True
        except Exception:
            pass
        page.wait_for_timeout(1000)
    return False


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


def ask_provider(page, provider, image, task, prompt, attach=True, marker=""):
    """One round on one provider. attach=False = schema mode (no image).
    Verifies the message was actually sent before waiting for a reply.
    Returns reply text or None."""
    urls = {"copilot": "https://copilot.microsoft.com/",
            "chatgpt": "https://chatgpt.com/",
            "deepseek": "https://chat.deepseek.com/"}
    print(f"\n[ai] asking {provider} ...")
    try:
        page.goto(urls[provider], wait_until="domcontentloaded")  # chatgpt.com never finishes 'load'
    except Exception as e:
        print(f"[!] {provider}: navigation failed: {e}")
        return None
    page.wait_for_timeout(2000)
    if not wait_for_ready(page, provider):
        return None
    if provider in ("chatgpt", "deepseek"):
        print(f"[i] {provider}: waiting 10s for the composer to fully load...")
        page.wait_for_timeout(10000)
    try:
        if attach:
            if not attach_image(page, provider, image):
                return None
        if not send_message(page, provider, prompt):
            return None
        if not confirm_sent(page, provider, marker):
            print(f"[!] {provider}: message NOT confirmed sent — retrying once")
            page.wait_for_timeout(1500)
            if not send_message(page, provider, prompt):
                print(f"[!] {provider}: retry send failed — skipping provider")
                return None
            if not confirm_sent(page, provider, marker):
                print(f"[!] {provider}: still not confirmed — skipping provider")
                try:
                    page.screenshot(path=str(SHOTS / f"debug_{provider}_notsent.png"))
                except Exception:
                    pass
                return None
        print(f"[i] {provider}: message confirmed sent")
    except Exception as e:
        print(f"[!] {provider}: send/attach raised: {e}")
        try:
            page.screenshot(path=str(SHOTS / f"debug_{provider}.png"))
        except Exception:
            pass
        return None
    print(f"[i] waiting for {provider} reply...")
    return extract_reply(page, provider)


def ask_once(page, providers, prompt, marker):
    """Ask providers in order for a plain text reply (no command execution)."""
    for provider in providers:
        reply = ask_provider(page, provider, None, "", prompt, attach=False, marker=marker)
        if reply and reply.strip():
            return reply, provider
    return None, None


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
    ap.add_argument("--steps", type=int, default=DEFAULT_STEPS,
                    help="max loop rounds (default: infinite until 'done' or Ctrl+C)")
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
    ap.add_argument("--mode", choices=["auto", "schema", "image"], default="auto",
                    help="how the AI sees the page: schema (text only, no image rate limits), "
                         "image (screenshot), auto (image first, schema fallback)")
    ap.add_argument("--progress-threshold", type=float, default=6.0,
                    help="mean pixel diff below which the page counts as 'unchanged' "
                         "(progress detection)")
    args = ap.parse_args()

    if re.match(r"^\s*(task\s*:|i am automating this browser)", args.task, re.I):
        print("[!] you pasted the instruction template as the task!")
        print("    pass your REAL goal, e.g.:")
        print('    .venv/bin/python3 ai_driver.py --url "https://survey-site.com" "i want to make money with this web by doing a survey"')
        return

    providers = [p.strip().lower() for p in args.providers.split(",") if p.strip()]
    prompt = INSTRUCTION.format(task=args.task)
    profile = load_profile()
    if profile:
        print(f"[i] profile loaded: {len(profile)} auto-answers (birthday, address, income, ...)")
    profile_ctx = profile_context(profile)

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

        step = 0
        consecutive_fails = 0
        last_shot, last_cmd = None, None
        classified_url, is_form = None, False
        questions_done_url, personal_block = None, ""
        history = []

        def remember(msg):
            history.append(msg)
            if len(history) > 25:
                del history[0]
        try:
            while True:
                step += 1
                if args.steps and step > args.steps:
                    print(f"\n[info] reached --steps {args.steps} limit — stopping")
                    break
                print(f"\n=== round {step} ===")
                # adopt any popup/new tab that appeared since last round
                # (survey sites often open popups late, after the command ran)
                new_pages = [pg for pg in ctx.pages if pg not in (ai_page, task_page)]
                if new_pages:
                    old = task_page
                    task_page = new_pages[0]
                    try:
                        task_page.wait_for_load_state("domcontentloaded", timeout=15000)
                    except Exception:
                        pass
                    try:
                        old.close()
                    except Exception:
                        pass
                    print("[i] adopted a new popup/tab as the task page")
                    remember(f"round {step}: adopted popup -> {task_page.url}")
                if step == 1 and args.image:
                    shot = Path(args.image).resolve()
                else:
                    shot = SHOTS / f"round_{step}.png"
                    task_page.bring_to_front()
                    task_page.screenshot(path=str(shot))
                print(f"[i] screenshot: {shot}")

                # --- progress check: did the page change since last round? ---
                no_progress = False
                if (last_shot is not None and last_cmd
                        and last_cmd.split(None, 1)[0].lower() in ("click",)
                        and images_similar(shot, last_shot, args.progress_threshold)):
                    no_progress = True
                    print(f"[!] no page change after '{last_cmd}' — will ask for a DIFFERENT action")
                last_shot = shot

                prompt_round = prompt
                if no_progress:
                    prompt_round = (prompt + "\n\nNOTE: I did what you suggested (" + last_cmd +
                                    ") but the page looks identical — nothing happened. "
                                    "Do NOT repeat that action. Suggest a DIFFERENT button or approach.")

                marker = f"round marker {step}-{random.randint(1000, 9999)}"
                round_modes = ["image", "schema"] if args.mode == "auto" else [args.mode]
                done_round = False
                for rmode in round_modes:
                    if rmode == "schema":
                        schema_text = extract_schema(task_page)
                        print(f"--- schema (round {step}) ---")
                        print(schema_text[:4000])
                        print("--- end schema ---")
                        try:
                            (SHOTS / f"schema_round_{step}.txt").write_text(schema_text)
                        except Exception:
                            pass
                        auto_filled = auto_answer(task_page, schema_text, profile)
                        if auto_filled:
                            print("[i] profile auto-answers applied — continuing the flow")
                            task_page.wait_for_timeout(800)
                        # classify the page once per URL: command web vs survey form
                        if classified_url != task_page.url:
                            classify_prompt = CLASSIFY_INSTRUCTION.format(schema=schema_text, question=extract_question(schema_text)) + f"\n{marker}"
                            reply_c, prov_c = ask_once(ai_page, providers, classify_prompt, marker)
                            if reply_c:
                                low = reply_c.lower()
                                is_form = ("form" in low and "command" not in low)
                                print(f"[{prov_c}] page type: {reply_c.strip()[:80]}")
                            else:
                                is_form = ("input" in schema_text or "select" in schema_text
                                           or "textarea" in schema_text)
                                print(f"[i] classify failed — heuristic: {'form' if is_form else 'command web'}")
                            classified_url = task_page.url
                            print(f"[i] page classified: {'SURVEY FORM' if is_form else 'COMMAND WEB'}")
                            done_round = True
                            consecutive_fails = 0
                            break
                        history_text = ""
                        if history:
                            history_text = ("PREVIOUS STEPS (what you already did — stay consistent):\n"
                                            + "\n".join(f"- step {i+1}: {h}" for i, h in enumerate(history)))
                        # step 1 of form mode: extract the questions + their kind
                        if is_form and questions_done_url != task_page.url:
                            extract_prompt = QUESTION_EXTRACT_INSTRUCTION.format(schema=schema_text, profile=profile_ctx) + f"\n{marker}"
                            reply_q, prov_q = ask_once(ai_page, providers, extract_prompt, marker)
                            personal_block = ""
                            if reply_q:
                                qs = parse_questions(reply_q)
                                lines = ["PERSONAL INFO ANSWERS (use EXACTLY these — do not invent):"]
                                added = False
                                for q, kind, a in qs:
                                    if kind == "personal":
                                        if not a:
                                            _, a = match_profile(q, profile)
                                        if a:
                                            lines.append(f"- {q}: {a}")
                                            added = True
                                if added:
                                    personal_block = "\n".join(lines)
                                print(f"[{prov_q}] extracted {len(qs)} question(s)")
                            else:
                                print("[!] question extraction failed — continuing without it")
                            questions_done_url = task_page.url
                            done_round = True
                            consecutive_fails = 0
                            break
                        if is_form:
                            prompt_round = FORM_INSTRUCTION.format(schema=schema_text, task=args.task,
                                                                   profile=profile_ctx, history=history_text,
                                                                   question=extract_question(schema_text),
                                                                   personal=personal_block)
                            print("[i] survey form mode — AI fills ALL fields in one response")
                        else:
                            prompt_round = SCHEMA_INSTRUCTION.format(schema=schema_text, task=args.task,
                                                                     profile=profile_ctx, history=history_text)
                        if no_progress:
                            note = ("\n\nNOTE: I did what you suggested (" + last_cmd +
                                    ") but the page looks identical — nothing happened.")
                            errs = extract_errors(schema_text)
                            if errs:
                                note += "\nThe page shows an ERROR/WARNING: " + " | ".join(errs)
                                note += ("\nCheck the HTML carefully to understand the field/button "
                                         "properties and answer properly.")
                            note += "\nDo NOT repeat that action. Suggest a DIFFERENT action."
                            prompt_round += note
                        attach = False
                        print("[i] mode: schema (page structure text — no image, no rate limit)")
                    else:
                        prompt_round = prompt
                        if no_progress:
                            prompt_round = (prompt + "\n\nNOTE: I did what you suggested (" + last_cmd +
                                            ") but the page looks identical — nothing happened. "
                                            "Do NOT repeat that action. Suggest a DIFFERENT button or approach.")
                        attach = True
                        print("[i] mode: image (screenshot)")
                    prompt_round = prompt_round + f"\n{marker}"
                    for provider in providers:
                        reply = ask_provider(ai_page, provider, shot if attach else None,
                                             args.task, prompt_round, attach=attach, marker=marker)
                        if reply and reply.strip():
                            print(f"[{provider}] {reply!r}")
                            cmds = parse_commands(reply, exclude=prompt_round)
                            if cmds:
                                url_before = task_page.url
                                executed_done = False
                                for cmd in cmds:
                                    if cmd.split(None, 1)[0].lower() == "done":
                                        executed_done = True
                                        print("[cmd] done (AI finished its series — verifying task completion)")
                                        continue
                                    print(f"[cmd] {cmd}")
                                    try:
                                        cont = execute_command(task_page, cmd)
                                    except Exception as e:
                                        print(f"[!] command raised: {e} — continuing")
                                        task_page.screenshot(path=str(SHOTS / f"debug_exec_{step}.png"))
                                        cont = True
                                        remember(f"round {step}: {cmd} FAILED")
                                    else:
                                        remember(f"round {step}: {cmd}")
                                    if not cont:
                                        executed_done = True
                                        break
                                # let redirects/navigations settle before the next round
                                try:
                                    task_page.wait_for_load_state("domcontentloaded", timeout=15000)
                                except Exception:
                                    pass
                                # if any action opened a new tab (target=_blank), follow it
                                new_pages = [pg for pg in ctx.pages if pg not in (ai_page, task_page)]
                                if new_pages:
                                    old = task_page
                                    task_page = new_pages[0]
                                    task_page.wait_for_load_state("domcontentloaded")
                                    try:
                                        old.close()
                                    except Exception:
                                        pass
                                    print("[i] action opened a new tab — now controlling it")
                                remember(f"round {step}: page -> {task_page.url}")
                                # 'done' only ends the run if the page really stopped changing
                                if executed_done:
                                    page_moved = task_page.url != url_before
                                    if not page_moved:
                                        # ignore 'done' while unanswered form fields remain
                                        try:
                                            chk_schema = _extract_schema_once(task_page)
                                            unfilled = [ln for ln in chk_schema.splitlines()
                                                        if ("input" in ln or "select" in ln or "textarea" in ln)
                                                        and " value='" not in ln
                                                        and "placeholder='" not in ln
                                                        and "submit" not in ln]
                                            if unfilled:
                                                print(f"[i] 'done' ignored — {len(unfilled)} unanswered field(s) remain")
                                                page_moved = True
                                        except Exception:
                                            pass
                                    if not page_moved:
                                        try:
                                            chk = SHOTS / f"done_check_{step}.png"
                                            task_page.screenshot(path=str(chk))
                                            page_moved = not images_similar(shot, chk, args.progress_threshold)
                                        except Exception:
                                            pass
                                    if page_moved:
                                        print("[i] page changed after 'done' — continuing the loop")
                                    else:
                                        print("\n[done] task complete (AI said done, page stable)")
                                        ctx.close()
                                        return
                                done_round = True
                                consecutive_fails = 0
                                last_cmd = cmds[-1]
                                break
                            else:
                                print(f"[!] {provider} replied but no command found — next provider")
                        else:
                            print(f"[!] {provider} returned nothing — next provider")
                    if done_round:
                        break
                if not done_round:
                    consecutive_fails += 1
                    if consecutive_fails == 1:
                        shot_dbg = SHOTS / f"debug_round_{step}.png"
                        ai_page.screenshot(path=str(shot_dbg))
                        print(f"[!] all providers failed — debug: {shot_dbg}")
                    if consecutive_fails >= 3 and last_cmd:
                        mq = re.search(r"'([^']*)'", last_cmd)
                        if mq:
                            print(f"[i] 3 failures in a row — OCR click fallback on '{mq.group(1)}'")
                            click_by_ocr(task_page, mq.group(1))
                            task_page.wait_for_timeout(1500)
                    print(f"[!] round {step} failed ({consecutive_fails} in a row) — "
                          "retrying forever; Ctrl+C to stop")
                task_page.wait_for_timeout(1500)
        except KeyboardInterrupt:
            print("\n[cancelled] stopped by user (Ctrl+C)")

        print("\n[finished] check the browser / screenshots/")


if __name__ == "__main__":
    main()
