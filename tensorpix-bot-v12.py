#!/usr/bin/env python3
"""
TensorPix Video Upscaler Bot v12 (CDN-ONLY DEFAULT)
====================================================
All-in-one: Creates accounts -> splits video -> enhances -> collects CDN links

v12 CHANGES from v6:
  - DEFAULT MODE IS CDN-ONLY (no download). Faster pipeline.
  - After enhancement, uses REST API to get CDN URL instead of downloading.
  - CDN URLs saved to cdn_links.txt
  - --download-enhanced flag enables full v6 download+merge pipeline
  - --links-only is default (can still pass explicitly)
  - --resume flag to resume from bot_state.json
  - Boomlify domain changed to @usa.priyo.edu.pl with new API key
  - Boomlify endpoint fallback: tries /api/v1/emails/create then /emails/create
  - Referral URL changed (no referral param)
  - --input defaults to input.mkv (not required)
  - --output defaults to cdn_links.txt
  - --boomlify-key for API key override
  - Enhanced files saved to enhanced/ directory when downloading
  - cdn_links.txt includes metadata header and per-segment details

Usage:
  # CDN links only (DEFAULT - fast):
  python3 tensorpix-bot-v12.py --input input.mkv

  # Full download + merge (v6 behavior):
  python3 tensorpix-bot-v12.py --input input.mkv --download-enhanced

  # Resume from state:
  python3 tensorpix-bot-v12.py --input input.mkv --resume

  # Custom API key:
  python3 tensorpix-bot-v12.py --input input.mkv --boomlify-key api_xxx

  # All options:
  python3 tensorpix-bot-v12.py --input input.mkv --count 5 --segments 6 --resolution 2160p --model animation --download-enhanced --resume

Requirements (Colab):
  !pip install playwright
  !playwright install chromium
  !playwright install-deps
  !apt-get install -y ffmpeg aria2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Optional, List, Dict, Any

# ═══════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════

# Default Boomlify key (overridable via --boomlify-key, --api-keys, or BOOMLIFY_API_KEY env)
DEFAULT_BOOMLIFY_KEY = "api_12dd62b98e58eb5638cc90f7db8d5dcd3d8d8140bf3b68833d6a95a73c7dc033"

BOOMLIFY_BASE = "https://v1.boomlify.com"
EMAIL_DOMAIN = "usa.priyo.edu.pl"

PASSWORD = "TpixAcc2026!"
TENSORPIX = "https://app.tensorpix.ai"

DEFAULT_REFERRAL = "https://app.tensorpix.ai/auth/sign-up"

# Segment durations by input resolution (seconds per segment) -- used when auto-splitting
SEG_DURATIONS = {"1080p": 60, "720p": 60, "480p": 90, "360p": 120}

ENHANCE_TIMEOUT = 900          # 15 min per segment
UPLOAD_TIMEOUT = 300            # 5 min for upload
EMAIL_POLL_TIMEOUT = 120        # 2 min for verify email
CDN_POLL_TIMEOUT = 60           # 1 min polling for CDN URL via API

# State file for --resume
STATE_FILE = "bot_state.json"

# ═══════════════════════════════════════════════════════════════════════════
# GLOBALS (set from CLI args)
# ═══════════════════════════════════════════════════════════════════════════

BOOMLIFY_API_KEYS: List[str] = []
_current_key_idx = 0

def get_boomlify_key():
    """Get current Boomlify API key, cycling through the list."""
    global _current_key_idx
    if not BOOMLIFY_API_KEYS:
        return DEFAULT_BOOMLIFY_KEY
    key = BOOMLIFY_API_KEYS[_current_key_idx % len(BOOMLIFY_API_KEYS)]
    _current_key_idx += 1
    return key

# Custom enhance settings from CLI
CUSTOM_RESOLUTION = None   # e.g. "2160p" -- set via --resolution
CUSTOM_MODEL = None        # e.g. "animation" -- set via --model
CUSTOM_SEGMENTS = None     # e.g. 4 -- set via --segments

# ═══════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════

log = logging.getLogger("tpx")
log.setLevel(logging.DEBUG)
_fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s",
                         datefmt="%H:%M:%S")
_ch = logging.StreamHandler(sys.stdout)
_ch.setLevel(logging.INFO)
_ch.setFormatter(_fmt)
log.addHandler(_ch)

def _add_log_file(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fh = logging.FileHandler(path, mode="a", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(_fmt)
    log.addHandler(fh)


# ═══════════════════════════════════════════════════════════════════════════
# BOOMLIFY  (temp email for account creation)
# v12: Fallback to /emails/create with Bearer auth if v1 endpoint fails
# ═══════════════════════════════════════════════════════════════════════════

def _bheaders(api_key=None):
    key = api_key or get_boomlify_key()
    return {
        "X-API-Key": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _bheaders_bearer(api_key=None):
    """Headers for the newer /emails/create endpoint with Bearer auth."""
    key = api_key or get_boomlify_key()
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def create_inbox():
    """Create a temp email inbox via Boomlify. Cycles through API keys on failure.

    v12: Tries /api/v1/emails/create first (v6 style with X-API-Key + query params).
    If that fails with certain errors, falls back to /emails/create (Bearer auth + JSON body).
    """
    # Try each key
    tried = 0
    max_keys = len(BOOMLIFY_API_KEYS) if BOOMLIFY_API_KEYS else 1
    while tried < max_keys:
        key = get_boomlify_key()

        # ── Strategy 1: v6-style endpoint /api/v1/emails/create ──
        params = urllib.parse.urlencode({"time": "1hour", "domain": EMAIL_DOMAIN})
        url_v1 = f"{BOOMLIFY_BASE}/api/v1/emails/create?{params}"
        req = urllib.request.Request(url_v1, method="POST", data=b"{}",
                                    headers=_bheaders(key))
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read())
            eo = data.get("email", {})
            inbox_id = eo.get("id")
            email_addr = eo.get("address")
            if inbox_id and email_addr:
                log.info("[Boomlify] Inbox created (v1 endpoint) with key ending ...%s", key[-8:])
                return inbox_id, email_addr
        except urllib.error.HTTPError as e:
            if e.code in (401, 402, 403, 429):
                log.warning("[Boomlify] v1 key ...%s failed (%d), trying fallback ...",
                           key[-8:], e.code)
                # Fall through to Strategy 2
            else:
                log.error("[Boomlify] v1 HTTP error: %s", e)
        except Exception as e:
            log.warning("[Boomlify] v1 inbox error: %s", e)

        # ── Strategy 2: Newer /emails/create endpoint with Bearer auth ──
        url_new = f"{BOOMLIFY_BASE}/emails/create"
        body = json.dumps({"address": f"@{EMAIL_DOMAIN}"}).encode()
        req2 = urllib.request.Request(url_new, method="POST", data=body,
                                      headers=_bheaders_bearer(key))
        try:
            with urllib.request.urlopen(req2, timeout=30) as r:
                data = json.loads(r.read())
            # The newer endpoint may return data in a different shape
            eo = data.get("email", data)
            inbox_id = eo.get("id")
            email_addr = eo.get("address")
            if inbox_id and email_addr:
                log.info("[Boomlify] Inbox created (fallback endpoint) with key ending ...%s",
                        key[-8:])
                return inbox_id, email_addr
            # Maybe the address field was filled by the server
            if not email_addr and "address" in data:
                email_addr = data["address"]
                inbox_id = data.get("id", data.get("email_id", ""))
                if inbox_id and email_addr:
                    log.info("[Boomlify] Inbox created (fallback, alt shape) with key ending ...%s",
                            key[-8:])
                    return inbox_id, email_addr
        except urllib.error.HTTPError as e:
            log.warning("[Boomlify] Fallback endpoint also failed (%d) for key ...%s",
                       e.code, key[-8:])
        except Exception as e:
            log.warning("[Boomlify] Fallback inbox error: %s", e)

        tried += 1

    log.error("[Boomlify] All API keys exhausted or failed (both endpoints)")
    return None, None


def poll_verify_link(inbox_id, timeout=EMAIL_POLL_TIMEOUT):
    eid = urllib.parse.quote(inbox_id, safe="")
    url = f"{BOOMLIFY_BASE}/api/v1/emails/{eid}/messages"
    key = BOOMLIFY_API_KEYS[0] if BOOMLIFY_API_KEYS else DEFAULT_BOOMLIFY_KEY
    req = urllib.request.Request(url, headers=_bheaders(key))
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
        except Exception:
            time.sleep(5)
            continue
        msgs = data.get("messages") or data.get("data") or []
        if not isinstance(msgs, list):
            msgs = []
        for msg in reversed(msgs):
            sender = str(msg.get("from_email", "")).lower()
            if "tensorpix" not in sender:
                continue
            body = msg.get("body_text") or msg.get("text_body") or ""
            m = re.search(r"(https://app\.tensorpix\.ai/verify-user/\S+)", body)
            if m:
                return m.group(1).rstrip(")")
            body = msg.get("body_html") or msg.get("html_body") or ""
            m = re.search(r"(https://app\.tensorpix\.ai/verify-user/\S+)", body)
            if m:
                return m.group(1).rstrip(")'\">")
        time.sleep(5)
    return None


# ═══════════════════════════════════════════════════════════════════════════
# ACCOUNT CREATION
# ═══════════════════════════════════════════════════════════════════════════

async def create_one_account(browser, referral_url, creds_file, index):
    """Create one TensorPix account via Boomlify temp email."""
    log.info("[Account] Creating account %d ...", index)

    inbox_id, email_addr = await asyncio.to_thread(create_inbox)
    if not inbox_id:
        log.error("[Account] No inbox created (Boomlify credits exhausted?)")
        return None
    log.info("[Account] Email: %s", email_addr)

    page = await browser.new_page()
    try:
        try:
            await page.goto(referral_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            try:
                await page.goto(referral_url, wait_until="commit", timeout=30000)
            except Exception as e2:
                log.error("[Account] Cannot reach TensorPix: %s", e2)
                return None
        await page.wait_for_timeout(5000)

        eloc = page.locator('input[type="email"], input[name="email"]').first
        await eloc.wait_for(state="visible", timeout=15000)
        await eloc.fill(email_addr)

        ploc = page.locator('input[type="password"], input[name="password"]').first
        await ploc.wait_for(state="visible", timeout=10000)
        await ploc.fill(PASSWORD)

        await page.wait_for_timeout(300)
        await page.locator('button:has-text("Create account")').click()
        await page.wait_for_timeout(8000)

        body = (await page.text_content("body") or "").lower()
        if "spam" in body or "blocked" in body:
            log.error("[Account] Email domain blocked")
            return None
        log.info("[Account] Registration submitted, waiting for verify email ...")

        vlink = await asyncio.to_thread(poll_verify_link, inbox_id)
        if not vlink:
            log.error("[Account] No verify link received")
            return None

        try:
            await page.goto(vlink, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            try:
                await page.goto(vlink, wait_until="commit", timeout=30000)
            except Exception:
                pass
        await page.wait_for_timeout(5000)

        try:
            await page.goto(f"{TENSORPIX}/login", wait_until="domcontentloaded",
                             timeout=30000)
        except Exception:
            try:
                await page.goto(f"{TENSORPIX}/login", wait_until="commit",
                             timeout=30000)
            except Exception:
                pass
        await page.wait_for_timeout(6000)

        await page.locator('input[type="email"], input[name="email"]').first.fill(email_addr)
        await page.locator('input[type="password"], input[name="password"]').first.fill(PASSWORD)
        await page.locator('button:has-text("Sign in")').click()
        await page.wait_for_timeout(8000)

        ts = datetime.now().isoformat()
        line = f"{email_addr}  |  {PASSWORD}  |  {ts}\n"
        with open(creds_file, "a") as f:
            f.write(line)
        log.info("[Account] CREATED OK: %s", email_addr)
        return {"email": email_addr, "password": PASSWORD}

    except Exception as e:
        log.error("[Account] Error: %s", e)
        return None
    finally:
        try:
            await page.close()
        except Exception:
            pass


def load_accounts(path):
    """Load accounts from creds file. Returns list of {email, password}."""
    accounts, seen = [], set()
    if not os.path.isfile(path):
        return accounts
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = re.split(r"[|\t]+", line, maxsplit=2)
            if len(parts) < 2:
                continue
            email, pw = parts[0].strip(), parts[1].strip()
            if "@" in email and email not in seen:
                seen.add(email)
                accounts.append({"email": email, "password": pw})
    return accounts


# ═══════════════════════════════════════════════════════════════════════════
# FFMPEG HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def get_video_info(path):
    try:
        r1 = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30)
        r2 = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=s=x:p=0", path],
            capture_output=True, text=True, timeout=30)
        dur = float(r1.stdout.strip())
        w, h = (int(x) for x in r2.stdout.strip().split("x"))
        return {"duration": dur, "width": w, "height": h}
    except Exception as e:
        log.error("ffprobe: %s", e)
        return None


def res_tier(w, h):
    m = min(w, h)
    if m >= 1080: return "1080p"
    if m >= 720:  return "720p"
    if m >= 480:  return "480p"
    return "360p"


def split_video(input_path, seg_dir, seg_dur):
    os.makedirs(seg_dir, exist_ok=True)
    info = get_video_info(input_path)
    if not info or info["duration"] <= 0:
        log.error("Cannot read video info")
        return []
    dur = info["duration"]

    # If --segments was specified, calculate segment duration to fit that count
    if CUSTOM_SEGMENTS and CUSTOM_SEGMENTS > 0:
        seg_dur = max(5, int(dur / CUSTOM_SEGMENTS))
        log.info("[Split] --segments=%d -> adjusting seg_dur to %ds for %.1fs video",
                 CUSTOM_SEGMENTS, seg_dur, dur)

    n = max(1, int(dur / seg_dur))
    if dur > seg_dur and dur % seg_dur > 0:
        n += 1
    log.info("Splitting %.1fs into %d segments of ~%ds each", dur, n, seg_dur)
    segs = []
    for i in range(n):
        out = os.path.join(seg_dir, f"segment_{i:03d}.mp4")
        cmd = ["ffmpeg", "-y", "-i", input_path, "-ss", str(i * seg_dur),
               "-t", str(seg_dur), "-c", "copy",
               "-avoid_negative_ts", "make_zero", out]
        try:
            subprocess.run(cmd, capture_output=True, text=True,
                           timeout=120, check=False)
        except subprocess.TimeoutExpired:
            continue
        if os.path.isfile(out) and os.path.getsize(out) > 100:
            segs.append(out)
            log.info("  segment_%03d.mp4 (%.0f KB)", i,
                     os.path.getsize(out) / 1024)
    log.info("Created %d segment(s)", len(segs))
    return segs


def merge_segments(paths, output):
    if not paths:
        return False
    concat = os.path.join(os.path.dirname(output), "_concat.txt")
    with open(concat, "w") as f:
        for p in paths:
            f.write(f"file '{p.replace(chr(92), '/')}'\n")
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
           "-i", concat, "-c", "copy", output]
    try:
        subprocess.run(cmd, capture_output=True, text=True,
                       timeout=300, check=False)
    except subprocess.TimeoutExpired:
        return False
    finally:
        if os.path.isfile(concat):
            os.remove(concat)
    if os.path.isfile(output):
        log.info("MERGED: %s (%.1f MB)", output,
                 os.path.getsize(output) / (1024 * 1024))
        return True
    return False


def get_file_duration(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


# ═══════════════════════════════════════════════════════════════════════════
# ARIA2C DOWNLOAD HELPER
# ═══════════════════════════════════════════════════════════════════════════

def _download_aria2c(url, output_path, referer="https://app.tensorpix.ai/"):
    """Download a file using aria2c with 16 connections. Returns True on success."""
    if not shutil.which("aria2c"):
        log.warning("[aria2c] Not found, falling back to urllib")
        return False

    cmd = [
        "aria2c",
        "-x", "16", "-s", "16", "-k", "1M",
        "--max-tries=3", "--retry-wait=5",
        "--timeout=120", "--connect-timeout=30",
        "--check-certificate=false",
        f"--referer={referer}",
        f"--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "-d", os.path.dirname(output_path) or ".",
        "-o", os.path.basename(output_path),
        url,
    ]
    log.info("[aria2c] Downloading to %s ...", output_path)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 0 and os.path.isfile(output_path):
            sz = os.path.getsize(output_path)
            log.info("[aria2c] OK: %s (%.1f MB)", output_path, sz / (1024 * 1024))
            return True
        else:
            log.warning("[aria2c] Failed (rc=%d): %s", result.returncode,
                       (result.stderr or "")[-200:])
            # Clean up partial file
            if os.path.isfile(output_path):
                try:
                    os.remove(output_path)
                except Exception:
                    pass
            return False
    except subprocess.TimeoutExpired:
        log.error("[aria2c] Timeout after 600s")
        return False
    except Exception as e:
        log.error("[aria2c] Error: %s", e)
        return False


def _download_urllib(url, output_path, referer="https://app.tensorpix.ai/"):
    """Download a file using urllib as fallback. Returns True on success."""
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36")
        req.add_header("Referer", referer)
        # NO Authorization header! CDN rejects it.
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = resp.read()
        if len(data) > 100 * 1024:
            with open(output_path, "wb") as f:
                f.write(data)
            log.info("[urllib] Downloaded %s (%.1f MB)",
                     output_path, len(data) / (1024 * 1024))
            return True
        else:
            log.warning("[urllib] File too small: %d bytes", len(data))
            return False
    except Exception as e:
        log.error("[urllib] Download failed: %s", str(e)[:150])
        return False


def download_file(url, output_path, referer="https://app.tensorpix.ai/"):
    """Download a file using aria2c (fast) or urllib (fallback).
    NO Authorization header sent to CDN -- that was the v5 bug."""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    # Try aria2c first (16 connections, much faster for large files)
    if _download_aria2c(url, output_path, referer):
        if os.path.isfile(output_path) and os.path.getsize(output_path) > 100 * 1024:
            return True
    # Fallback to urllib
    return _download_urllib(url, output_path, referer)


# ═══════════════════════════════════════════════════════════════════════════
# TENSORPIX BROWSER OPERATIONS
# ═══════════════════════════════════════════════════════════════════════════

async def tpx_login(page, email, password):
    log.info("[Bot] Logging in: %s", email)
    try:
        await page.goto(f"{TENSORPIX}/login", wait_until="domcontentloaded",
                         timeout=30000)
    except Exception:
        try:
            await page.goto(f"{TENSORPIX}/login", wait_until="commit", timeout=30000)
        except Exception:
            pass
    await page.wait_for_timeout(8000)
    try:
        await page.locator('input[type="email"], input[name="email"]').first.fill(email)
        await page.locator('input[type="password"], input[name="password"]').first.fill(password)
        await page.wait_for_timeout(500)
        await page.locator('button:has-text("Sign in")').click()
        await page.wait_for_timeout(8000)
    except Exception as e:
        log.error("[Bot] Login form error: %s", e)
        return False
    try:
        url = page.url
        if "/login" not in url.lower():
            log.info("[Bot] Login OK (redirected from /login)")
            return True
        text = (await page.text_content("body") or "").lower()
        if "credits" in text or "videos" in text or "enhance" in text:
            log.info("[Bot] Login OK (dashboard text detected)")
            return True
        if "invalid" in text or "incorrect" in text or "wrong" in text:
            log.error("[Bot] Bad credentials for %s", email)
            return False
        log.info("[Bot] Login OK (no error detected)")
        return True
    except Exception:
        return False


async def tpx_upload(page, file_path):
    abs_path = os.path.abspath(file_path)
    log.info("[Bot] Uploading: %s (%.1f MB)",
             os.path.basename(file_path), os.path.getsize(abs_path) / (1024 * 1024))
    try:
        await page.goto(f"{TENSORPIX}/videos", wait_until="domcontentloaded",
                         timeout=30000)
    except Exception:
        try:
            await page.goto(f"{TENSORPIX}/videos", wait_until="commit", timeout=30000)
        except Exception:
            pass
    await page.wait_for_timeout(8000)

    # Strategy 1: hidden file input
    try:
        inp = page.locator('input[type="file"]').first
        await inp.set_input_files(abs_path)
        log.info("[Bot] Uploaded via file input")
        await page.wait_for_timeout(5000)
        return True
    except Exception:
        pass

    # Strategy 2: make visible then upload
    try:
        await page.evaluate("""() => {
            document.querySelectorAll('input[type="file"]').forEach(i => {
                i.style.display = 'block';
                i.removeAttribute('hidden');
                i.style.opacity = '1';
                i.style.position = 'fixed';
                i.style.top = '0'; i.style.left = '0';
                i.style.zIndex = '99999';
            });
        }""")
        await page.wait_for_timeout(1000)
        inp = page.locator('input[type="file"]').first
        await inp.set_input_files(abs_path)
        log.info("[Bot] Uploaded after making input visible")
        await page.wait_for_timeout(5000)
        return True
    except Exception:
        pass

    # Strategy 3: file chooser dialog
    try:
        async with page.expect_file_chooser(timeout=10000) as fc:
            await page.locator('[class*="uppy"], [class*="upload"], [data-uppy]').first.click()
        await fc.value.set_files(abs_path)
        log.info("[Bot] Uploaded via file chooser dialog")
        await page.wait_for_timeout(5000)
        return True
    except Exception:
        pass

    log.error("[Bot] All upload strategies failed")
    return False


async def tpx_wait_upload(page):
    """Wait for upload to complete by detecting URL change."""
    log.info("[Bot] Waiting for upload to finish ...")
    start = time.time()
    initial_url = page.url
    last_url = initial_url

    if "/start-job" in initial_url or "/enhance" in initial_url:
        log.info("[Bot] Upload already done (URL = %s)", initial_url[:80])
        await page.wait_for_timeout(3000)
        return True

    while time.time() - start < UPLOAD_TIMEOUT:
        elapsed = time.time() - start
        if elapsed < 10:
            await page.wait_for_timeout(5000)
            continue
        try:
            cur_url = page.url
            text = (await page.text_content("body") or "").lower()
        except Exception:
            await page.wait_for_timeout(5000)
            continue

        if cur_url != initial_url and cur_url != last_url:
            log.info("[Bot] Upload done (URL: %s -> %s)", last_url[:60], cur_url[:60])
            await page.wait_for_timeout(5000)
            return True

        for w in ["enhancement settings", "start enhancing", "select enhancement",
                   "choose your enhancement", "enhancement model"]:
            if w in text:
                log.info("[Bot] Upload done (text: '%s')", w)
                await page.wait_for_timeout(3000)
                return True

        if cur_url != last_url:
            log.info("[Bot] URL drift: %s -> %s", last_url[:60], cur_url[:60])
            last_url = cur_url

        if "upload failed" in text or "upload error" in text:
            log.error("[Bot] Upload error detected")
            return False

        if int(elapsed) % 15 == 0 and elapsed > 10:
            log.info("[Bot] Still uploading ... %ds  url=%s", int(elapsed), cur_url[:80])

        await page.wait_for_timeout(5000)

    log.error("[Bot] Upload timed out after %ds", UPLOAD_TIMEOUT)
    return False


# ═══════════════════════════════════════════════════════════════════════════
# ENHANCEMENT SETTINGS (v12: customizable resolution + model -- same as v6)
# ═══════════════════════════════════════════════════════════════════════════

async def tpx_enhance(page):
    """Select enhancement settings and click Enhance.

    v12: Same as v6. Supports --resolution and --model CLI overrides.
    - If CUSTOM_MODEL is set (e.g. "animation"), selects that model instead of "4x Upscale Ultra 4"
    - If CUSTOM_RESOLUTION is set (e.g. "2160p"), selects that resolution
    - Otherwise uses default "4x Upscale Ultra 4" behavior
    """
    log.info("[Bot] Selecting enhancement preset ...")
    await page.wait_for_timeout(3000)

    # Click Manual tab
    tab_clicked = False
    for sel in ['a:has-text("Manual")', 'button:has-text("Manual")']:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=2000):
                txt = (await loc.text_content() or "").strip()
                if txt == "Manual":
                    log.info("[Bot] Clicked Manual tab")
                    await loc.click()
                    tab_clicked = True
                    await page.wait_for_timeout(3000)
                    break
        except Exception:
            continue

    if not tab_clicked:
        try:
            r = await page.evaluate("""() => {
                const els = document.querySelectorAll('a,button,div,span,[role="tab"]');
                for (const el of els) {
                    if (el.offsetParent === null) continue;
                    if ((el.textContent||'').trim() === 'Manual') {
                        el.click(); return 'clicked';
                    }
                }
                return 'not found';
            }""")
            if "clicked" in str(r):
                tab_clicked = True
                await page.wait_for_timeout(3000)
        except Exception:
            pass

    if not tab_clicked:
        log.warning("[Bot] Could not click Manual tab (may already be selected)")

    # ── STEP 1: Select model / preset ──
    await page.wait_for_timeout(2000)
    preset_ok = False

    if CUSTOM_MODEL:
        # User specified a custom model (e.g. "animation")
        model_names = [
            CUSTOM_MODEL,                          # exact match
            CUSTOM_MODEL.capitalize(),             # "Animation"
            CUSTOM_MODEL.title(),                  # "Animation"
            f"{CUSTOM_MODEL} 4",                   # "animation 4"
            f"{CUSTOM_MODEL.capitalize()} 4",      # "Animation 4"
        ]
        log.info("[Bot] Looking for custom model: %s (trying: %s)", CUSTOM_MODEL, model_names)

        for name in model_names:
            try:
                loc = page.locator(f"text={name}").first
                if await loc.is_visible(timeout=3000):
                    log.info("[Bot] Selected custom model: %s", name)
                    await loc.click()
                    preset_ok = True
                    await page.wait_for_timeout(2000)
                    break
            except Exception:
                continue

        if not preset_ok:
            # JS fallback: search for partial match
            try:
                r = await page.evaluate(f"""() => {{
                    const target = '{CUSTOM_MODEL}'.toLowerCase();
                    const els = document.querySelectorAll('div,span,button,a,li,[role="option"]');
                    for (const el of els) {{
                        if (el.offsetParent === null) continue;
                        const t = (el.textContent||'').trim().toLowerCase();
                        if (t.includes(target)) {{
                            el.click(); return 'clicked: ' + (el.textContent||'').trim();
                        }}
                    }}
                    return 'not found';
                }}""")
                if "clicked" in str(r):
                    preset_ok = True
                    await page.wait_for_timeout(2000)
                    log.info("[Bot] JS clicked model: %s", str(r))
            except Exception:
                pass
    else:
        # Default: "4x Upscale Ultra 4"
        for name in ["4x Upscale Ultra 4", "4x Upscale Ultra"]:
            try:
                loc = page.locator(f"text={name}").first
                if await loc.is_visible(timeout=3000):
                    log.info("[Bot] Selected: %s", name)
                    await loc.click()
                    preset_ok = True
                    await page.wait_for_timeout(2000)
                    break
            except Exception:
                continue

        if not preset_ok:
            try:
                r = await page.evaluate("""() => {
                    const els = document.querySelectorAll('div,span,button,a,li,[role="option"]');
                    for (const el of els) {
                        if (el.offsetParent === null) continue;
                        const t = (el.textContent||'').trim();
                        if (t.startsWith('4x Upscale Ultra')) {
                            el.click(); return 'clicked: ' + t;
                        }
                    }
                    return 'not found';
                }""")
                if "clicked" in str(r):
                    preset_ok = True
                    await page.wait_for_timeout(2000)
                    log.info("[Bot] JS clicked: %s", str(r))
            except Exception:
                pass

    if not preset_ok:
        log.error("[Bot] FAILED to find preset! (model=%s)", CUSTOM_MODEL or "4x Upscale Ultra 4")
        try:
            all_text = await page.evaluate("""() => {
                const els = document.querySelectorAll('div,span,button,a,li');
                const texts = [];
                for (const el of els) {
                    if (el.offsetParent === null) continue;
                    const t = (el.textContent||'').trim();
                    if (t.length > 0 && t.length < 80) texts.push(t);
                }
                return [...new Set(texts)].slice(0, 40);
            }""")
            log.info("[Bot] Page texts (debug): %s", all_text)
        except Exception:
            pass
    else:
        log.info("[Bot] Preset selected OK")

    # ── STEP 2: Select resolution (if --resolution specified) ──
    if CUSTOM_RESOLUTION:
        await page.wait_for_timeout(2000)
        res_ok = False
        res_str = str(CUSTOM_RESOLUTION)

        # Try common resolution labels
        res_labels = [
            res_str,                 # "2160p"
            res_str + "p",           # safety
            f"{CUSTOM_RESOLUTION} (4K)",  # "2160p (4K)"
            "2160p (4K)",
            "2160p",
            "1080p",
            "1440p",
        ]

        log.info("[Bot] Looking for resolution: %s", res_str)

        # Try clicking resolution selector/tab first
        for label in res_labels:
            try:
                loc = page.locator(f"text={label}").first
                if await loc.is_visible(timeout=2000):
                    log.info("[Bot] Selected resolution: %s", label)
                    await loc.click()
                    res_ok = True
                    await page.wait_for_timeout(1500)
                    break
            except Exception:
                continue

        if not res_ok:
            # JS fallback: find resolution option
            try:
                r = await page.evaluate(f"""() => {{
                    const target = '{res_str}';
                    const els = document.querySelectorAll('div,span,button,a,li,label,[role="option"],[role="radio"]');
                    for (const el of els) {{
                        if (el.offsetParent === null) continue;
                        const t = (el.textContent||'').trim().toLowerCase();
                        if (t.includes(target.toLowerCase()) || t.includes('4k') || t.includes('2160') || t.includes('1080') || t.includes('1440')) {{
                            el.click(); return 'clicked: ' + (el.textContent||'').trim();
                        }}
                    }}
                    return 'not found';
                }}""")
                if "clicked" in str(r):
                    res_ok = True
                    await page.wait_for_timeout(1500)
                    log.info("[Bot] JS clicked resolution: %s", str(r))
            except Exception:
                pass

        if res_ok:
            log.info("[Bot] Resolution %s selected", res_str)
        else:
            log.warning("[Bot] Could not find resolution '%s' -- continuing with default", res_str)

    # ── STEP 3: Click Enhance button ──
    await page.wait_for_timeout(2000)

    for sel in ['button:has-text("Enhance")', 'button:has-text("Enhance Now")',
                'button:has-text("Start Enhancing")']:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=3000):
                txt = (await btn.text_content() or "").strip()
                log.info("[Bot] Clicked: '%s'", txt)
                await btn.click()
                await page.wait_for_timeout(5000)
                return True
        except Exception:
            continue

    # JS fallback
    try:
        r = await page.evaluate("""() => {
            const all = document.querySelectorAll('button,[role="button"],div,a,span');
            for (const el of all) {
                if (el.offsetParent === null) continue;
                const t = (el.textContent||'').trim();
                const tl = t.toLowerCase();
                if (tl.includes('enhance specific') || tl.includes('buy credit') ||
                    tl.includes('preset') || tl.includes('segment')) continue;
                if (['enhance','enhance now','start enhancing'].includes(tl)) {
                    el.scrollIntoView(); el.click();
                    return {clicked: true, text: t};
                }
                if (/^\\d+\\.\\d+/.test(t) && /credits$/i.test(t) && t.length < 40) {
                    el.scrollIntoView(); el.click();
                    return {clicked: true, text: t};
                }
            }
            const btns = document.querySelectorAll('button,[role="button"]');
            const btnTexts = [];
            for (const b of btns) {
                if (b.offsetParent !== null)
                    btnTexts.push((b.textContent||'').trim().substring(0,60));
            }
            return {clicked: false, buttons: btnTexts};
        }""")
        log.info("[Bot] JS enhance scan: %s", str(r)[:300])
        if isinstance(r, dict) and r.get('clicked'):
            log.info("[Bot] JS clicked enhance: %s", r.get('text', ''))
            await page.wait_for_timeout(5000)
            return True
    except Exception:
        pass

    log.error("[Bot] Could not click Enhance button")
    return False


async def tpx_wait_enhancement(page):
    """Wait for enhancement to complete. Detects redirect to /videos/enhanced."""
    log.info("[Bot] Waiting for enhancement (timeout=%ds) ...", ENHANCE_TIMEOUT)
    start = time.time()

    while time.time() - start < ENHANCE_TIMEOUT:
        try:
            cur = page.url
        except Exception:
            await page.wait_for_timeout(10000)
            continue

        elapsed = time.time() - start

        if "/videos/enhanced" in cur:
            log.info("[Bot] *** REDIRECTED to /videos/enhanced -> VIDEO READY (%.0fs) ***",
                     elapsed)
            await page.wait_for_timeout(5000)
            return True

        try:
            text = (await page.text_content("body") or "").lower()
        except Exception:
            await page.wait_for_timeout(10000)
            continue

        for m in ["your video is ready", "enhancement complete",
                   "processing complete", "ready to download", "video ready",
                   "download enhanced"]:
            if m in text:
                log.info("[Bot] Done (text: '%s', %.0fs)", m, elapsed)
                return True

        for m in ["enhancement failed", "processing failed", "not enough credits",
                   "error processing", "failed to process"]:
            if m in text:
                log.error("[Bot] Enhancement error: '%s'", m)
                return False

        pct = re.search(r"(\d+)\s*%", text)
        if pct:
            log.info("[Bot] Progress: %s%% (%.0fs)", pct.group(1), elapsed)
            if int(pct.group(1)) >= 99:
                log.info("[Bot] >=99%%, waiting for redirect ...")
                await page.wait_for_timeout(10000)
                try:
                    if "/videos/enhanced" in page.url:
                        return True
                except Exception:
                    pass

        if int(elapsed) % 30 == 0 and int(elapsed) > 0:
            log.info("[Bot] Still processing ... %ds  url=%s",
                     int(elapsed), cur[:80])

        await page.wait_for_timeout(10000)

    log.error("[Bot] Enhancement timed out after %ds", ENHANCE_TIMEOUT)
    return False


# ═══════════════════════════════════════════════════════════════════════════
# DOWNLOAD  --  MULTI-STRATEGY (v12: same as v6 FIX: aria2c, NO auth header to CDN)
# ═══════════════════════════════════════════════════════════════════════════

def _tpx_api_login(email, password):
    """Login to TensorPix REST API, return JWT token."""
    url = "https://backend.tensorpix.ai/api/token/"
    data = json.dumps({"email": email, "password": password}).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                headers={"Content-Type": "application/json",
                                         "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            token_data = json.loads(resp.read())
        return token_data.get("access", "")
    except Exception as e:
        log.error("[DL-API] Login failed: %s", e)
        return ""


def _tpx_api_get_video(token):
    """Query restored-videos API for latest enhanced video. Returns dict or None."""
    url = "https://backend.tensorpix.ai/api/restored-videos/?limit=5"
    req = urllib.request.Request(url,
                                headers={"Authorization": f"Bearer {token}",
                                         "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        results = data.get("results", [])
        if not results:
            return None
        for v in results:
            if v.get("file"):
                return v
        return None
    except Exception as e:
        log.error("[DL-API] Get videos failed: %s", e)
        return None


async def _download_strategy_click_button(page, save_dir, seg_idx):
    """Strategy 1: Click download button + Playwright download event."""
    log.info("[DL] Strategy 1: Click download button ...")

    download_selectors = [
        'a:has-text("Download")',
        'button:has-text("Download")',
        'a:has-text("download")',
        'button:has-text("download")',
        'a[href*=".mp4"]',
        'a[href*=".mkv"]',
        '[class*="download"]',
    ]

    for sel in download_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=2000):
                log.info("[DL] Found download element: %s", sel)

                async with page.expect_download(timeout=120000) as download_event:
                    await loc.click()
                    await page.wait_for_timeout(5000)

                try:
                    dl = download_event.value
                    sp = os.path.join(save_dir, f"seg{seg_idx:03d}_enhanced.mp4")
                    await dl.save_as(sp)
                    sz = os.path.getsize(sp)
                    if sz > 100 * 1024:
                        dur = get_file_duration(sp)
                        log.info("[DL] Strategy 1 OK: %s (%.1f MB, %.1fs)",
                                 sp, sz / (1024 * 1024), dur)
                        return sp
                    else:
                        log.warning("[DL] Strategy 1: downloaded only %d KB (thumbnail?)", sz // 1024)
                        if os.path.isfile(sp):
                            os.remove(sp)
                except Exception as e:
                    log.warning("[DL] Strategy 1 download event error: %s", e)
                break
        except Exception:
            continue

    log.info("[DL] Strategy 1: No download button captured")
    return None


async def _download_strategy_video_element(page, save_dir, seg_idx):
    """Strategy 2: Probe <video> element for src/currentSrc."""
    log.info("[DL] Strategy 2: Video element probe ...")

    try:
        video_info = await page.evaluate("""() => {
            const videos = document.querySelectorAll('video');
            const results = [];
            for (const v of videos) {
                results.push({
                    src: v.src || '',
                    currentSrc: v.currentSrc || '',
                    poster: v.poster || '',
                });
                const sources = v.querySelectorAll('source');
                for (const s of sources) {
                    results.push({ src: s.src || '', currentSrc: '', poster: '' });
                }
            }
            return results;
        }""")

        log.info("[DL] Found %d video element(s)", len(video_info))

        for vi in video_info:
            url = vi.get("currentSrc") or vi.get("src") or ""
            if not url or url.startswith("blob:") or url.startswith("data:"):
                continue
            if ".mp4" in url or ".mkv" in url or ".webm" in url or "cdn" in url.lower():
                log.info("[DL] Found video URL: %s", url[:120])
                sp = os.path.join(save_dir, f"seg{seg_idx:03d}_enhanced.mp4")
                ok = download_file(url, sp, referer="https://app.tensorpix.ai/")
                if ok and os.path.isfile(sp) and os.path.getsize(sp) > 100 * 1024:
                    dur = get_file_duration(sp)
                    log.info("[DL] Strategy 2 OK: %s (%.1f MB, %.1fs)",
                             sp, os.path.getsize(sp) / (1024 * 1024), dur)
                    return sp

    except Exception as e:
        log.warning("[DL] Strategy 2 error: %s", e)

    return None


async def _download_strategy_network_intercept(page, save_dir, seg_idx):
    """Strategy 3: Reload page while intercepting video/mp4 responses."""
    log.info("[DL] Strategy 3: Network intercept on page reload ...")

    captured_urls = []

    def on_response(response):
        try:
            ct = response.headers.get("content-type", "")
            url = response.url
            if ("video/mp4" in ct or "video/webm" in ct or
                ".mp4" in url or ".mkv" in url):
                if "thumbnail" not in url.lower() and "preview" not in url.lower():
                    cl = response.headers.get("content-length", "0")
                    try:
                        size = int(cl)
                    except (ValueError, TypeError):
                        size = 0
                    if size > 100 * 1024:
                        captured_urls.append({"url": url, "size": size, "ct": ct})
                        log.info("[DL-Intercept] Caught: %s (%d bytes)", url[:80], size)
        except Exception:
            pass

    page.on("response", on_response)

    try:
        await page.reload(wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(15000)

        if captured_urls:
            captured_urls.sort(key=lambda x: x["size"], reverse=True)
            best = captured_urls[0]
            log.info("[DL] Best intercepted URL: %s (%d bytes)", best["url"][:80], best["size"])
            sp = os.path.join(save_dir, f"seg{seg_idx:03d}_enhanced.mp4")
            ok = download_file(best["url"], sp, referer="https://app.tensorpix.ai/")
            if ok and os.path.isfile(sp) and os.path.getsize(sp) > 100 * 1024:
                dur = get_file_duration(sp)
                log.info("[DL] Strategy 3 OK: %s (%.1f MB, %.1fs)",
                         sp, os.path.getsize(sp) / (1024 * 1024), dur)
                return sp
            else:
                log.warning("[DL] Strategy 3: download too small or failed")
        else:
            log.info("[DL] Strategy 3: No video responses intercepted")
    except Exception as e:
        log.warning("[DL] Strategy 3 error: %s", str(e)[:150])
    finally:
        page.remove_listener("response", on_response)

    return None


async def _download_strategy_api(page, save_dir, seg_idx, email, password):
    """Strategy 4: REST API fallback -- THE CRITICAL FIX IS HERE.

    v5 BUG: This sent 'Authorization: Bearer {token}' header to the CDN URL,
    which caused HTTP 400 Bad Request from Cloudflare R2.

    v6 FIX: Download via aria2c/urllib with ONLY Referer header, NO Authorization.
    The API endpoint itself still gets the auth header. Only the CDN file download
    is stripped of it.
    """
    log.info("[DL] Strategy 4: REST API fallback ...")

    token = _tpx_api_login(email, password)
    if not token:
        log.error("[DL-API] Auth failed")
        return None

    # Poll for video (may not appear immediately after redirect)
    video_entry = None
    poll_start = time.time()
    while time.time() - poll_start < 60:
        video_entry = _tpx_api_get_video(token)
        if video_entry:
            break
        log.info("[DL-API] No video yet, retrying in 5s ...")
        await asyncio.sleep(5)

    if not video_entry:
        log.error("[DL-API] No enhanced video found after 60s")
        return None

    file_url = video_entry.get("file", "")
    vid_name = video_entry.get("name", "?")
    vid_w = video_entry.get("width", 0)
    vid_h = video_entry.get("height", 0)
    vid_size = video_entry.get("size", 0)
    log.info("[DL-API] Found: %s (%dx%d, %d bytes)", vid_name, vid_w, vid_h, vid_size)
    log.info("[DL-API] CDN URL: %s...%s",
             file_url[:60], file_url[-40:] if len(file_url) > 40 else "")

    if not file_url:
        log.error("[DL-API] No file URL in response")
        log.info("[DL-API] Available keys: %s", list(video_entry.keys()))
        return None

    # Build output filename from API info
    clean_name = re.sub(r'[^\w\-.]', '_', vid_name).strip('_')
    if not clean_name:
        clean_name = f"segment_{seg_idx:03d}"
    sp = os.path.join(save_dir, f"seg{seg_idx:03d}_{clean_name}.mp4")

    # Download CDN file WITHOUT Authorization header
    # aria2c does NOT send auth headers by default -- perfect
    log.info("[DL-API] Downloading via aria2c/urllib (NO auth header to CDN) ...")
    ok = download_file(file_url, sp, referer="https://app.tensorpix.ai/")

    if ok and os.path.isfile(sp):
        sz = os.path.getsize(sp)
        dur = get_file_duration(sp)
        log.info("[DL-API] Strategy 4 OK: %s (%.1f MB, %.1fs)",
                 sp, sz / (1024 * 1024), dur)
        return sp

    log.error("[DL-API] Download failed (all methods)")
    return None


async def tpx_download(page, save_dir, seg_idx, email, password):
    """Download enhanced video using MULTI-STRATEGY approach.

    v12: Same as v6. All strategies use download_file() which tries aria2c first, urllib fallback.
    NO Authorization header sent to CDN URLs.
    """
    os.makedirs(save_dir, exist_ok=True)
    log.info("[Bot] Downloading enhanced video for seg %d ...", seg_idx)

    try:
        cur = page.url
        if "/videos/enhanced" not in cur:
            log.warning("[DL] Not on enhanced page (url=%s), navigating ...", cur[:80])
            await page.goto(f"{TENSORPIX}/videos/enhanced",
                           wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)
    except Exception as e:
        log.warning("[DL] Navigation to enhanced page failed: %s", e)

    # Strategy 1: Click download button
    result = await _download_strategy_click_button(page, save_dir, seg_idx)
    if result:
        return result

    # Strategy 2: Video element probe
    result = await _download_strategy_video_element(page, save_dir, seg_idx)
    if result:
        return result

    # Strategy 3: Network intercept
    result = await _download_strategy_network_intercept(page, save_dir, seg_idx)
    if result:
        return result

    # Strategy 4: API fallback (the one that actually works)
    result = await _download_strategy_api(page, save_dir, seg_idx, email, password)
    if result:
        return result

    log.error("[Bot] ALL download strategies failed for segment %d", seg_idx)

    # Debug dump
    try:
        debug_info = await page.evaluate("""() => {
            return {
                url: window.location.href,
                title: document.title,
                hasVideo: !!document.querySelector('video'),
                buttons: Array.from(document.querySelectorAll('button')).slice(0,10).map(b => b.textContent?.trim()?.substring(0,40)),
            };
        }""")
        log.info("[DL-DEBUG] %s", json.dumps(debug_info, indent=2)[:400])
    except Exception:
        pass

    return None


# ═══════════════════════════════════════════════════════════════════════════
# STATE / RESUME  (v12 new)
# ═══════════════════════════════════════════════════════════════════════════

def load_state(state_path: str) -> Dict[str, Any]:
    """Load bot state from JSON file. Returns empty dict if not found or invalid."""
    if not os.path.isfile(state_path):
        return {}
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        log.info("[State] Loaded state: %d segment entries", len(state.get("segments", {})))
        return state
    except Exception as e:
        log.warning("[State] Failed to load state: %s", e)
        return {}


def save_state(state_path: str, state: Dict[str, Any]):
    """Save bot state to JSON file atomically."""
    state["last_updated"] = datetime.now().isoformat()
    tmp = state_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, state_path)
        log.debug("[State] State saved (%d segments)", len(state.get("segments", {})))
    except Exception as e:
        log.error("[State] Failed to save state: %s", e)


# ═══════════════════════════════════════════════════════════════════════════
# CDN LINKS OUTPUT  (v12 new)
# ═══════════════════════════════════════════════════════════════════════════

def write_cdn_links(output_path: str, input_name: str, total_segments: int,
                    segment_results: List[Dict[str, Any]], model_name: str = "4x Upscale Ultra 4"):
    """Write cdn_links.txt with formatted CDN URL output.

    Args:
        output_path: Path to cdn_links.txt
        input_name: Name of the input video file
        total_segments: Total number of segments
        segment_results: List of dicts with keys:
            - seg_idx: int
            - cdn_url: str or None
            - email: str
            - file_path: str or None (if downloaded)
            - status: str ("ok", "failed_cdn", "failed_enhance", "skipped")
        model_name: The enhancement model used
    """
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cdn_found = sum(1 for r in segment_results if r.get("cdn_url"))
    file_found = sum(1 for r in segment_results if r.get("file_path"))

    lines = []
    lines.append("TensorPix CDN Links")
    lines.append(f"Generated: {now}")
    lines.append(f"Input: {input_name}")
    lines.append(f"Total segments: {total_segments}")
    if cdn_found > 0:
        lines.append(f"CDN links found: {cdn_found}/{total_segments}")
    if file_found > 0:
        lines.append(f"Files downloaded: {file_found}/{total_segments}")
    lines.append(f"Model: {model_name}")
    lines.append("")

    for r in segment_results:
        seg_idx = r.get("seg_idx", 0)
        seg_num = seg_idx + 1
        email = r.get("email", "?")
        cdn_url = r.get("cdn_url")
        file_path = r.get("file_path")
        status = r.get("status", "unknown")

        if cdn_url:
            lines.append(f"# Segment {seg_num}  email={email}  model={model_name}")
            lines.append(cdn_url)
            lines.append("")
        elif file_path:
            lines.append(f"# Segment {seg_num}  email={email}  model={model_name}  (downloaded)")
            lines.append(f"# file://{file_path}")
            lines.append("")
        elif status == "skipped":
            lines.append(f"# Segment {seg_num} — SKIPPED (already done)")
            lines.append("")
        elif status == "failed_cdn":
            lines.append(f"# Segment {seg_num} — MISSING  status=failed_cdn")
            lines.append(f"# email={email}")
            lines.append("")
        elif status == "failed_enhance":
            lines.append(f"# Segment {seg_num} — MISSING  status=failed_enhance")
            lines.append(f"# email={email}")
            lines.append("")
        elif status == "failed_login":
            lines.append(f"# Segment {seg_num} — MISSING  status=failed_login")
            lines.append(f"# email={email}")
            lines.append("")
        elif status == "failed_upload":
            lines.append(f"# Segment {seg_num} — MISSING  status=failed_upload")
            lines.append(f"# email={email}")
            lines.append("")
        else:
            lines.append(f"# Segment {seg_num} — MISSING  status={status}")
            lines.append(f"# email={email}")
            lines.append("")

    content = "\n".join(lines)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    log.info("[Output] CDN links written to %s", output_path)
    log.info("[Output] %d/%d segments have CDN URLs", cdn_found, total_segments)
    if file_found > 0:
        log.info("[Output] %d/%d segments were downloaded", file_found, total_segments)


# ═══════════════════════════════════════════════════════════════════════════
# PROCESS ONE SEGMENT  (v12: supports both links-only and download modes)
# ═══════════════════════════════════════════════════════════════════════════

async def process_segment(browser, account, seg_path, seg_idx, total,
                          save_dir, seg_duration, links_only=False):
    """Full pipeline: login -> upload -> enhance -> wait -> CDN URL or download.

    v12: Supports two modes:
    - links_only=True (default): Gets CDN URL via REST API, no download. Faster.
    - links_only=False (--download-enhanced): Full v6 download pipeline.

    Returns dict with:
        - seg_idx: int
        - email: str
        - cdn_url: str or None
        - file_path: str or None (only in download mode)
        - status: str ("ok", "failed_cdn", "failed_enhance", "failed_login", "failed_upload")
    """
    log.info("=" * 60)
    log.info("SEGMENT %d/%d -- account: %s -- mode=%s",
             seg_idx + 1, total, account["email"],
             "cdn_only" if links_only else "download")
    log.info("=" * 60)

    page = None
    result: Dict[str, Any] = {
        "seg_idx": seg_idx,
        "email": account["email"],
        "cdn_url": None,
        "file_path": None,
        "status": "failed_enhance",
    }

    try:
        page = await browser.new_page(accept_downloads=True)

        if not await tpx_login(page, account["email"], account["password"]):
            log.error("[Bot] Login failed: %s", account["email"])
            result["status"] = "failed_login"
            return result

        if not await tpx_upload(page, seg_path):
            result["status"] = "failed_upload"
            return result

        if not await tpx_wait_upload(page):
            result["status"] = "failed_upload"
            return result

        if not await tpx_enhance(page):
            result["status"] = "failed_enhance"
            return result

        if not await tpx_wait_enhancement(page):
            result["status"] = "failed_enhance"
            return result

        log.info("[Bot] Enhancement done!")

        await page.wait_for_timeout(3000)

        if links_only:
            # ── CDN-ONLY MODE: Get CDN URL via REST API (no download) ──
            log.info("[Bot] Getting CDN URL via REST API ...")
            token = await asyncio.to_thread(_tpx_api_login, account["email"],
                                            account["password"])
            if not token:
                log.error("[Bot] API login failed, cannot get CDN URL")
                result["status"] = "failed_cdn"
                return result

            cdn_url = None
            poll_start = time.time()
            while time.time() - poll_start < CDN_POLL_TIMEOUT:
                video_entry = await asyncio.to_thread(_tpx_api_get_video, token)
                if video_entry:
                    cdn_url = video_entry.get("file", "")
                    if cdn_url:
                        break
                log.info("[CDN] No video yet in API, retrying in 5s ...")
                await asyncio.sleep(5)

            if cdn_url:
                log.info("[Bot] CDN URL obtained: %s...%s",
                         cdn_url[:60], cdn_url[-40:] if len(cdn_url) > 40 else "")
                result["cdn_url"] = cdn_url
                result["status"] = "ok"
            else:
                log.error("[Bot] Could not get CDN URL after %ds polling", CDN_POLL_TIMEOUT)
                result["status"] = "failed_cdn"

            # Close page immediately in links-only mode (no need for enhanced page)
            return result

        else:
            # ── DOWNLOAD MODE: Full v6 behavior ──
            log.info("[Bot] Downloading enhanced video ...")
            downloaded = await tpx_download(page, save_dir, seg_idx,
                                            account["email"], account["password"])

            if downloaded and os.path.isfile(downloaded):
                log.info("[Bot] SEGMENT %d DONE -> %s (%.1f MB)",
                         seg_idx, downloaded, os.path.getsize(downloaded) / (1024 * 1024))
                result["file_path"] = downloaded
                result["status"] = "ok"

                # Also grab CDN URL for the output file
                try:
                    token = await asyncio.to_thread(_tpx_api_login, account["email"],
                                                    account["password"])
                    if token:
                        video_entry = await asyncio.to_thread(_tpx_api_get_video, token)
                        if video_entry:
                            result["cdn_url"] = video_entry.get("file", "")
                except Exception:
                    pass
            else:
                log.error("[Bot] SEGMENT %d FAILED at download", seg_idx)
                result["status"] = "failed_cdn"

            return result

    except Exception as e:
        log.error("[Bot] Segment %d error: %s", seg_idx, e)
        result["status"] = "failed_enhance"
        return result
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════
# MAIN  (v12: two modes, resume support, cdn_links.txt output)
# ═══════════════════════════════════════════════════════════════════════════

async def run(args):
    log.info("=" * 60)
    log.info("TensorPix Bot v12 -- CDN-First Video Enhancement Pipeline")
    log.info("=" * 60)

    # Set globals from CLI
    global BOOMLIFY_API_KEYS, CUSTOM_RESOLUTION, CUSTOM_MODEL, CUSTOM_SEGMENTS

    # Determine mode
    links_only = not args.download_enhanced
    resume_mode = args.resume

    log.info("Mode: %s", "CDN-ONLY (links)" if links_only else "FULL DOWNLOAD")
    if resume_mode:
        log.info("Resume: ON")

    # API keys
    if args.api_keys:
        BOOMLIFY_API_KEYS = [k.strip() for k in args.api_keys.split(",") if k.strip()]
        log.info("Using %d Boomlify API key(s) from --api-keys", len(BOOMLIFY_API_KEYS))
    elif args.boomlify_key:
        BOOMLIFY_API_KEYS = [args.boomlify_key]
        log.info("Using 1 Boomlify API key from --boomlify-key")
    else:
        env_key = os.environ.get("BOOMLIFY_API_KEY", "")
        if env_key:
            BOOMLIFY_API_KEYS = [env_key]
            log.info("Using 1 API key from BOOMLIFY_API_KEY env")
        else:
            BOOMLIFY_API_KEYS = [DEFAULT_BOOMLIFY_KEY]
            log.info("Using 1 API key (default)")

    # Custom resolution
    CUSTOM_RESOLUTION = args.resolution
    if CUSTOM_RESOLUTION:
        log.info("Custom resolution: %s", CUSTOM_RESOLUTION)

    # Custom model
    CUSTOM_MODEL = args.model
    if CUSTOM_MODEL:
        log.info("Custom model: %s", CUSTOM_MODEL)

    # Custom segment count
    CUSTOM_SEGMENTS = args.segments
    if CUSTOM_SEGMENTS:
        log.info("Custom segment count override: %d", CUSTOM_SEGMENTS)

    # Input file
    input_file = os.path.abspath(args.input)
    if not os.path.isfile(input_file):
        log.error("Input video not found: %s", input_file)
        log.info("Hint: Use --input to specify the input video file")
        sys.exit(1)

    work_dir = os.path.dirname(input_file)
    creds_file = args.accounts_file or os.path.join(work_dir, "tensorpix_accounts.txt")
    seg_dir = os.path.join(work_dir, "segments")
    save_dir = os.path.join(work_dir, "enhanced")
    output_path = os.path.abspath(args.output or os.path.join(work_dir, "cdn_links.txt"))
    state_path = os.path.join(work_dir, STATE_FILE)

    os.makedirs(seg_dir, exist_ok=True)
    os.makedirs(save_dir, exist_ok=True)

    # Load existing accounts
    accounts = load_accounts(creds_file)
    log.info("Found %d existing accounts", len(accounts))

    # Create more accounts if needed
    needed = args.count or 3
    if len(accounts) < needed:
        to_create = needed - len(accounts)
        log.info("Need %d more accounts (have %d, need %d) ...",
                 to_create, len(accounts), needed)

        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-dev-shm-usage"])

            created = 0
            for i in range(1, to_create + 1):
                result = await create_one_account(
                    browser, DEFAULT_REFERRAL, creds_file, len(accounts) + i)
                if result:
                    created += 1
                if i < to_create:
                    await asyncio.sleep(3)
            await browser.close()

        accounts = load_accounts(creds_file)
        log.info("Now have %d accounts (%d new)", len(accounts), created)

    if not accounts:
        log.error("No accounts available! Cannot proceed.")
        sys.exit(1)

    # Analyze input video
    info = get_video_info(input_file)
    if not info:
        log.error("Cannot read input video: %s", input_file)
        sys.exit(1)

    tier = res_tier(info["width"], info["height"])
    seg_dur = args.seg_seconds or SEG_DURATIONS.get(tier, 60)

    log.info("Input: %s (%.1fs, %dx%d, tier=%s)",
             os.path.basename(input_file), info["duration"],
             info["width"], info["height"], tier)
    log.info("Segment duration: %ds", seg_dur)
    log.info("Output: %s", output_path)
    log.info("Accounts: %d", len(accounts))

    # Split video
    segments = split_video(input_file, seg_dir, seg_dur)
    if not segments:
        log.error("Failed to split video")
        sys.exit(1)

    total = len(segments)
    log.info("Processing %d segment(s) ...", total)

    # Load state for resume
    state = {}
    if resume_mode:
        state = load_state(state_path)
        if state.get("segments"):
            log.info("[Resume] Found %d completed/skipped segment entries in state",
                     len(state["segments"]))

    # Process each segment
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"])

        segment_results: List[Dict[str, Any]] = []
        enhanced_files = []
        failed = []

        for idx, seg_path in enumerate(segments):
            seg_key = str(idx)

            # Resume: skip segments that already have CDN URLs
            if resume_mode and seg_key in state.get("segments", {}):
                prev = state["segments"][seg_key]
                prev_status = prev.get("status", "")
                prev_cdn = prev.get("cdn_url", "")

                if prev_status == "ok" and (prev_cdn or prev.get("file_path")):
                    log.info("[Resume] Skipping segment %d/%d (already done: %s)",
                             idx + 1, total, prev_status)
                    prev["seg_idx"] = idx
                    segment_results.append(prev)
                    if prev.get("file_path") and os.path.isfile(prev["file_path"]):
                        enhanced_files.append(prev["file_path"])
                    continue
                else:
                    log.info("[Resume] Segment %d/%d had status=%s, re-processing ...",
                             idx + 1, total, prev_status)

            acct = accounts[idx % len(accounts)]
            seg_result = await process_segment(
                browser, acct, seg_path, idx, total, save_dir, seg_dur,
                links_only=links_only)

            segment_results.append(seg_result)

            # Save state after each segment
            if "segments" not in state:
                state["segments"] = {}
            state["segments"][seg_key] = seg_result
            state["input_file"] = os.path.basename(input_file)
            state["total_segments"] = total
            state["mode"] = "cdn_only" if links_only else "download"
            save_state(state_path, state)

            # Track results
            if seg_result["status"] == "ok":
                if seg_result.get("file_path") and os.path.isfile(seg_result["file_path"]):
                    enhanced_files.append(seg_result["file_path"])
            else:
                failed.append(idx)

            if idx < total - 1:
                await asyncio.sleep(5)

        await browser.close()

    # Determine model name for output
    model_name = CUSTOM_MODEL or "4x Upscale Ultra 4"

    # Write CDN links file
    write_cdn_links(output_path, os.path.basename(input_file), total,
                    segment_results, model_name)

    # If download mode, merge enhanced files
    if not links_only and enhanced_files:
        # Build merged output name
        merged_output = os.path.join(work_dir, "enhanced_output.mp4")
        # If user specified --output and it doesn't end with .txt, use it
        if args.output and not args.output.endswith(".txt"):
            merged_output = os.path.abspath(args.output)

        log.info("=" * 60)
        log.info("Merging %d enhanced segments -> %s", len(enhanced_files), merged_output)
        merge_ok = merge_segments(enhanced_files, merged_output)
        if merge_ok:
            log.info("MERGE SUCCESS: %s (%.1f MB)",
                     merged_output, os.path.getsize(merged_output) / (1024 * 1024))
        else:
            log.error("MERGE FAILED")

    # Final report
    log.info("=" * 60)
    ok_count = sum(1 for r in segment_results if r["status"] == "ok")
    cdn_count = sum(1 for r in segment_results if r.get("cdn_url"))
    dl_count = sum(1 for r in segment_results if r.get("file_path"))
    log.info("RESULTS: %d/%d segments OK", ok_count, total)
    if cdn_count > 0:
        log.info("CDN URLs obtained: %d/%d", cdn_count, total)
    if dl_count > 0:
        log.info("Files downloaded: %d/%d", dl_count, total)
    if failed:
        log.warning("Failed segments: %s", [f + 1 for f in failed])
    log.info("Output: %s", output_path)
    log.info("=" * 60)

    if not links_only and enhanced_files:
        if merge_segments(enhanced_files,
                          os.path.join(work_dir, "enhanced_output.mp4")):
            merged_path = os.path.join(work_dir, "enhanced_output.mp4")
            log.info("SUCCESS! Enhanced video: %s (%.1f MB)",
                     merged_path, os.path.getsize(merged_path) / (1024 * 1024))
    elif cdn_count > 0:
        log.info("SUCCESS! %d CDN URLs saved to %s", cdn_count, output_path)
    else:
        log.error("FAILED: No segments were enhanced")


def main():
    parser = argparse.ArgumentParser(
        description="TensorPix Video Upscaler Bot v12 (CDN-First)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # CDN links only (DEFAULT - fast, no download):
  python3 tensorpix-bot-v12.py --input input.mkv

  # Full download + merge (v6 behavior):
  python3 tensorpix-bot-v12.py --input input.mkv --download-enhanced

  # Resume from previous run:
  python3 tensorpix-bot-v12.py --input input.mkv --resume

  # Custom API key:
  python3 tensorpix-bot-v12.py --input input.mkv --boomlify-key api_xxx

  # With multiple API keys (comma-separated, cycles on failure):
  python3 tensorpix-bot-v12.py --input input.mkv --api-keys "key1,key2,key3"

  # Override segment count (split into exactly 4 segments):
  python3 tensorpix-bot-v12.py --input input.mkv --segments 4

  # Custom resolution + model (bypasses default preset):
  python3 tensorpix-bot-v12.py --input input.mkv --resolution 2160p --model animation

  # Full download with all options:
  python3 tensorpix-bot-v12.py --input input.mkv --count 5 --segments 6 --resolution 2160p --model animation --download-enhanced --resume

  # Custom output filename:
  python3 tensorpix-bot-v12.py --input input.mkv --output my_links.txt
        """)

    parser.add_argument("--input", default="input.mkv",
                        help="Input video file (default: input.mkv)")
    parser.add_argument("--output", default=None,
                        help="Output file (default: cdn_links.txt, or enhanced_output.mp4 with --download-enhanced)")
    parser.add_argument("--count", type=int, default=3,
                        help="Number of accounts to create/use (default: 3)")
    parser.add_argument("--seg-seconds", type=int, default=None,
                        help="Override segment duration in seconds")
    parser.add_argument("--segments", type=int, default=None,
                        help="Override auto-split: create exactly N segments")
    parser.add_argument("--resolution", type=str, default=None,
                        help="Target resolution e.g. 2160p, 1080p, 1440p")
    parser.add_argument("--model", type=str, default=None,
                        help="Enhancement model e.g. animation, general")
    parser.add_argument("--api-keys", type=str, default=None,
                        help="Comma-separated Boomlify API keys (cycles on failure)")
    parser.add_argument("--boomlify-key", type=str, default=None,
                        help="Single Boomlify API key override")
    parser.add_argument("--accounts-file", default=None,
                        help="Path to accounts file (default: ./tensorpix_accounts.txt)")

    # v12 new flags
    parser.add_argument("--links-only", action="store_true", default=True,
                        help="CDN-links only mode (default: True). Only collects CDN URLs, no download.")
    parser.add_argument("--download-enhanced", action="store_true", default=False,
                        help="Full download mode. Downloads enhanced files and merges them. Overrides --links-only.")
    parser.add_argument("--resume", action="store_true", default=False,
                        help="Resume from previous run. Skips segments that already have CDN URLs or downloads.")

    args = parser.parse_args()

    # Normalize: --download-enhanced overrides --links-only
    # (links_only is computed in run() from args.download_enhanced)

    # Add log file
    input_abs = os.path.abspath(args.input)
    log_dir = os.path.dirname(input_abs)
    _add_log_file(os.path.join(log_dir, "tensorpix_v12.log"))

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
