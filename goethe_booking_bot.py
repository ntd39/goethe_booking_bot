#!/usr/bin/env python
"""
Goethe B2 booking watcher + auto-book flow.

Behavior (per user spec):
- Start at START_URL.
- Dismiss privacy popup if shown.
- Refresh randomly 0–800 ms (after each successful reload) until a
  "SELECT MODULES" button/link OR an enabled exam booking button appears.
- Click it to begin booking flow.
- Page sequence:
    1. (Start) Wait for SELECT MODULES; click.
    2. Find CONTINUE; else restart.
    3. Find BOOK FOR MYSELF; else restart.
    4. Fill login form (email/password); click Log in; else restart.
    5. Fill personal details; click CONTINUE; else restart.
    6. Find CONTINUE; else restart.
    7. Find "ORDER, SUBJECT TO CHANGE"; click; else restart.
    8. Wait for confirmation text
       "You will receive email confirmation of your booking."
       -> sound alarm 2s every 60s until user double-clicks page.
       -> stop alarm & exit.

Supports:
- Embedded fallback credentials (edit below).
- Optional .env overrides (python-dotenv).
- Optional multi-student CSV.
- Logging to stdout + file.
"""

import asyncio
import contextlib
import csv
import os
import pathlib
import platform
import random
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

# ---------------------------------------------------------------------------
# Optional dotenv support
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - fallback no-op
    def load_dotenv(*_args, **_kwargs):
        return False


# ---------------------------------------------------------------------------
# Embedded fallback credentials / data  *** EDIT THESE ***
# ---------------------------------------------------------------------------
EMBED_EMAIL = "dummy@example.com"
EMBED_PASSWORD = "DummyPass123!"
EMBED_PHONE = "+254700000000"
EMBED_FIRST_NAME = "Test"
EMBED_SURNAME = "User"
EMBED_COUNTY = "Nairobi"
EMBED_DOB = "2000-01-01"  # YYYY-MM-DD
EMBED_PLACE_OF_BIRTH = "Nairobi"
EMBED_ZIP = "00100"
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Alarm (winsound on Windows, bell fallback elsewhere)
# ---------------------------------------------------------------------------
if platform.system().lower().startswith("win"):
    try:
        import winsound  # type: ignore

        def _alarm_beep(freq: int, duration_ms: int) -> None:
            try:
                winsound.Beep(freq, duration_ms)
            except Exception:
                sys.stderr.write("[alarm] winsound.Beep failed\n")
    except Exception:  # winsound import failure
        def _alarm_beep(freq: int, duration_ms: int) -> None:
            sys.stdout.write("\a")
            sys.stdout.flush()
            time.sleep(duration_ms / 1000)
else:
    def _alarm_beep(freq: int, duration_ms: int) -> None:
        sys.stdout.write("\a")
        sys.stdout.flush()
        time.sleep(duration_ms / 1000)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
START_URL = "https://www.goethe.de/ins/ke/en/spr/prf/gzb2.cfm"
MAX_REFRESH_MS = 800
STEP_WAIT_MS = 200
DEFAULT_TIMEOUT = 5_000
ALARM_BEEP_FREQ = 950
ALARM_BEEP_DURATION_MS = 2_000
ALARM_REPEAT_SEC = 60

# Step trigger texts (case-insensitive)
TXT_SELECT_MODULES = "select modules"
TXT_CONTINUE = "continue"
TXT_BOOK_FOR_MYSELF = "book for myself"
TXT_LOGIN = "log in"
TXT_ORDER_SUBJECT_TO_CHANGE = "order, subject to change"
TXT_CONFIRMATION = "You will receive email confirmation of your booking."
# Privacy banner words
TXT_PRIVACY_ACCEPT = "accept all"
TXT_PRIVACY_DENY = "deny"
TXT_PRIVACY_SETTINGS = "settings"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_LOG_F = None  # file handle

def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

