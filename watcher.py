#!/usr/bin/env python3
"""
Money SMP leaderboard watcher (runs inside GitHub Actions).

Why a browser: smp.money sits behind Vercel's bot check, so plain HTTP
requests get a 429 "verifying your browser" page. This script opens the
leaderboard once in Chrome (Playwright) to pass the check, then every
30 seconds calls the same internal API the page uses, from inside the page.

Every check:
  1. fetch leaderboards.getCurrentState
  2. compare it with the previous copy (data/latest.json)
  3. append any differences to data/changes.json
  4. save the new copy as data/latest.json
Then it commits data/ to the repo: within a minute of any change, and a
heartbeat every 2 minutes otherwise (so the files always show a fresh
checkedAt time). Change commits are titled with the money moves.

Env:
  RUN_MINUTES   how long to watch before exiting (default 340, job max is 360)
  POLL_SECONDS  seconds between checks (default 30)
  CHAIN         "yes" = start the next run when this one ends (default yes)
  NO_GIT        "1" = never commit/push (local testing)
  HEARTBEAT_SECONDS  push the latest check time at least this often (default 120)
"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

PAGE_URL = "https://www.smp.money/leaderboard/current"
API_PATH = (
    "/api/public/leaderboards.getCurrentState?batch=1&input="
    "%7B%220%22%3A%7B%22json%22%3Anull%2C%22meta%22%3A%7B%22values%22%3A%5B%22undefined%22%5D%2C%22v%22%3A1%7D%7D%7D"
)
API_URL = "https://www.smp.money" + API_PATH

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(REPO_DIR, "data")
LATEST = os.path.join(DATA_DIR, "latest.json")
CHANGES = os.path.join(DATA_DIR, "changes.json")
STATUS = os.path.join(DATA_DIR, "status.json")

RUN_MINUTES = float(os.environ.get("RUN_MINUTES", "340") or 340)
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "30") or 30)
CHAIN = (os.environ.get("CHAIN", "yes") or "yes").lower() in ("yes", "true", "1")
NO_GIT = os.environ.get("NO_GIT") == "1"
PUSH_MIN_GAP = 60          # at most one data commit per minute
HEARTBEAT_EVERY = float(os.environ.get("HEARTBEAT_SECONDS", "120") or 120)

IST = timezone(timedelta(hours=5, minutes=30))
TEAM_NAMES = {
    "PINK_TEAM": "Pink Hearts",
    "PURPLE_TEAM": "Purple Aces",
    "BLUE_TEAM": "Blue Diamonds",
    "GREEN_TEAM": "Green Clubs",
    "YELLOW_TEAM": "Yellow Spades",
    "RED_TEAM": "Red Jokers",
}
# Player fields worth logging. playtime and online flip constantly, so they
# are kept in latest.json but never logged as changes.
TRACKED = ("money", "kills", "deaths", "team", "captain", "tier", "vip")

STOP = False


def log(msg):
    print(datetime.now(IST).strftime("%H:%M:%S IST"), msg, flush=True)


def now_pair():
    t = datetime.now(timezone.utc)
    return t.isoformat(timespec="seconds").replace("+00:00", "Z"), t.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- compare

def ranked(players):
    """Rank by money (desc). Ties keep the site's order."""
    order = sorted(range(len(players)), key=lambda i: (-(players[i].get("money") or 0), i))
    return {players[i]["id"]: r + 1 for r, i in enumerate(order)}


def team_totals(players):
    totals = {}
    for p in players:
        t = p.get("team") or "NONE"
        totals.setdefault(t, {"team": t, "name": TEAM_NAMES.get(t, t), "money": 0, "players": 0})
        totals[t]["money"] += p.get("money") or 0
        totals[t]["players"] += 1
    return sorted(totals.values(), key=lambda x: -x["money"])


