# Running Separate (Isolated) Servers — One Per Person

By default, everyone who logs into a single server shares **one** workspace:
the same config, the same Chrome/Facebook sessions, the same bot, and the same
results. Two logins there = two keys to the **same room**.

If you want to give people **separate, isolated** environments — each with its
own config, its own Chrome, its own bot, its own recovered accounts, and its own
kill switch — run **one instance per person**. This guide shows how.

---

## What "isolated" means here

| | Shared server (default) | Separate instances |
|---|---|---|
| Login accounts / disable | separate | separate |
| Config, target URL, Hero SMS login | **shared** | **separate** |
| Chrome + Facebook sessions | **shared** | **separate** |
| The bot (Start/Stop) | **shared, one run** | **separate, one each** |
| Recovered accounts, stats, logs | **shared** | **separate** |
| Kill switch | one, global | one **per instance** |

Each instance is a full copy of the app in its own folder, on its own port,
with its own Chrome (its own remote-debugging port and profile folder).

---

## One-time setup on the server

Install the dependencies once (they are shared by all instances):

```bat
pip install -r requirements.txt
python -m playwright install chromium
```

---

## Create an instance (repeat per person)

Double-click **`new_instance.bat`** (or run `powershell -File new_instance.ps1`).
It asks for:

- **Instance name** — e.g. `client1`
- **Web port** — e.g. `5001` (use a different port for each: 5001, 5002, 5003 …)

It creates `instances\<name>\` with its own copy of the code and a ready-to-run
`start.bat`.

Example — two customers:

```
new_instance.bat  ->  name: client1   port: 5001
new_instance.bat  ->  name: client2   port: 5002
```

You get:

```
instances\client1\start.bat   -> server on port 5001, its own everything
instances\client2\start.bat   -> server on port 5002, its own everything
```

---

## Start an instance

Double-click `instances\<name>\start.bat`. It launches that instance on its
port, reachable at `http://<server-ip>:<port>`.

First time in each instance:

1. Log in with the default owner account: **`admin` / `admin`**
2. **Change the owner password immediately** (click "admin" → Change Password).
3. Click **Chrome/Launch** in the dashboard and log that instance's Chrome into
   the Facebook + Hero SMS accounts it should use. Those sessions stay inside
   this instance only.
4. Create a login for the person (**Users** → Add), give them the URL + login.

---

## Giving access and cutting it off

For each person you have **their** instance. On that instance you are the owner:

- **Disable one user:** Users panel → Disable.
- **Suspend that whole instance:** the **Service** button (kill switch) — it only
  affects that instance; other instances keep running.
- **Cut them off completely:** stop that instance's `start.bat`.

Because instances are separate, suspending or stopping `client1` has **no
effect** on `client2`.

---

## Notes / gotchas

- **Different port per instance.** Two instances cannot share a port.
- **Each instance launches its own Chrome** (auto-picks debug ports 9222, 9223 …
  and its own `chrome_profiles_hero` folder). The dashboard's "Kill Chrome"
  button only closes **that instance's** Chrome, not the others.
- **RAM.** Each running Chrome + bot uses memory. Don't run many instances on a
  tiny box — budget ~1–2 GB per active instance.
- **Facebook IP.** All instances on one machine share that machine's IP. If you
  are recovering many accounts, consider per-instance proxies (set inside each
  instance's config / Chrome).
- **Updating the code** later: re-run `new_instance.bat` with the same name — it
  refreshes the code files and leaves that instance's accounts/config/results
  untouched.
- **Never give people the server files or RDP/SSH** — only the URL + login. File
  access lets someone bypass the login entirely.