def _log(level: str, msg: str, email: str = ""):
    """Write a log line to stdout and the active log file."""
    line = f"{_ts()} [{level}] {email} {msg}".rstrip() + "\n"
    sys.stdout.write(line)
    if _LOG_F:
        try:
            _LOG_F.write(line)
            _LOG_F.flush()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Student:
    email: str
    password: str
    phone: str
    first_name: str
    surname: str
    county: str
    dob: str
    place_of_birth: str
    zip_code: str

    @classmethod
    def from_row(cls, row: Dict[str, str]) -> "Student":
        return cls(
            email=row.get("email", ""),
            password=row.get("password", ""),
            phone=row.get("phone", ""),
            first_name=row.get("first_name", ""),
            surname=row.get("surname", ""),
            county=row.get("county", ""),
            dob=row.get("dob", ""),
            place_of_birth=row.get("place_of_birth", ""),
            zip_code=row.get("zip_code", ""),
        )

DUMMY_STUDENT = Student(
    email=EMBED_EMAIL,
    password=EMBED_PASSWORD,
    phone=EMBED_PHONE,
    first_name=EMBED_FIRST_NAME,
    surname=EMBED_SURNAME,
    county=EMBED_COUNTY,
    dob=EMBED_DOB,
    place_of_birth=EMBED_PLACE_OF_BIRTH,
    zip_code=EMBED_ZIP,
)


# ---------------------------------------------------------------------------
# Small async helpers
# ---------------------------------------------------------------------------
async def short_wait(ms: int = STEP_WAIT_MS):
    await asyncio.sleep(ms / 1000)

def _rand_refresh_delay() -> float:
    return random.uniform(0, MAX_REFRESH_MS) / 1000

async def _safe_click(el):
    with contextlib.suppress(Exception):
        await el.scroll_into_view_if_needed()
    await el.click()

async def _find_by_text(page: Page, text: str):
    pattern = re.compile(re.escape(text.strip()), re.I)
    try:
        return page.get_by_role("button", name=pattern)
    except Exception:
        return page.get_by_text(pattern)

async def _first_visible(page: Page, candidates: List[Any]):
    for c in candidates:
        if c is None:
            continue
        try:
            await c.wait_for(state="visible", timeout=DEFAULT_TIMEOUT)
            return c
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    return None

async def _try_click_text(page: Page, *texts: str) -> bool:
    cands = [await _find_by_text(page, t) for t in texts if t]
    el = await _first_visible(page, cands)
    if el is None:
        return False
    await _safe_click(el)
    await short_wait()
    return True


# ---------------------------------------------------------------------------
# Privacy banner
# ---------------------------------------------------------------------------
async def _handle_privacy_popup(page: Page, email: str):
    _log("INFO", "checking privacy popup", email)
    try:
        if await _try_click_text(page, TXT_PRIVACY_ACCEPT, "accept", "accept all"):
            _log("PASS", "privacy accepted", email)
            await short_wait()
            return
        if await _try_click_text(page, TXT_PRIVACY_DENY, "deny"):
            _log("PASS", "privacy denied fallback", email)
            await short_wait()
            return
        if await _try_click_text(page, TXT_PRIVACY_SETTINGS, "settings"):
            _log("INFO", "privacy settings clicked", email)
            await short_wait()
            return
    except Exception as e:
        _log("ERROR", f"privacy popup handling error {e}", email)


# ---------------------------------------------------------------------------
# Start / restart
# ---------------------------------------------------------------------------
async def _goto_start(page: Page, email: str):
    _log("INFO", f"navigating start {START_URL}", email)
    await page.goto(START_URL, wait_until="domcontentloaded")
    await short_wait()
    await _handle_privacy_popup(page, email)


