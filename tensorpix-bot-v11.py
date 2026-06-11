#!/usr/bin/env python3
"""
TensorPix Bot v11 - Automated video upscaling via TensorPix REST API
  - CroxyProxy-based account creation (Playwright) with email verification
  - Boomlify temp emails (@usa.priyo.edu.pl) via v1.boomlify.com
  - API registration + CroxyProxy web signup for auto-verification
  - REST API: upload video, create job, poll CDN link
  - --links-only mode: collect CDN URLs without downloading
  - Forced 2160p output via output_resolution=3840
  - Animation-aware model selection (no "manual tab" issues via API)
  - Fallback model chain: 4x -> 2x -> 1x based on balance
  - Resumable: saves state to bot_state.json

Changes from v7:
  - Fixed Boomlify endpoint: POST https://v1.boomlify.com/emails/create (was wrong host+path)
  - Fixed Playwright page.evaluate() 3-arg bug (must use 2-arg with array wrapper)
  - Added output_resolution=3840 to force 2160p (4K) output on every job
  - Added API registration before CroxyProxy (speeds things up)
  - Added animation model support (model IDs for anime/animation content)
  - Added --model-preset flag: '4k' (default), 'anime-4k', '2k', 'anime-2k'
  - No "manual tab" issue — API handles everything, no frontend routing
  - Suppressed SSL warnings properly with urllib3
"""

import argparse
import json
import os
import random
import string
import subprocess
import sys
import time
from pathlib import Path

import requests
import urllib3

# Suppress InsecureRequestWarning for Boomlify SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
BOOMLIFY_API_KEY = "api_12dd62b98e58eb5638cc90f7db8d5dcd3d8d8140bf3b68833d6a95a73c7dc033"
EMAIL_DOMAIN = "@usa.priyo.edu.pl"
PASSWORD = "TpixAcc2026!"
BASE_URL = "https://backend.tensorpix.ai"
CROXYPROXY_URL = "https://www.croxyproxy.com"
TENSORPIX_SIGNUP = "https://app.tensorpix.ai/auth/sign-up"

# ML Model IDs (discovered from TensorPix REST API)
# Super Resolution models:
ML_MODEL_4X_ULTRA = 44    # "4x Upscale Ultra 4" — cost 2.0, max_input=1280x720, output up to 3840x2160
ML_MODEL_2X = 45          # "2x Upscale 4"       — cost 0.5, max_input=1920x1080, output up to 2560x1440
ML_MODEL_4X = 46          # "4x Upscale"          — cost ~1.5
ML_MODEL_2X_ANIME = 47    # "2x Anime Upscale"   — optimized for animation
ML_MODEL_4X_ANIME = 48    # "4x Anime Upscale"   — optimized for animation (higher quality)
# Note: exact anime model IDs may vary; the bot will try them in order.
# If a model ID doesn't exist on the API, it'll fail gracefully and try the next.

# Model presets: (primary_models[], fallback_models[], description)
MODEL_PRESETS = {
    "4k": {
        "description": "4K Ultra upscale (default, best quality for live-action)",
        "models": [ML_MODEL_4X_ULTRA],
        "fallback": [ML_MODEL_2X],
    },
    "anime-4k": {
        "description": "4K Anime upscale (optimized for animation/animated content)",
        "models": [ML_MODEL_4X_ANIME, ML_MODEL_4X_ULTRA],
        "fallback": [ML_MODEL_2X_ANIME, ML_MODEL_2X],
    },
    "2k": {
        "description": "2K upscale (faster, cheaper)",
        "models": [ML_MODEL_2X],
        "fallback": [ML_MODEL_2X],
    },
    "anime-2k": {
        "description": "2K Anime upscale (animation, faster)",
        "models": [ML_MODEL_2X_ANIME, ML_MODEL_2X],
        "fallback": [ML_MODEL_2X],
    },
}

DEFAULT_PRESET = "4k"
OUTPUT_FILE = "cdn_links.txt"
STATE_FILE = "bot_state.json"

# JavaScript for filling React controlled inputs (2-arg version for Playwright)
FILL_INPUT_JS = """([el, val]) => {
    const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value'
    ).set;
    setter.call(el, val);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
}"""


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
def log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"processed": {}, "cdn_links": {}}


def rand_username(length=8):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


