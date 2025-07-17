Below are:

1. **Windows setup & run instructions (PowerShell + VS Code).**
2. **Function‑by‑function walkthrough of the script (what each chunk does and how it maps to your 8‑step flow).**

---

# 1. Windows Setup & Run Instructions

> All commands shown for **Windows PowerShell**. If you use `cmd.exe`, drop the `./` prefixes.

### A. Install Python (if not already)

Download latest 3.11+ from python.org. During install:

* ✅ Check **“Add Python to PATH”**.
* ✅ Install for “All Users” (recommended).

Verify:

```powershell
python --version
```

### B. Create project folder (already done)

You have: `C:\Users\Admin\Desktop\goethe_booking_bot`

### C. Create & activate virtual environment

From project folder:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

Prompt should show `(venv)` prefix.

### D. Upgrade pip & install requirements

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

> **If you see errors installing Playwright:**
>
> * Make sure `pip install playwright` succeeded.
> * Then install the browser binaries:
>
>   ```powershell
>   playwright install
>   ```
>
>   (Or: `python -m playwright install`.)

### E. Optional: verify Playwright imports

Run a quick smoke test:

```powershell
python - <<'EOF'
from playwright.async_api import async_playwright
print("Playwright imports OK")
EOF
```

### F. Copy & edit environment / CSV files (optional)

```powershell
copy .env.sample .env
copy students.csv.sample students.csv
```

Edit `.env` with your credentials/data (or edit the embedded constants in the script).
Edit `students.csv` for multi‑student runs.

### G. Run the bot (headed)

```powershell
python goethe_booking_bot.py --students students.csv --headed --log run.log
```

Aliases: `--students` or `--csv` are accepted. Headed mode shows a live browser window.

### H. Run headless (background)

```powershell
python goethe_booking_bot.py --students students.csv --headless --log run_headless.log
```

### I. Single student from env (ignore CSV)

```powershell
python goethe_booking_bot.py --env-only --headed --log run_single.log
```

### J. Use embedded creds only (ignore env)

```powershell
python goethe_booking_bot.py --env-only --ignore-env --headed
```

### K. Watch log in real‑time

In a 2nd PowerShell window:

```powershell
Get-Content .\run.log -Wait
```

---

# 2. Fixing Pylance “missing imports” in VS Code

Pylance flags appear because VS Code’s Python language server can’t find the installed packages in the interpreter you have selected.

**Fix:**

1. Open the **Command Palette** → *Python: Select Interpreter*.
2. Pick the interpreter from your **project venv** (path ends with `goethe_booking_bot\venv\Scripts\python.exe`).
3. Save file; Pylance should re‑index.
4. If warnings persist, ensure the packages are installed in that venv:

   ```powershell
   .\venv\Scripts\Activate.ps1
   pip install playwright python-dotenv typer rich pydantic
   ```
5. Reload VS Code window (`Developer: Reload Window`).

The “reportMissingImports” diagnostics will clear once the imports resolve in the active interpreter.

---

# 3. Script Walkthrough (chunk‑level)

Below I show *what each major function block does* and *which requirement step(s)* it serves.

---

## Global constants / config

* **EMBED\_* constants*\* – default credentials & personal data embedded in code (your requirement: “email and password should be embedded before running” + other details).
* **START\_URL** – canonical restart URL; every failure step navigates back here (“start over” rule).
* **MAX\_REFRESH\_MS** – upper bound (800 ms) for random reload jitter (matches your “0 to 800ms” requirement).
* **TXT\_* constants*\* – keywords we search for when advancing steps (case‑insensitive).

---

## Logging

* `_log()` writes timestamped lines to stdout *and* a log file (opened via `_init_log()`), per your request to “add logs to show where the bot might fail/passed and write to a file; create if missing.”
* Levels: INFO, PASS, FAIL, ERROR, RELOAD\_OK, RELOAD\_ERR (helps at a glance).

---

## Student dataclass

Holds the per‑student data you listed (email, password, phone, etc.).
Built from:

* CSV row (`Student.from_row`)
* Environment variables (`student_from_env`)
* Embedded constants (fallback).

---

## Alarm