# ---------------------------------------------------------------------------
# Booking open detection
# ---------------------------------------------------------------------------
async def _find_enabled_exam_button(page: Page) -> Optional[Any]:
    """Return the first enabled .pr-buttons button (not disabled) if any."""
    buttons = await page.query_selector_all(".pr-buttons button")
    for b in buttons:
        try:
            if await b.get_attribute("disabled") is None:
                return b
        except Exception:
            continue
    return None

async def _poll_until_select_modules(page: Page, email: str):
    """Aggressive 0–800 ms reload loop until booking becomes possible."""
    _log("INFO", "polling for select modules", email)
    while True:
        # 1. explicit SELECT MODULES text
        try:
            el = await _find_by_text(page, TXT_SELECT_MODULES)
            await el.wait_for(state="visible", timeout=DEFAULT_TIMEOUT)
            await _safe_click(el)
            await short_wait()
            _log("PASS", "select modules clicked", email)
            return
        except PlaywrightTimeoutError:
            pass
        except Exception as e:
            _log("ERROR", f"select modules error {e}", email)

        # 2. enabled exam button fallback
        try:
            exam_btn = await _find_enabled_exam_button(page)
            if exam_btn:
                _log("INFO", "enabled exam button found fallback", email)
                await _safe_click(exam_btn)
                await short_wait()
                _log("PASS", "exam button clicked (treated as select modules)", email)
                return
        except Exception as e:
            _log("ERROR", f"exam button scan error {e}", email)

        # 3. reload
        delay = _rand_refresh_delay()
        _log("INFO", f"select modules not visible; reloading after {delay:.3f}s", email)
        await asyncio.sleep(delay)
        try:
            await page.reload(wait_until="domcontentloaded")
            _log("RELOAD_OK", "page reloaded", email)
        except Exception as e:
            _log("RELOAD_ERR", f"reload error {e}", email)
            continue
        await short_wait()
        await _handle_privacy_popup(page, email)


# ---------------------------------------------------------------------------
# Step advance wrapper
# ---------------------------------------------------------------------------
async def _advance_or_restart(page: Page, texts: List[str], email: str) -> bool:
    _log("INFO", f"advancing via {texts}", email)
    ok = await _try_click_text(page, *texts)
    if ok:
        _log("PASS", f"clicked {texts}", email)
        return True
    _log("FAIL", f"did not find {texts}, restart", email)
    await _goto_start(page, email)
    return False


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
async def _login(page: Page, student: Student) -> bool:
    email = student.email
    _log("INFO", "login step", email)
    selectors = ["input[type=email]", "#email", "input[name=email]"]
    pw_selectors = ["input[type=password]", "#password", "input[name=password]"]
    filled = False

    for sel in selectors:
        try:
            f = await page.query_selector(sel)
            if f:
                await f.fill(student.email)
                filled = True
                _log("INFO", f"filled email {sel}", email)
                break
        except Exception as e:
            _log("ERROR", f"email fill {sel} {e}", email)

    for sel in pw_selectors:
        try:
            f = await page.query_selector(sel)
            if f:
                await f.fill(student.password)
                _log("INFO", f"filled password {sel}", email)
                break
        except Exception as e:
            _log("ERROR", f"password fill {sel} {e}", email)

    if filled and await _try_click_text(page, TXT_LOGIN, "login", "sign in", "log-in"):
        _log("PASS", "login submit clicked", email)
        await short_wait()
        return True

    _log("FAIL", "login step failed restart", email)
    await _goto_start(page, email)
    return False