def diff(prev, cur, at, at_ist):
    """Return a list of change records between two payloads."""
    out = []
    if not prev:
        return out
    pp = {p["id"]: p for p in prev.get("players", []) if p.get("id")}
    cp = {p["id"]: p for p in cur.get("players", []) if p.get("id")}
    pr, cr = ranked(prev.get("players", [])), ranked(cur.get("players", []))

    for pid, c in cp.items():
        p = pp.get(pid)
        base = {"at": at, "atIST": at_ist, "player": c.get("name"), "id": pid,
                "team": TEAM_NAMES.get(c.get("team"), c.get("team"))}
        if p is None:
            out.append({**base, "type": "joined", "money": c.get("money"), "rank": cr.get(pid)})
            continue
        fields = {f: {"from": p.get(f), "to": c.get(f)} for f in TRACKED if p.get(f) != c.get(f)}
        if p.get("name") != c.get("name"):
            fields["name"] = {"from": p.get("name"), "to": c.get("name")}
        if not fields:
            continue
        rec = {**base}
        if "money" in fields:
            delta = (c.get("money") or 0) - (p.get("money") or 0)
            rec.update({"type": "money", "from": p.get("money"), "to": c.get("money"), "delta": delta,
                        "rankFrom": pr.get(pid), "rankTo": cr.get(pid)})
            flags = []
            if abs(delta) >= 50:
                flags.append("big_move")
            if not p.get("online") and not c.get("online"):
                flags.append("while_offline")
            if pr.get(pid) and cr.get(pid) and abs(pr[pid] - cr[pid]) >= 5:
                flags.append("rank_jump")
            if flags:
                rec["flags"] = flags
            other = {k: v for k, v in fields.items() if k != "money"}
            if other:
                rec["also"] = other
        else:
            rec.update({"type": "stats", "fields": fields})
        out.append(rec)

    for pid, p in pp.items():
        if pid not in cp:
            out.append({"at": at, "atIST": at_ist, "type": "left", "player": p.get("name"), "id": pid,
                        "team": TEAM_NAMES.get(p.get("team"), p.get("team")), "money": p.get("money")})

    def ev(x):
        e = x.get("currentEvent")
        return e.get("name") if isinstance(e, dict) else e

    ev_fields = {}
    for key, a, b in (("isLive", prev.get("isLive"), cur.get("isLive")),
                      ("currentEvent", ev(prev), ev(cur)),
                      ("eventFinished", prev.get("eventFinished"), cur.get("eventFinished")),
                      ("eventName", prev.get("eventName"), cur.get("eventName"))):
        if a != b:
            ev_fields[key] = {"from": a, "to": b}
    if ev_fields:
        out.append({"at": at, "atIST": at_ist, "type": "event", "fields": ev_fields})

    # money changes first, biggest moves first
    out.sort(key=lambda r: (r["type"] != "money", -abs(r.get("delta") or 0)))
    return out


# ---------------------------------------------------------------- files

def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj, one_per_line=False):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        if one_per_line and isinstance(obj, list):
            # one record per line keeps git diffs tiny (appends only)
            f.write("[\n" + ",\n".join(json.dumps(x, ensure_ascii=False) for x in obj) + ("\n]\n" if obj else "]\n"))
        else:
            json.dump(obj, f, indent=2, ensure_ascii=False)
            f.write("\n")
    os.replace(tmp, path)


def save_latest(payload, at, at_ist, check_no, last_change=None):
    players = payload.get("players", [])
    ranks = ranked(players)
    write_json(LATEST, {
        "checkedAt": at,
        "checkedAtIST": at_ist,
        "check": check_no,
        "lastChangeAt": (last_change or {}).get("at"),
        "lastChangeAtIST": (last_change or {}).get("atIST"),
        "source": API_URL,
        "teams": team_totals(players),
        "board": sorted(({"rank": ranks[p["id"]], "name": p.get("name"), "money": p.get("money"),
                          "team": TEAM_NAMES.get(p.get("team"), p.get("team"))} for p in players if p.get("id")),
                        key=lambda x: x["rank"]),
        "payload": payload,  # exact copy of what the API returned
    })


# ---------------------------------------------------------------- git

def git(*args, check=False):
    return subprocess.run(["git", *args], cwd=REPO_DIR, capture_output=True, text=True, check=check)


def push(message):
    if NO_GIT:
        return True
    git("add", "data")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return True
    git("commit", "-q", "-m", message)
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    for attempt in range(4):
        r = git("push", "-q", "origin", "HEAD:" + branch)
        if r.returncode == 0:
            return True
        log("push failed, rebasing: " + (r.stderr or "").strip()[:200])
        git("fetch", "-q", "origin", branch)
        git("rebase", "-q", "origin/" + branch)
        time.sleep(3 * (attempt + 1))
    return False