* `_alarm_beep()` – cross‑platform; uses `winsound.Beep` on Windows when available, fallback bell char otherwise.
* `AlarmController` – background task that beeps for 2s every 60s until stopped (your 2‑sec alarm w/ 1‑min interval).
* `_install_doubleclick_stop()` – injects a script into the page that calls back into Python when the user double‑clicks anywhere. That stops the alarm & completes the script (your “double‑click stops alarm and script” requirement).

---

## Navigation helpers

* `short_wait()` – tiny async sleep between actions.
* `_rand_refresh_delay()` – pick 0‑0.8s delay before each reload (requirement).
* `_safe_click()` – scroll into view (best effort) then click.
* `_find_by_text()` – build regex & try `get_by_role(button)` then fallback `get_by_text`; case‑insensitive; used for “SELECT MODULES”, “CONTINUE”, etc.
* `_first_visible()` & `_try_click_text()` – wait for a visible candidate among variants; click when found.

---

## Privacy banner

* `_handle_privacy_popup()` – tries “Accept All”, fallback Deny/Settings. Logged PASS/INFO. Called after every fresh navigation/reload because the banner sometimes returns.

---

## Start / restart

* `_goto_start()` – navigates to START\_URL (your restart link) and then handles privacy banner. Every failure path calls this so the flow resets exactly as you specified.

---

## Waiting for booking to open

* `_find_enabled_exam_button()` – scans the exam table for any non‑disabled button (Goethe uses `<button disabled>` when booking not yet open; removes attr when open). Used as a fallback trigger if “SELECT MODULES” text isn’t present.
* `_poll_until_select_modules()` – the aggressive watch loop.
  **Logic:**

  1. Look for explicit “SELECT MODULES” text.
  2. If not, look for an enabled exam button.
  3. If neither found in \~5s, schedule reload after random 0‑0.8s.
  4. After reload logs `[RELOAD_OK]`, recheck privacy, loop.
     This exactly implements your: *“refresh the page randomly between 0 to 800ms, each refresh only after successful reload, until the 'SELECT MODULES' button appears. Click it.”*

---

## Step advance wrapper

* `_advance_or_restart(page, texts, email)` – tries to click any of the given text triggers (e.g., `["continue"]`). If success: PASS. If not found: FAIL + restart at START\_URL (your Step 2/3/6/7 restart rule).

---

## Login

* `_login(page, student)` – fill email/password into common selectors; click “Log in” synonyms; restart on failure (your Step 4).

---

## Personal details form

* `_fill_personal_form(page, student)` – brute‑force match of known partial field names (“first”, “sur”, “zip”, etc.) against `name`, `placeholder`, `aria-label` attributes across all `input,select,textarea` fields; fills accordingly. Then clicks Continue else restart (your Step 5).

---

## run\_booking() – the full flow

Runs the 8 steps in order:

1. Start & privacy.
2. Poll for “SELECT MODULES” (fast reload loop).
3. Click “CONTINUE” (step 2 rule).
4. Click “BOOK FOR MYSELF” (step 3 rule).
5. Login (step 4 rule).
6. Fill personal form → Continue (step 5 rule).
7. Continue (step 6 rule).
8. “ORDER, SUBJECT TO CHANGE” (step 7 rule).
9. Wait for confirmation text; start alarm; stop on double‑click; exit (step 8 rule).

Any failure triggers restart via `_goto_start()` per your instructions.

---

## Multi‑student orchestration

* `main_async()` launches browser once; loops students sequentially (fresh context each).
* `_run_for_student()` wraps each student’s context & page; ensures close & logging.
* `load_students_from_csv()` loads CSV; fallback dummy.
* `build_students()` chooses env/embedded/CSV logic per CLI flags.
* `_parse_args()` wires CLI:

  * `--csv / --students`
  * `--env`
  * `--env-only`
  * `--ignore-env`
  * `--headless` / `--headed`
  * `--log`

---

# 4. Cleaned `goethe_booking_bot.py`

Below is a cleaned version (no duplication; docstrings; minor formatting tidy; one place to edit embedded data). Copy‑paste to replace your current file.

> **NOTE:** This version is functionally equivalent to what you posted (the second copy), but tidied and annotated. I have *not* yet added the advanced “wait until 8 pm Kenya time” logic because you haven’t answered the targeting questions; this keeps current behavior (immediate fast polling). We can extend once you clarify date‑targeting preferences.