# ---------------------------------------------------------------------------
# Personal details
# ---------------------------------------------------------------------------
async def _fill_personal_form(page: Page, student: Student) -> bool:
    email = student.email
    _log("INFO", "personal form step", email)

    mapping = [
        ("phone", student.phone),
        ("first", student.first_name),
        ("given", student.first_name),
        ("sur", student.surname),
        ("last", student.surname),
        ("county", student.county),
        ("birth", student.place_of_birth),
        ("zip", student.zip_code),
        ("post", student.zip_code),
        ("date", student.dob),
        ("dob", student.dob),
    ]
    lower_map = [(k.lower(), v) for k, v in mapping if v]

    inputs = await page.query_selector_all("input,select,textarea")
    for inp in inputs:
        try:
            name = (await inp.get_attribute("name") or "").lower()
            placeholder = (await inp.get_attribute("placeholder") or "").lower()
            aria = (await inp.get_attribute("aria-label") or "").lower()
            match_txt = f"{name} {placeholder} {aria}"
            for key, val in lower_map:
                if key in match_txt:
                    tag = await inp.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "select":
                        await inp.select_option(label=val)
                    else:
                        await inp.fill(val)
                    _log("INFO", f"filled field {key}", email)
                    break
        except Exception as e:
            _log("ERROR", f"field fill error {e}", email)

    if await _try_click_text(page, TXT_CONTINUE):
        _log("PASS", "personal form continue", email)
        return True

    _log("FAIL", "personal form continue missing restart", email)
    await _goto_start(page, email)
    return False


# ---------------------------------------------------------------------------
# Alarm controller
# ---------------------------------------------------------------------------
class AlarmController:
    def __init__(self):
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    def stop(self):
        self._stop.set()

    async def _beep_once(self):
        await asyncio.to_thread(_alarm_beep, ALARM_BEEP_FREQ, ALARM_BEEP_DURATION_MS)

    async def _run(self):
        while not self._stop.is_set():
            await self._beep_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=ALARM_REPEAT_SEC)
            except asyncio.TimeoutError:
                continue

    def start(self):
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def wait_stopped(self):
        if self._task:
            await self._task


async def _install_doubleclick_stop(page: Page, alarm: AlarmController, email: str):
    async def _stop_alarm_binding(_source, *_args):
        _log("INFO", "double-click stop alarm", email)
        alarm.stop()

    await page.expose_binding("__goetheStopAlarm", _stop_alarm_binding)
    await page.add_init_script("""
        (() => {
          document.addEventListener('dblclick', () => {
            if (window.__goetheStopAlarm) {
              window.__goetheStopAlarm();
            }
          }, { once: true });
        })();
    """)


# ---------------------------------------------------------------------------
# Main booking flow (per student)
# ---------------------------------------------------------------------------
async def run_booking(page: Page, student: Student) -> bool:
    email = student.email

    await _goto_start(page, email)
    await _poll_until_select_modules(page, email)

    if not await _advance_or_restart(page, [TXT_CONTINUE], email):
        return False
    if not await _advance_or_restart(page, [TXT_BOOK_FOR_MYSELF], email):
        return False
    if not await _login(page, student):
        return False
    if not await _fill_personal_form(page, student):
        return False
    if not await _advance_or_restart(page, [TXT_CONTINUE], email):
        return False
    if not await _advance_or_restart(page, [TXT_ORDER_SUBJECT_TO_CHANGE], email):
        return False

    # Confirmation page
    try:
        conf = await _find_by_text(page, TXT_CONFIRMATION)
        await conf.wait_for(state="visible", timeout=30_000)
        _log("PASS", "confirmation text visible", email)
    except Exception as e:
        _log("FAIL", f"confirmation not visible {e}", email)
        await _goto_start(page, email)
        return False

    # Alarm until double-click
    alarm = AlarmController()
    await _install_doubleclick_stop(page, alarm, email)
    alarm.start()
    _log("INFO", "alarm started waiting for double-click", email)
    await alarm.wait_stopped()
    _log("PASS", "alarm stopped", email)
    return True


# ---------------------------------------------------------------------------
# Browser orchestration
# ---------------------------------------------------------------------------
async def _new_context(browser: Browser) -> BrowserContext:
    return await browser.new_context(viewport={"width": 1280, "height": 800})