def commit_message(new_changes, at_ist):
    money = [c for c in new_changes if c["type"] == "money"]
    hhmm = at_ist[11:16]
    if not money:
        kinds = sorted({c["type"] for c in new_changes})
        return f"{hhmm} IST: {len(new_changes)} change(s) ({', '.join(kinds)})" if new_changes else f"{hhmm} IST: heartbeat"
    parts = [f"{c['player']} {c['delta']:+d} (${c['to']})" for c in money[:8]]
    more = f" +{len(money) - 8} more" if len(money) > 8 else ""
    return f"{hhmm} IST: " + ", ".join(parts) + more


def start_next_run():
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    wf = os.environ.get("WORKFLOW_FILE", "watch.yml")
    if NO_GIT or not repo or not token:
        return
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/dispatches",
        data=json.dumps({"ref": os.environ.get("GITHUB_REF_NAME", "main")}).encode(),
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            log(f"queued the next run (HTTP {r.status})")
    except Exception as e:
        log(f"could not queue next run: {e} (the hourly schedule will start one)")


# ---------------------------------------------------------------- browser

FETCH_JS = """async (path) => {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 20000);
  try {
    const r = await fetch(location.origin + path, {credentials: 'include', cache: 'no-store', signal: ctrl.signal});
    return {status: r.status, text: await r.text()};
  } catch (e) {
    return {status: 0, text: String(e)};
  } finally { clearTimeout(t); }
}"""