# ──────────────────────────────────────────────
# 1. Boomlify temp email
# ──────────────────────────────────────────────
def generate_temp_email(api_key=None):
    """
    Create a temp email via Boomlify REST API.
    Correct endpoint: POST https://v1.boomlify.com/emails/create
    Returns 403/401 but STILL creates the mailbox (confirmed working).
    Returns email string or None.
    """
    key = api_key or BOOMLIFY_API_KEY
    username = rand_username()
    email = f"{username}{EMAIL_DOMAIN}"

    if key:
        try:
            resp = requests.post(
                "https://v1.boomlify.com/emails/create",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json={"address": email},
                timeout=20,
                verify=False,
            )
            # Endpoint returns 403/401 but still creates the mailbox.
            # Check for "encrypted" key in response body to confirm.
            try:
                body = resp.json()
                if "encrypted" in body or resp.status_code in (200, 201):
                    log(f"  Temp email created: {email}")
                    return email
            except Exception:
                pass
            # Any non-connection error response means the API processed it
            if resp.status_code in (200, 201, 400, 401, 403):
                log(f"  Temp email created (status {resp.status_code}): {email}")
                return email
        except Exception as e:
            log(f"  Boomlify API error: {e}")

    return None


# ──────────────────────────────────────────────
# 2. TensorPix account creation
# ──────────────────────────────────────────────
def create_tensorpix_account(email, password=PASSWORD):
    """
    Create and verify a TensorPix account:
      1. Register via API (POST /api/accounts/register/)
      2. Complete signup via CroxyProxy web UI (triggers email verification / auto-activates)
      3. Verify login works
    Returns True on success.
    """
    # ── Step 1: API Registration ──────────────────────────
    log(f"  [register] Creating account via API: {email}")
    try:
        resp = requests.post(
            f"{BASE_URL}/api/accounts/register/",
            json={"email": email, "password": password, "password_confirm": password},
            timeout=30,
        )
        if resp.status_code == 201:
            log(f"  [register] API registration OK (201)")
        elif resp.status_code == 400:
            # Email might already exist — that's fine, try CroxyProxy anyway
            log(f"  [register] Email may already exist (400), continuing...")
        else:
            log(f"  [register] API registration returned {resp.status_code}: {resp.text[:150]}")
    except Exception as e:
        log(f"  [register] API registration error: {e}")

    # ── Step 2: CroxyProxy web signup (auto-verification) ─
    log(f"  [croxy] Starting CroxyProxy signup flow...")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("  [FATAL] Playwright not installed. Run:")
        log("    pip install playwright && playwright install chromium")
        return False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()

        try:
            # A: Open CroxyProxy
            log("  [croxy] Opening CroxyProxy...")
            page.goto(CROXYPROXY_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            # B: Enter TensorPix signup URL in proxy bar
            log("  [croxy] Entering TensorPix URL...")
            url_bar = _find_input(page, [
                'input[name="url"]', 'input[type="text"]',
                'input[placeholder*="URL" i]', 'input[placeholder*="url" i]',
                '#url', '#address', 'input.url-bar', 'input.text-input',
            ])
            if url_bar:
                _fill_input(page, url_bar, TENSORPIX_SIGNUP)
                page.wait_for_timeout(500)
                url_bar.press("Enter")
            else:
                page.goto(
                    f"{CROXYPROXY_URL}/?url={TENSORPIX_SIGNUP}",
                    wait_until="domcontentloaded", timeout=60000,
                )

            # C: Wait for TensorPix signup form to load through proxy
            log("  [croxy] Waiting for TensorPix page...")
            for _ in range(15):
                page.wait_for_timeout(2000)
                try:
                    page.wait_for_selector(
                        "input[type='email'], input[name='email'], input[placeholder*='email' i]",
                        timeout=5000,
                    )
                    break
                except Exception:
                    pass

            # D: Fill email field
            log("  [croxy] Filling signup form...")
            email_input = _find_input(page, [
                'input[type="email"]', 'input[name="email"]',
                'input[placeholder*="email" i]', '#email',
            ])
            if email_input:
                _fill_input(page, email_input, email)
                log(f"  [croxy] Email filled: {email}")

            # E: Fill password field
            password_input = _find_input(page, [
                'input[type="password"]', 'input[name="password"]',
                'input[placeholder*="password" i]', '#password',
            ])
            if password_input:
                _fill_input(page, password_input, password)
                log("  [croxy] Password filled")

            # F: Check terms checkbox
            try:
                cb = page.wait_for_selector('input[type="checkbox"]', timeout=3000)
                if cb and not cb.is_checked():
                    cb.check()
                    log("  [croxy] Terms checkbox checked")
            except Exception:
                log("  [croxy] No terms checkbox (skipping)")

            # G: Click submit button
            log("  [croxy] Submitting signup...")
            clicked = _click_button(page, [
                'button[type="submit"]',
                'button:has-text("Sign Up")',
                'button:has-text("Sign up")',
                'button:has-text("Create Account")',
                'button:has-text("Register")',
                'button:has-text("Get Started")',
                'input[type="submit"]',
            ])
            if not clicked:
                log("  [croxy] WARNING: could not find submit button")

            # H: Wait for account creation / verification redirect
            log("  [croxy] Waiting for account verification...")
            page.wait_for_timeout(15000)

            safe_name = email.split("@")[0]
            try:
                page.screenshot(path=f"debug_signup_{safe_name}.png")
            except Exception:
                pass

        except Exception as e:
            log(f"  [croxy] Error: {e}")
            try:
                page.screenshot(path=f"debug_error_{email.split('@')[0]}.png")
            except Exception:
                pass
        finally:
            browser.close()

    # ── Step 3: Verify login works ────────────────────────
    log("  [api] Verifying account login...")
    resp = requests.post(
        f"{BASE_URL}/api/token/",
        json={"email": email, "password": password},
        timeout=30,
    )
    if resp.status_code == 200:
        log(f"  [api] Account VERIFIED: {email}")
        return True
    else:
        log(f"  [api] Login FAILED: {resp.status_code} — {resp.text[:100]}")
        return False


def _find_input(page, selectors):
    """Find the first visible input matching any of the selectors."""
    for sel in selectors:
        try:
            el = page.wait_for_selector(sel, timeout=4000)
            if el and el.is_visible():
                return el
        except Exception:
            continue
    # Last resort: any visible text input
    for inp in page.query_selector_all("input"):
        try:
            if inp.is_visible():
                return inp
        except Exception:
            continue
    return None


def _fill_input(page, element, value):
    """Fill a React controlled input using nativeInputValueSetter trick.
    Uses 2-arg page.evaluate (array wrapper) for Playwright Python compatibility."""
    page.evaluate(FILL_INPUT_JS, [element, value])


def _click_button(page, selectors):
    """Click the first visible button matching any selector."""
    for sel in selectors:
        try:
            btn = page.wait_for_selector(sel, timeout=3000)
            if btn and btn.is_visible():
                btn.click()
                return True
        except Exception:
            continue
    return False


# ──────────────────────────────────────────────
# 3. TensorPix REST API helpers
# ──────────────────────────────────────────────
def tp_login(email, password=PASSWORD):
    """Login and return JWT access token, or None."""
    resp = requests.post(
        f"{BASE_URL}/api/token/",
        json={"email": email, "password": password},
        timeout=30,
    )
    if resp.status_code == 200:
        return resp.json()["access"]
    return None


def tp_balance(email):
    """Return account USD balance or None."""
    token = tp_login(email)
    if not token:
        return None
    try:
        resp = requests.get(
            f"{BASE_URL}/api/accounts/profile/balance/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if resp.status_code == 200:
            return float(resp.json().get("balance_usd", "0"))
    except Exception:
        pass
    return None


def tp_upload_video(token, file_path):
    """Upload video file. Returns video_id (int) or None."""
    fname = os.path.basename(file_path)
    log(f"  [api] Uploading {fname} ({os.path.getsize(file_path)/1024/1024:.1f} MB)...")
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                f"{BASE_URL}/api/videos/",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": (fname, f, "video/mp4")},
                timeout=600,
            )
        if resp.status_code == 201:
            vid_id = resp.json()["id"]
            log(f"  [api] Upload OK  video_id={vid_id}")
            return vid_id
        log(f"  [api] Upload FAIL  HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        log(f"  [api] Upload error: {e}")
    return None


def tp_wait_video_ready(email, video_id, timeout=300):
    """Poll GET /api/videos/{id}/ until 'file' field is non-null."""
    log(f"  [api] Waiting for video {video_id} to finish uploading...")
    start = time.time()
    while time.time() - start < timeout:
        token = tp_login(email)
        if not token:
            time.sleep(10)
            continue
        try:
            resp = requests.get(
                f"{BASE_URL}/api/videos/{video_id}/",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            if resp.status_code == 200:
                v = resp.json()
                if v.get("file"):
                    log(f"  [api] Video {video_id} READY")
                    return True
        except Exception:
            pass
        time.sleep(10)
    log(f"  [api] TIMEOUT waiting for video {video_id}")
    return False


def tp_create_job(token, video_id, ml_model_id, output_resolution=3840):
    """Create enhancement job with forced output resolution.
    Returns job_id (int), "NO_CREDITS", or None.
    output_resolution=3840 forces 4K (2160p) output.
    """
    log(f"  [api] Creating job  model={ml_model_id}  video={video_id}  output_res={output_resolution}")
    try:
        payload = {
            "input_video": video_id,
            "ml_models": [ml_model_id],
            "output_resolution": output_resolution,
        }
        resp = requests.post(
            f"{BASE_URL}/api/jobs/",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )
        if resp.status_code in (200, 201):
            job_id = resp.json()["id"]
            log(f"  [api] Job created  job_id={job_id}")
            return job_id
        error_msg = resp.text[:300]
        log(f"  [api] Job FAIL  HTTP {resp.status_code}: {error_msg}")
        if "credits" in error_msg.lower() or "top up" in error_msg.lower() or "insufficient" in error_msg.lower():
            return "NO_CREDITS"
    except Exception as e:
        log(f"  [api] Job creation error: {e}")
    return None


def tp_poll_cdn(email, timeout=25 * 60):
    """Poll /api/restored-videos/ for a CDN URL. Returns URL str or None."""
    start = time.time()
    while time.time() - start < timeout:
        token = tp_login(email)
        if not token:
            time.sleep(30)
            continue
        try:
            resp = requests.get(
                f"{BASE_URL}/api/restored-videos/",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                vids = data.get("results", []) if isinstance(data, dict) else data
                for v in vids or []:
                    if isinstance(v, dict):
                        for key in ("file", "cdn_url", "download_url"):
                            val = v.get(key)
                            if val and "cloudflarestorage" in str(val):
                                return val
                elapsed = int(time.time() - start)
                log(f"  [api] [{elapsed//60}m{elapsed%60:02d}s] processing... "
                    f"({len(vids or [])} videos)")
        except Exception:
            pass
        time.sleep(45)
    return None


def tp_check_job_status(email, job_id):
    """Return job status integer or None.
    0=Queue, 1=Processing, 2=Finished, -1=Failed, -2=Canceled"""
    token = tp_login(email)
    if not token:
        return None
    try:
        resp = requests.get(
            f"{BASE_URL}/api/jobs/{job_id}/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json().get("status")
    except Exception:
        pass
    return None


# ──────────────────────────────────────────────
# 4. ffmpeg helpers
# ──────────────────────────────────────────────
def get_video_duration(path):
    """Return video duration in seconds."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def split_video(input_file, num_segments, seg_dir="segments"):
    """Split input into num_segments pieces using ffmpeg copy mode."""
    os.makedirs(seg_dir, exist_ok=True)
    duration = get_video_duration(input_file)
    seg_duration = duration / num_segments

    log(f"  [ffmpeg] Duration={duration:.1f}s  seg_duration={seg_duration:.1f}s  "
        f"segments={num_segments}")

    cmd = [
        "ffmpeg", "-y", "-i", input_file,
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(seg_duration),
        "-reset_timestamps", "1",
        os.path.join(seg_dir, "segment_%03d.mp4"),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        log(f"  [ffmpeg] ERROR: {result.stderr[-400:]}")
        return []

    segments = sorted(str(p) for p in Path(seg_dir).glob("segment_*.mp4"))
    log(f"  [ffmpeg] Created {len(segments)} segment files")
    return segments


# ──────────────────────────────────────────────
# 5. Main pipeline
# ──────────────────────────────────────────────
def process_segment(seg_num, seg_file, model_preset, output_resolution, state, boomlify_key=None):
    """
    Full pipeline for one segment:
      create email -> register via API -> verify via CroxyProxy -> upload -> create job -> poll CDN
    Returns (cdn_url | None, updated_state_entry)
    """
    # ── 5a. Create temp email ──────────────────────────────
    email = None
    for attempt in range(3):
        email = generate_temp_email(api_key=boomlify_key)
        if email:
            break
        log(f"  Email gen failed, retry {attempt+1}/3...")
        time.sleep(5)

    if not email:
        log(f"  FATAL: could not generate temp email")
        return None, {"status": "failed_email", "email": None}

    # ── 5b. Create TensorPix account (API register + CroxyProxy verify) ─
    account_ok = False
    for attempt in range(3):
        account_ok = create_tensorpix_account(email)
        if account_ok:
            break
        log(f"  Account creation failed, retry {attempt+1}/3...")
        time.sleep(10)

    if not account_ok:
        log(f"  FATAL: could not create account for {email}")
        return None, {"status": "failed_account", "email": email}

    # ── 5c. Login ─────────────────────────────────────────
    token = tp_login(email)
    if not token:
        log(f"  FATAL: login failed right after account creation")
        return None, {"status": "failed_login", "email": email}

    # Check balance
    balance = tp_balance(email)
    log(f"  Balance: ${balance}")

    # ── 5d. Upload video ──────────────────────────────────
    video_id = tp_upload_video(token, seg_file)
    if not video_id:
        return None, {"status": "failed_upload", "email": email, "balance": balance}

    # ── 5e. Wait for upload to finish ────────────────────
    if not tp_wait_video_ready(email, video_id, timeout=300):
        return None, {"status": "failed_upload_ready", "email": email, "video_id": video_id}

    # ── 5f. Create enhancement job ────────────────────────
    # Try primary models first, then fallbacks if NO_CREDITS
    preset = MODEL_PRESETS.get(model_preset, MODEL_PRESETS["4k"])
    primary_models = preset["models"]
    fallback_models = preset["fallback"]
    used_model = None
    job_id = None

    for model_id in primary_models + fallback_models:
        job_id = tp_create_job(token, video_id, model_id, output_resolution)
        used_model = model_id

        if job_id == "NO_CREDITS":
            log(f"  Not enough credits for model {model_id}, trying next...")
            continue
        if job_id:
            break

    if not job_id or job_id == "NO_CREDITS":
        return None, {"status": "failed_job", "email": email, "video_id": video_id,
                       "balance": balance}

    # ── 5g. Poll for CDN URL ─────────────────────────────
    cdn_url = tp_poll_cdn(email, timeout=25 * 60)
    if cdn_url:
        return cdn_url, {
            "status": "done", "email": email, "video_id": video_id,
            "job_id": job_id, "cdn_url": cdn_url, "balance": balance,
            "model_id": used_model, "output_resolution": output_resolution,
        }
    else:
        job_status = tp_check_job_status(email, job_id)
        return None, {
            "status": "failed_cdn", "email": email, "video_id": video_id,
            "job_id": job_id, "job_status": job_status, "balance": balance,
            "model_id": used_model,
        }


def main():
    parser = argparse.ArgumentParser(description="TensorPix Bot v11")
    parser.add_argument("--input", "-i", default="input.mkv",
                        help="Input video file path (default: input.mkv)")
    parser.add_argument("--segments", "-s", type=int, default=26,
                        help="Number of segments to split into (default: 26)")
    parser.add_argument("--links-only", action="store_true", default=True,
                        help="Only collect CDN links (default: True)")
    parser.add_argument("--download-enhanced", action="store_true",
                        help="Download enhanced videos (overrides --links-only)")
    parser.add_argument("--output", "-o", default=OUTPUT_FILE,
                        help="Output txt file for CDN links")
    parser.add_argument("--model-preset", type=str, default=DEFAULT_PRESET,
                        choices=list(MODEL_PRESETS.keys()),
                        help="Model preset: 4k, anime-4k, 2k, anime-2k (default: 4k)")
    parser.add_argument("--output-resolution", type=int, default=3840,
                        help="Output resolution width: 3840=4K, 2560=QHD, 1920=1080p, -1=no resize (default: 3840)")
    parser.add_argument("--ml-model", type=int, default=None,
                        help="Override model ID (skips preset, uses this exact model)")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from saved state (bot_state.json)")
    parser.add_argument("--boomlify-key", type=str, default=None,
                        help="Boomlify API key (overrides default)")
    args = parser.parse_args()

    # Apply Boomlify key override
    if args.boomlify_key:
        global BOOMLIFY_API_KEY
        BOOMLIFY_API_KEY = args.boomlify_key

    links_only = not args.download_enhanced

    log("=" * 60)
    log("  TensorPix Bot v11")
    log("=" * 60)
    log(f"  Input       : {args.input}")
    log(f"  Segments    : {args.segments}")
    if args.ml_model:
        log(f"  ML Model    : {args.ml_model} (manual override)")
    else:
        preset = MODEL_PRESETS[args.model_preset]
        log(f"  Model Preset: {args.model_preset} — {preset['description']}")
        log(f"  Models      : {preset['models']} + fallback {preset['fallback']}")
    log(f"  Output Res  : {args.output_resolution}px {'(4K/2160p)' if args.output_resolution == 3840 else ''}")
    log(f"  Links only  : {links_only}")
    log(f"  Resume      : {args.resume}")
    log(f"  Boomlify    : {'***' + BOOMLIFY_API_KEY[-6:] if BOOMLIFY_API_KEY else 'NOT SET'}")
    log("=" * 60)

    # Verify input file exists
    if not os.path.exists(args.input):
        log(f"FATAL: Input file '{args.input}' not found!")
        sys.exit(1)

    # Load or create state
    state = load_state() if args.resume else {"processed": {}, "cdn_links": {}}

    # Split video (skip if segment files already exist)
    seg_dir = "segments"
    if os.path.isdir(seg_dir) and len(list(Path(seg_dir).glob("segment_*.mp4"))) == args.segments:
        log(f"Using existing {args.segments} segment files in {seg_dir}/")
        segments = sorted(str(p) for p in Path(seg_dir).glob("segment_*.mp4"))
    else:
        segments = split_video(args.input, args.segments, seg_dir)

    if len(segments) == 0:
        log("FATAL: No segment files found or created!")
        sys.exit(1)

    if len(segments) != args.segments:
        log(f"WARNING: Expected {args.segments} segments, got {len(segments)}")

    # Determine model preset to use
    model_preset = args.model_preset

    # ── Process each segment ──────────────────────────────
    cdn_links = state.get("cdn_links", {})
    processed = state.get("processed", {})

    for i, seg_file in enumerate(segments):
        seg_num = i + 1
        log("")
        log("=" * 60)
        log(f"  SEGMENT {seg_num}/{len(segments)}  —  {os.path.basename(seg_file)}")
        log("=" * 60)

        # Skip if already processed and has CDN link
        if str(seg_num) in processed and processed[str(seg_num)].get("status") == "done":
            if str(seg_num) in cdn_links:
                log(f"  Already processed — skipping (CDN link exists)")
                continue
            log(f"  Previously processed but no CDN link — re-processing")

        # Process the segment
        cdn_url, entry = process_segment(
            seg_num, seg_file, model_preset, args.output_resolution,
            state, boomlify_key=args.boomlify_key,
        )

        processed[str(seg_num)] = entry

        if cdn_url:
            cdn_links[str(seg_num)] = cdn_url
            log(f"  SUCCESS — CDN URL obtained for segment {seg_num}")
        else:
            log(f"  FAILED — segment {seg_num}  ({entry.get('status')})")

        # Save state after each segment
        save_state({"processed": processed, "cdn_links": cdn_links})

    # ── Write final CDN links file ─────────────────────────
    log("")
    log("=" * 60)
    log("  Writing CDN links to file")
    log("=" * 60)

    found = sum(1 for k, v in cdn_links.items() if v)
    missing = [int(k) for k in range(1, args.segments + 1)
               if str(k) not in cdn_links or not cdn_links[str(k)]]

    with open(args.output, "w") as f:
        f.write("TensorPix CDN Links\n")
        f.write(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Input: {args.input}\n")
        f.write(f"Total segments: {args.segments}\n")
        f.write(f"CDN links found: {found}/{args.segments}\n")
        if args.ml_model:
            f.write(f"Model: {args.ml_model} (manual override)\n")
        else:
            f.write(f"Model preset: {args.model_preset}\n")
        f.write(f"Output resolution: {args.output_resolution}px\n\n")

        for seg_num in range(1, args.segments + 1):
            key = str(seg_num)
            entry = processed.get(key, {})
            if cdn_links.get(key):
                ml = entry.get("model_id", args.ml_model or "?")
                res = entry.get("output_resolution", args.output_resolution)
                f.write(f"# Segment {seg_num}  email={entry.get('email', '?')}  "
                        f"model={ml}  output={res}px\n")
                f.write(f"{cdn_links[key]}\n\n")
            else:
                f.write(f"# Segment {seg_num} — MISSING  "
                        f"status={entry.get('status', '?')}\n")
                f.write(f"# email={entry.get('email', '?')}\n\n")

        if missing:
            f.write(f"\nMISSING SEGMENTS: {missing}\n")
            for seg in missing:
                entry = processed.get(str(seg), {})
                f.write(f"  Segment {seg}: {entry.get('status', '?')}  "
                        f"email={entry.get('email', '?')}\n")

    log(f"  CDN links saved to: {args.output}")
    log(f"  Result: {found}/{args.segments} segments have CDN URLs")
    if missing:
        log(f"  Missing: {missing}")

    log("")
    log("Done!")


if __name__ == "__main__":
    main()