async def _run_for_student(browser: Browser, headed: bool, student: Student) -> bool:
    email = student.email
    _log("INFO", "new context", email)
    ctx = await _new_context(browser)
    page = await ctx.new_page()
    ok = False
    try:
        ok = await run_booking(page, student)
    finally:
        await ctx.close()
        _log("INFO", "context closed", email)
    return ok

async def main_async(headless: bool, students: List[Student]):
    _log("INFO", f"launching browser headless={headless}", "")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        results = []
        for s in students:
            _log("INFO", "begin student", s.email)
            ok = await _run_for_student(browser, not headless, s)
            results.append((s.email, ok))
            _log("INFO", f"end student success={ok}", s.email)
        await browser.close()
    _log("INFO", "browser closed", "")
    return results


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_students_from_csv(path: Optional[str]) -> List[Student]:
    if not path:
        return [DUMMY_STUDENT]
    students: List[Student] = []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                students.append(Student.from_row(row))
    except Exception as e:
        _log("ERROR", f"csv read error {e}", "")
    return students or [DUMMY_STUDENT]

def load_env(env_path: Optional[str]):
    if env_path and os.path.exists(env_path):
        load_dotenv(env_path)
    else:
        load_dotenv()

def student_from_env() -> Student:
    return Student(
        email=os.getenv("GOETHE_EMAIL", EMBED_EMAIL),
        password=os.getenv("GOETHE_PASSWORD", EMBED_PASSWORD),
        phone=os.getenv("GOETHE_PHONE", EMBED_PHONE),
        first_name=os.getenv("GOETHE_FIRST_NAME", EMBED_FIRST_NAME),
        surname=os.getenv("GOETHE_SURNAME", EMBED_SURNAME),
        county=os.getenv("GOETHE_COUNTY", EMBED_COUNTY),
        dob=os.getenv("GOETHE_DOB", EMBED_DOB),
        place_of_birth=os.getenv("GOETHE_PLACE_OF_BIRTH", EMBED_PLACE_OF_BIRTH),
        zip_code=os.getenv("GOETHE_ZIP", EMBED_ZIP),
    )

def build_students(ignore_env: bool, env_only: bool, csv_path: Optional[str]) -> List[Student]:
    if ignore_env:
        return [DUMMY_STUDENT] if env_only else load_students_from_csv(csv_path)
    return [student_from_env()] if env_only else load_students_from_csv(csv_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args(argv: List[str]):
    import argparse
    p = argparse.ArgumentParser(prog="goethe-booker")
    p.add_argument("--csv", "--students", dest="csv", help="CSV file with student rows")
    p.add_argument("--env", help=".env path")
    p.add_argument(
        "--env-only",
        action="store_true",
        help="Use env (or embedded) single student and ignore CSV",
    )
    p.add_argument(
        "--ignore-env",
        action="store_true",
        help="Do not read env vars; use embedded values",
    )
    p.add_argument("--headless", action="store_true", help="Run browser headless")
    p.add_argument("--headed", action="store_true", help="Force headed mode")
    p.add_argument("--log", help="log file path", default="goethe_booking_bot.log")
    args = p.parse_args(argv)
    headed = not args.headless
    if args.headed:
        headed = True
    return args, headed

def _init_log(path: str):
    global _LOG_F
    try:
        p = pathlib.Path(path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        
        # Check if log file exists and clean it before creating new one
        if p.exists():
            p.unlink()  # Remove the existing file
        
        _LOG_F = open(p, "w", encoding="utf-8")
    except Exception:
        _LOG_F = None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None):
    if argv is None:
        argv = sys.argv[1:]
    args, headed = _parse_args(argv)
    _init_log(args.log)
    load_env(args.env)
    students = build_students(args.ignore_env, args.env_only, args.csv)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    results = loop.run_until_complete(main_async(headless=not headed, students=students))
    for email, ok in results:
        print(f"{email}: {'SUCCESS' if ok else 'FAILED'}")
    if _LOG_F:
        _LOG_F.close()

if __name__ == "__main__":
    main()