class Browser:
    def __init__(self, pw):
        self.pw = pw
        self.browser = None
        self.page = None
        self.mode = None

    def launch(self, headless):
        args = ["--disable-blink-features=AutomationControlled", "--disable-gpu", "--mute-audio",
                "--disable-extensions", "--no-first-run"]
        try:
            b = self.pw.chromium.launch(channel="chrome", headless=headless, args=args)
        except Exception as e:
            log(f"system Chrome not usable ({str(e)[:120]}), falling back to Playwright Chromium")
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
            b = self.pw.chromium.launch(headless=headless, args=args)
        major = (b.version or "131").split(".")[0]
        ua = f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
        ctx = b.new_context(user_agent=ua, viewport={"width": 1280, "height": 800}, locale="en-US")
        ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        self.browser, self.page = b, ctx.new_page()
        self.mode = "headless" if headless else "headed"
        log(f"Chrome {b.version} started ({self.mode})")

    def close(self):
        try:
            if self.browser:
                self.browser.close()
        except Exception:
            pass
        self.browser = self.page = None

    def fetch(self):
        try:
            return self.page.evaluate(FETCH_JS, API_PATH)
        except Exception as e:  # page navigating (challenge reload) etc.
            return {"status": -1, "text": str(e)[:200]}

    def pass_check(self, wait_s=90):
        """Open the leaderboard so Vercel's check runs, then wait until the API answers."""
        try:
            self.page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log(f"open page: {str(e)[:150]}")
        end = time.time() + wait_s
        while time.time() < end and not STOP:
            r = self.fetch()
            if r["status"] == 200:
                # park on the plain JSON URL so the site's own 30 s polling and
                # React rendering stop; our fetch stays same-origin.
                try:
                    self.page.goto(API_URL, wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
                return True
            time.sleep(3)
        return False

    def ensure(self):
        """Start a browser that has passed the check. Tries headless, then headed (needs xvfb)."""
        modes = [True, False] if os.environ.get("DISPLAY") else [True]
        for headless in modes:
            self.close()
            self.launch(headless)
            if self.pass_check():
                log(f"passed the site's browser check ({self.mode})")
                return True
            log(f"browser check did not pass in {self.mode} mode")
        return False


# ---------------------------------------------------------------- main

def main():
    global STOP
    os.makedirs(DATA_DIR, exist_ok=True)
    if not NO_GIT:
        git("config", "user.name", "smp-watch-bot")
        git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")

    def handle(sig, _frm):
        global STOP
        STOP = True
        log(f"signal {sig}, finishing up")
    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)

    prev_doc = read_json(LATEST, None)
    prev = prev_doc.get("payload") if isinstance(prev_doc, dict) else None
    changes = read_json(CHANGES, [])
    if not os.path.exists(CHANGES):
        write_json(CHANGES, changes, one_per_line=True)
    status = read_json(STATUS, {})
    started_at, started_ist = now_pair()
    status.update({
        "runStartedAt": started_at, "runStartedAtIST": started_ist,
        "runId": os.environ.get("GITHUB_RUN_ID"), "pollSeconds": POLL_SECONDS,
        "checksThisRun": 0, "errorsThisRun": 0, "state": "starting",
    })
    status.setdefault("totalChecks", 0)
    status.setdefault("totalChanges", len(changes))

    from playwright.sync_api import sync_playwright

    deadline = time.time() + RUN_MINUTES * 60
    pending, last_push, last_beat = [], 0.0, time.time()
    healthy = False
    force_push = False
    with sync_playwright() as pw:
        br = Browser(pw)
        if not br.ensure():
            status.update({"state": "blocked", "lastError": "could not pass the site's browser check"})
            write_json(STATUS, status)
            push("watcher: blocked by the site's browser check")
            log("giving up this run: browser check never passed")
            sys.exit(1)

        next_tick = time.time()
        fails_in_row = 0
        while not STOP and time.time() < deadline:
            r = br.fetch()
            at, at_ist = now_pair()
            payload = None
            if r["status"] == 200:
                try:
                    payload = json.loads(r["text"])[0]["result"]["data"]["json"]
                except Exception as e:
                    r = {"status": -2, "text": f"bad json: {e}"}
            if payload is None:
                fails_in_row += 1
                status["errorsThisRun"] += 1
                status["lastError"] = f"{at_ist} HTTP {r['status']}: {r['text'][:120]}"
                log(f"check failed: HTTP {r['status']} {r['text'][:100]}")
                if fails_in_row >= 2 or r["status"] in (403, 429):
                    log("re-running the browser check")
                    if not br.ensure():
                        time.sleep(30)
            else:
                fails_in_row = 0
                healthy = True
                status["checksThisRun"] += 1
                status["totalChecks"] += 1
                new = diff(prev, payload, at, at_ist)
                if prev is None:
                    log(f"baseline saved: {len(payload.get('players', []))} players")
                    force_push = True
                    if not os.path.exists(CHANGES):
                        write_json(CHANGES, changes, one_per_line=True)
                if new:
                    changes.extend(new)
                    pending.extend(new)
                    status["totalChanges"] = len(changes)
                    status["lastChangeAt"], status["lastChangeAtIST"] = at, at_ist
                    write_json(CHANGES, changes, one_per_line=True)
                    for c in new:
                        if c["type"] == "money":
                            log(f"  {c['player']}: ${c['from']} -> ${c['to']} ({c['delta']:+d}) rank {c['rankFrom']}->{c['rankTo']} {' '.join(c.get('flags', []))}")
                        else:
                            log(f"  {c['type']}: {c.get('player', '')} {json.dumps(c.get('fields', ''))[:150]}")
                save_latest(payload, at, at_ist, status["totalChecks"],
                            {"at": status.get("lastChangeAt"), "atIST": status.get("lastChangeAtIST")})
                prev = payload
                status.update({"state": "watching", "lastCheckAt": at, "lastCheckAtIST": at_ist,
                               "players": len(payload.get("players", [])), "browserMode": br.mode})
                log(f"check #{status['checksThisRun']} ok, {len(new)} change(s)")

            now = time.time()
            if force_push or (pending and now - last_push >= PUSH_MIN_GAP):
                write_json(STATUS, status)
                msg = commit_message(pending, now_pair()[1]) if pending else f"{now_pair()[1][11:16]} IST: baseline"
                if push(msg):
                    pending, last_push, last_beat, force_push = [], now, now, False
            elif now - last_beat >= HEARTBEAT_EVERY:
                write_json(STATUS, status)
                push(f"{now_pair()[1][11:16]} IST: checked, no change ({status['checksThisRun']} checks this run)")
                last_beat = now

            next_tick += POLL_SECONDS
            if next_tick < time.time():  # fell behind (slow re-check); skip missed ticks
                next_tick = time.time() + POLL_SECONDS
            while not STOP and time.time() < next_tick:
                time.sleep(min(1.0, next_tick - time.time()))

        br.close()

    status["state"] = "handing over" if (CHAIN and healthy) else "stopped"
    status["runEndedAt"], status["runEndedAtIST"] = now_pair()
    write_json(STATUS, status)
    msg = commit_message(pending, now_pair()[1]) if pending else f"{now_pair()[1][11:16]} IST: run ended after {status['checksThisRun']} checks"
    push(msg)
    if CHAIN and healthy and not STOP:
        start_next_run()
    log(f"done: {status['checksThisRun']} checks, {status['errorsThisRun']} errors")


if __name__ == "__main__":
    main()
