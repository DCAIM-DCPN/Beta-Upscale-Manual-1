#!/usr/bin/env python3
import asyncio, json, logging, os, re, sys, time, urllib.parse, urllib.request
from playwright.async_api import async_playwright

# Configuration
BOOMLIFY_API_KEY = "api_6e99a3e4e22d3fe379f4f5185b3ff3480b7d1325b5a46f79801bcae5af433410"
BOOMLIFY_BASE = "https://v1.boomlify.com"
PASSWORD = "TpixAcc2026!"
TENSORPIX = "https://app.tensorpix.ai"

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
log = logging.getLogger("tensorpix_bot")

def _bheaders():
    return {"X-API-Key": BOOMLIFY_API_KEY, "Content-Type": "application/json", "Accept": "application/json"}

async def create_inbox():
    url = f"{BOOMLIFY_BASE}/api/v1/emails/create"
    data = json.dumps({"time": "10min"}).encode()
    req = urllib.request.Request(url, method="POST", data=data, headers=_bheaders())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read())
            eo = res.get("email", {})
            return eo.get("id"), eo.get("address")
    except Exception as e:
        log.error("Inbox creation failed: %s", e)
        return None, None

async def poll_verify_link(inbox_id, timeout=600): # Increased timeout to 10 mins
    url = f"{BOOMLIFY_BASE}/api/v1/emails/{urllib.parse.quote(inbox_id)}/messages"
    req = urllib.request.Request(url, headers=_bheaders())
    start = time.time()
    log.info("Polling for verification email (ID: %s)...", inbox_id)
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
                msgs = data.get("messages") or data.get("data") or []
                for msg in reversed(msgs):
                    if "tensorpix" in str(msg.get("from_email", "")).lower():
                        msg_id = msg.get("id")
                        detail_url = f"{BOOMLIFY_BASE}/api/v1/emails/{urllib.parse.quote(inbox_id)}/messages/{msg_id}"
                        detail_req = urllib.request.Request(detail_url, headers=_bheaders())
                        with urllib.request.urlopen(detail_req, timeout=10) as dr:
                            detail = json.loads(dr.read())
                            body = detail.get("body_html") or detail.get("body_text") or ""
                            # Improved regex for link extraction
                            m = re.search(r'https://app\.tensorpix\.ai/verify-user/[a-zA-Z0-9\-\._~%]+', body)
                            if m: 
                                link = m.group(0)
                                log.info("Found verification link: %s", link)
                                return link
        except Exception as e:
            log.debug("Poll error: %s", e)
        await asyncio.sleep(10)
    return None

async def enhance_video(input_path, output_path):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        
        # 1. Create Account
        log.info("Creating fresh account...")
        inbox_id, email = await create_inbox()
        if not email: return False
        
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()
        
        try:
            log.info("Registering %s...", email)
            await page.goto(f"{TENSORPIX}/register", timeout=60000)
            await page.locator('input[type="email"]').fill(email)
            await page.locator('input[type="password"]').fill(PASSWORD)
            await page.locator('button:has-text("Create account")').click()
            await page.wait_for_timeout(5000)
            
            if "throttled" in (await page.content()).lower():
                log.error("Registration throttled. IP might be blocked.")
                return False
            
            vlink = await poll_verify_link(inbox_id)
            if not vlink:
                log.error("Verification link timeout")
                return False
            
            log.info("Verifying account...")
            await page.goto(vlink, timeout=60000)
            await page.wait_for_timeout(10000)
            
            # 2. Login
            log.info("Logging in...")
            await page.goto(f"{TENSORPIX}/login", timeout=60000)
            await page.locator('input[type="email"]').fill(email)
            await page.locator('input[type="password"]').fill(PASSWORD)
            await page.locator('button:has-text("Sign in")').click()
            await page.wait_for_url("**/videos", timeout=60000)
            
            # 3. Upload
            log.info("Uploading %s...", input_path)
            file_input = page.locator('input[type="file"]').first
            await file_input.set_input_files(input_path)
            
            filename = os.path.basename(input_path)
            # Wait for upload to complete (filename appears in list)
            row = page.locator(f"div:has-text('{filename}')").first
            await row.wait_for(state="visible", timeout=300000)
            
            enhance_btn = row.locator("button:has-text('Enhance')")
            await enhance_btn.wait_for(state="visible", timeout=300000)
            await enhance_btn.click()
            
            # 4. Configure (2x Upscale Ultra 3)
            log.info("Configuring enhancement...")
            await page.locator('button:has-text("Manual")').click()
            await page.locator('div:has-text("2x Upscale Ultra 3")').first.click()
            await page.locator('button:has-text("Enhance")').last.click()
            
            # 5. Wait & Download
            log.info("Waiting for enhancement to complete...")
            while True:
                await page.goto(f"{TENSORPIX}/videos/enhanced", timeout=60000)
                await page.wait_for_timeout(10000)
                
                dl_btn = page.locator(f"div:has-text('{filename}') button:has-text('Download')").first
                if await dl_btn.is_visible():
                    log.info("Enhancement complete! Downloading...")
                    async with page.expect_download(timeout=120000) as download_info:
                        await dl_btn.click()
                    download = await download_info.value
                    await download.save_as(output_path)
                    log.info("Saved to %s", output_path)
                    return True
                
                content = await page.content()
                if "Processing" in content:
                    log.info("Status: Processing...")
                elif "Queue" in content:
                    log.info("Status: In Queue...")
                else:
                    log.info("Status: Waiting...")
                    
                await asyncio.sleep(60)
                
        except Exception as e:
            log.error("An error occurred: %s", e)
            await page.screenshot(path="error_debug.png")
            return False
        finally:
            await browser.close()

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 tensorpix_final.py <input> <output>")
        sys.exit(1)
    asyncio.run(enhance_video(sys.argv[1], sys.argv[2]))
