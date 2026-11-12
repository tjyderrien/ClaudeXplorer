#!/usr/bin/env python3
"""claude-explorer — find, triage and dispatch Claude Code work across your machines.

Hosts are discovered with `tailscale status --json` (or given with --hosts). Each Linux host is read
over SSH by piping a small stdlib-only Python script to `python3 -` (nothing is installed remotely);
the local machine is read directly.

    claude-explorer               Claude Code sessions on every host, grouped by how to finish them
    claude-explorer todo          ... as Todoist tasks, with points for finishing them
    claude-explorer dispatch      hand Todoist tasks labelled @claude to Claude Code agents
    claude-explorer videos        gather the screen recordings of a terminal into one folder
    claude-explorer move          move Claude Code sessions (and their folders) to another host

Required Notice: Copyright 2026 Thibault JY Derrien (https://github.com/tjyderrien)
Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE.md); commercial use needs a separate license.
"""
import argparse
import base64
import calendar
import concurrent.futures as cf
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

CONFIG_PATH = os.path.expanduser("~/.config/claude-explorer.json")
LEGACY_CONFIG = os.path.expanduser("~/.config/tailmon.json")  # read first; CONFIG_PATH overrides it
STATE_DIR = os.path.expanduser("~/.local/state/claude-explorer")
LEGACY_STATE = os.path.expanduser("~/.local/state/tailmon")
DEFAULT_CONFIG = {
    # SSH user per hostname; hosts not listed use "default_user"
    # (null: whatever ssh picks, i.e. ~/.ssh/config or your local username).
    "users": {},
    "default_user": None,
    # SSH private key passed with -i; null: ssh's usual keys / agent.
    "identity": None,
    # Hostnames never contacted.
    "exclude": [],
    # Regexes on a session's folder or first request; matching sessions are hidden.
    "claude_exclude": [],
    # `todo`: Todoist API token (or set TODOIST_API_TOKEN) and the project its tasks go to.
    "todoist_token": None,
    "todoist_project": "Claude sessions",
    # Claude accounts (e.g. seats of a Team plan): name -> config dir, logged in once per host with
    # `CLAUDE_CONFIG_DIR=<dir> claude`. ~/.claude is always included, as "default" unless named here.
    "claude_accounts": {},
    # `dispatch`: Todoist label and project that hand a task to an agent, and how many agents may run at once.
    "dispatch_label": "claude",
    "dispatch_project": "Claude-Tasks",
    "dispatch_max": 3,  # per account
    # Hours an account is left alone after one of its agents hit the usage limit.
    "dispatch_rest_hours": 1.0,
    # Hosts where agents get a restricted shell: hostname -> true (SHELL_COMMANDS) or a list of command
    # prefixes. Elsewhere agents have no shell at all.
    "dispatch_shell": {},
    # Give a task the due date "today" when its report is posted (unless it already has one), so it shows
    # up in Todoist's Today view and reminders.
    "dispatch_due_today": True,
}
# `dispatch_shell: true`: SLURM plus read-only file commands and harmless setup.
SHELL_COMMANDS = ["sbatch", "squeue", "sacct", "sinfo", "scontrol show job",
                  "ls", "cat", "head", "tail", "grep", "wc", "diff", "mkdir", "cp", "scp"]
# Refused even if a prefix above would let them through.
SHELL_DENY = ["rm", "mv", "scancel", "sudo", "ssh", "curl", "wget", "chmod", "git push"]


# ---------------------------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------------------------
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    for path in (LEGACY_CONFIG, CONFIG_PATH):
        if os.path.exists(path):
            with open(path) as f:
                cfg.update({k: v for k, v in json.load(f).items() if k in DEFAULT_CONFIG})
    return cfg


def shell_for(cfg, host):
    """Command prefixes an agent may run on host; empty: no shell."""
    allowed = {k.lower(): v for k, v in cfg["dispatch_shell"].items()}.get(host.lower())
    return list(SHELL_COMMANDS) if allowed is True else list(allowed or [])


def migrate_state():
    """Copy todo/dispatch state written by `tailmon todo|dispatch` the first time."""
    if os.path.isdir(STATE_DIR) or not os.path.isdir(LEGACY_STATE):
        return
    os.makedirs(STATE_DIR)
    for name in ("todo.json", "dispatch.json"):
        if os.path.exists(os.path.join(LEGACY_STATE, name)):
            shutil.copy2(os.path.join(LEGACY_STATE, name), os.path.join(STATE_DIR, name))


def discover():
    """Return (self_node, [peer nodes]) from tailscale; each node is a dict."""
    st = json.loads(subprocess.run(["tailscale", "status", "--json"], check=True,
                                   stdout=subprocess.PIPE).stdout)

    def node(p):
        return {"name": p["DNSName"].split(".")[0] or p["HostName"], "dns": p["DNSName"].rstrip("."),
                "ip": (p.get("TailscaleIPs") or [None])[0], "os": p.get("OS", ""), "online": p.get("Online", False)}
    return node(st["Self"]), [node(p) for p in (st.get("Peer") or {}).values()]


def targets_for(cfg, only=None):
    """Return ([(node, is_self)], [skipped nodes])."""
    try:
        me, peers = discover()
    except (OSError, subprocess.CalledProcessError, ValueError):
        if not only:
            sys.exit("claude-explorer: tailscale isn't available; name the hosts with --hosts")
        # Without Tailscale: plain ssh names, as in ~/.ssh/config.
        local = os.uname().nodename.split(".")[0]
        return [({"name": h, "dns": None, "ip": None, "os": "linux", "online": True}, h == local)
                for h in sorted(only)], []
    targets, skipped = [(me, True)], []
    for p in peers:
        if only and p["name"] not in only:
            continue
        if p["name"] in cfg["exclude"]:
            continue
        if p["os"] != "linux" or not p["online"]:
            skipped.append(p)
            continue
        targets.append((p, False))
    if only:
        targets = [t for t in targets if t[0]["name"] in only]
    return targets, skipped


COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(text, pct):
    if not COLOR or pct is None:
        return text
    code = "31" if pct >= 90 else "33" if pct >= 70 else "32"
    return "\033[%sm%s\033[0m" % (code, text)


def bar(pct, width=10):
    filled = int(round(pct / 100.0 * width))
    return c("\u2588" * filled + "\u00b7" * (width - filled), pct)


# ---------------------------------------------------------------------------------------------
# Claude Code sessions: a stdlib-only worker summarizes ~/.claude on each host; triage happens here.
# ---------------------------------------------------------------------------------------------
CLAUDE_AGENT = r'''
import glob, json, os, re, subprocess, sys, time

DAYS = float(sys.argv[1])
CONFIGS = sys.argv[2:] or ["~/.claude"]
HOME = os.path.expanduser("~")
NOISE = ("<command", "<local-command", "<system", "<bash-", "<task-notification",
         "[Request interrupted", "Caveat:", "This session is being continued")

def text_of(row):
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return content, False, False
    parts, has_tool_use, has_tool_result = [], False, False
    for block in content or []:
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text", ""))
        elif kind == "tool_use":
            has_tool_use = True
        elif kind == "tool_result":
            has_tool_result = True
    return "\n".join(parts), has_tool_use, has_tool_result

def live_sessions(config):
    """Session id -> registry entry, for processes that are still alive."""
    live = {}
    for path in glob.glob(os.path.join(os.path.expanduser(config), "sessions/*.json")):
        try:
            with open(path) as f:
                entry = json.load(f)
            os.kill(int(entry["pid"]), 0)
        except (OSError, ValueError, KeyError):
            continue
        live[entry.get("sessionId")] = entry
    return live

_git_cache = {}

def git_state(cwd):
    if cwd in _git_cache:
        return _git_cache[cwd]
    state = None
    if cwd and os.path.isdir(cwd) and os.path.realpath(cwd) != os.path.realpath(HOME):
        try:
            out = subprocess.run(["git", "-C", cwd, "status", "-sb", "--porcelain"], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, universal_newlines=True, timeout=20)
            if out.returncode == 0:
                lines = out.stdout.splitlines()
                branch = lines[0][3:] if lines else ""
                ahead = re.search(r"(?:ahead|devant) (\d+)", branch)
                state = {"branch": branch.split("...")[0], "dirty": len(lines) - 1,
                         "ahead": int(ahead.group(1)) if ahead else 0, "upstream": "..." in branch}
        except (OSError, subprocess.SubprocessError):
            pass
    _git_cache[cwd] = state
    return state

def summarize(path, live):
    prompts, last_reply, title, cwd = [], "", "", ""
    first_ts = last_ts = ""
    last_kind = None  # "reply", "tool_use", "tool_result", "prompt"
    with open(path, errors="replace") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") == "summary":
                title = row.get("summary") or title
            title = row.get("customTitle") or row.get("aiTitle") or title
            if row.get("type") not in ("user", "assistant") or row.get("isSidechain"):
                continue
            cwd = row.get("cwd") or cwd
            if row.get("timestamp"):
                first_ts = first_ts or row["timestamp"]
                last_ts = row["timestamp"]
            text, tool_use, tool_result = text_of(row)
            text = text.strip()
            if row["type"] == "user":
                if tool_result:
                    last_kind = "tool_result"
                elif text and not row.get("isMeta") and not text.startswith(NOISE):
                    prompts.append(re.sub(r"\s+", " ", text))
                    last_kind = "prompt"
            else:
                if text:
                    last_reply, last_kind = text, "reply"
                if tool_use:
                    last_kind = "tool_use"
    if not prompts:
        return None
    sid = os.path.basename(path)[:-6]
    entry = live.get(sid)
    return {
        "id": sid, "path": path, "cwd": cwd, "title": title,
        "start": first_ts, "end": last_ts or time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(os.path.getmtime(path))),
        "prompts": len(prompts), "first_prompt": prompts[0][:400],
        "last_prompt": prompts[-1][:400], "last_reply": last_reply[-1500:], "last_kind": last_kind,
        "live": {"pid": entry["pid"], "status": entry.get("status"), "name": entry.get("name"),
                 "kind": entry.get("kind")} if entry else None,
        "git": git_state(cwd),
    }

cutoff = time.time() - DAYS * 86400
sessions = []
for config in CONFIGS:
    live = live_sessions(config)
    for path in glob.glob(os.path.join(os.path.expanduser(config), "projects/*/*.jsonl")):
        try:
            if os.path.getmtime(path) < cutoff:
                continue
            summary = summarize(path, live)
        except OSError:
            continue
        if summary:
            summary["config"] = config
            sessions.append(summary)
json.dump({"home": HOME, "sessions": sessions}, sys.stdout)
'''

DEFAULT_CLAUDE = "~/.claude"
# Session state, in report order; the first matching rule in classify() wins.
CLAUDE_STATES = [
    ("RUNNING", "Running now"),
    ("INPUT", "Needs your input"),
    ("LIMIT", "Cut off by usage limit"),
    ("INTERRUPTED", "Stopped mid-task"),
    ("UNFINISHED", "Announced more work, never reported back"),
    ("GIT", "Done, but git work not committed/pushed"),
    ("OPEN", "Idle session still open"),
    ("DONE", "Done"),
]
# How to finish a session, in report order (quickest first).
CLAUDE_METHODS = [
    ("COMMAND", "Run a command yourself"),
    ("GIT", "Commit / push git"),
    ("ANSWER", "Answer the session"),
    ("RESTART", "Restart the task in a new session"),
    ("WAIT", "Running, just wait"),
    ("CLOSE", "Idle, can be closed"),
    ("DONE", "Done"),
]
LIMIT_RE = re.compile(r"hit your (session|usage) limit|usage limit", re.I)
ASK_RE = re.compile(
    r"\?\s*$|\b(want me to|do you want|would you like|shall I|tell me (which|if|when)|"
    r"let me know|or say which|which do you prefer|can you run|run it yourself|"
    r"voulez-vous|veux-tu|tu veux|pouvez-vous|peux-tu)\b", re.I)
BYE_RE = re.compile(r"^\W*(thanks|thank you|merci|ok|okay)?\W*(good ?bye|bye|à plus|ciao)?\W*$", re.I)
PROMISE_RE = re.compile(
    r"\b(I'll report back|I will report|next I'll|now I'm|I'm checking|still running|"
    r"running in the background|when it finishes|once it finishes|I'll wait|waiting for|"
    r"en cours|je reviens)\b", re.I)
CMD_RE = re.compile(r"(^|\n)\s*`*! \S|run (it|this|these|them) yourself|in your own terminal|"
                    r"you need to run|dans votre propre terminal|lance[rz]? (cette|la) commande", re.I)


def claude_accounts(cfg):
    """Account name -> Claude config dir; ~/.claude is always one of them."""
    accounts = dict(cfg["claude_accounts"])
    if DEFAULT_CLAUDE not in accounts.values():
        accounts = dict([("default", DEFAULT_CLAUDE)] + list(accounts.items()))
    return accounts


def resume_cmd(config, sid=""):
    # ~/.claude is the default: setting CLAUDE_CONFIG_DIR to it would also move ~/.claude.json.
    env = "" if config in (None, DEFAULT_CLAUDE) else "CLAUDE_CONFIG_DIR=%s " % config
    return ("%sclaude --resume %s" % (env, sid)).strip()


def claude_cmd(node, cfg, is_self, days):
    return remote_python(node, cfg, is_self, [str(days)] + list(claude_accounts(cfg).values()))


def claude_on_host(node, cfg, is_self, days):
    try:
        r = subprocess.run(claude_cmd(node, cfg, is_self, days), input=CLAUDE_AGENT, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, timeout=180)
    except subprocess.TimeoutExpired:
        return {"node": node["name"], "error": "timeout"}
    if r.returncode != 0:
        err = (r.stderr.strip().splitlines() or ["exit %d" % r.returncode])[-1]
        return {"node": node["name"], "error": err[:160]}
    try:
        return dict(json.loads(r.stdout), node=node["name"])
    except ValueError:
        return {"node": node["name"], "error": "unreadable output"}


def claude_classify(s):
    live, git = s.get("live"), s.get("git") or {}
    reply = s["last_reply"]
    if live and live.get("status") == "busy":
        return "RUNNING"
    if LIMIT_RE.search(reply[-300:]):
        return "LIMIT"
    if s["last_kind"] == "prompt" and BYE_RE.search(s["last_prompt"]):
        return "DONE"
    if s["last_kind"] in ("tool_use", "tool_result", "prompt") and not live:
        return "INTERRUPTED"
    tail = reply[-600:]
    if ASK_RE.search(tail):
        return "INPUT"
    if PROMISE_RE.search(tail):
        return "UNFINISHED"
    if git.get("ahead") or git.get("dirty"):
        return "GIT"
    return "OPEN" if live else "DONE"


def claude_method(s):
    state, live = s["state"], s.get("live")
    if state == "RUNNING":
        return "WAIT"
    if state in ("LIMIT", "INTERRUPTED", "UNFINISHED"):
        return "ANSWER" if live else "RESTART"
    if state == "INPUT":
        return "COMMAND" if CMD_RE.search(s["last_reply"][-1500:].replace("*", "")) else "ANSWER"
    if state == "GIT":
        return "GIT"
    return "CLOSE" if state == "OPEN" else "DONE"


def claude_dedupe(sessions):
    """Resumed/forked sessions repeat the same first prompt; keep the most recent."""
    groups = {}
    for s in sessions:
        groups.setdefault((s["node"], s["first_prompt"][:200]), []).append(s)
    kept = []
    for group in groups.values():
        latest = max(group, key=lambda s: s["end"])
        latest["live"] = latest.get("live") or next((s["live"] for s in group if s.get("live")), None)
        kept.append(latest)
    return kept


def one_line(text, limit):
    text = re.sub(r"\s+", " ", re.sub(r"[*`#|]", "", text)).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def ago(ts):
    try:
        then = calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))  # session timestamps are UTC
    except ValueError:
        return "?"
    days = (time.time() - then) / 86400
    return "%dh" % (days * 24) if days < 1 else "%dd" % days


def gist(s):
    """The part of the last reply that says what is pending."""
    sentences = re.split(r"(?<=[.?!:])\s+", re.sub(r"\s+", " ", s["last_reply"]).strip())
    if s["state"] == "INPUT":
        asks = [x for x in sentences if ASK_RE.search(x)]
        return asks[-1] if asks else sentences[-1]
    if s["state"] in ("LIMIT", "INTERRUPTED"):
        return "Last request: " + s["last_prompt"]
    return " ".join(sentences[-2:])


def git_note(git):
    bits = []
    if git.get("ahead"):
        bits.append("%d commit(s) not pushed" % git["ahead"])
    if git.get("dirty"):
        bits.append("%d uncommitted change(s)" % git["dirty"])
    if not git.get("upstream"):
        bits.append("no upstream")
    return "git %s: %s" % (git["branch"], ", ".join(bits))


def command_in(reply):
    """First shell command suggested in a reply (a `! cmd` line or a code block line)."""
    for line in reply.splitlines():
        line = line.strip().strip("`")
        if line.startswith("! "):
            return line[2:]
    for block in reversed(re.findall(r"```[a-z]*\n(.*?)```", reply, re.S)):
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if lines:
            return lines[0]
    return ""


def next_step(s):
    how = s["method"]
    if how == "COMMAND":
        cmd = command_in(s["last_reply"])
        return ("$ " + cmd) if cmd else gist(s)
    if how == "GIT":
        return git_note(s.get("git") or {}).split(": ", 1)[-1]
    if how == "RESTART":
        return ("[%s] " % s["state"].lower()) + s["last_prompt"]
    if how in ("CLOSE", "DONE"):
        return s["title"] or s["first_prompt"]
    return gist(s)


def project(s):
    if os.path.realpath(s["cwd"] or s["home"]) == os.path.realpath(s["home"]):
        return "~ " + (s["title"] or s["first_prompt"])
    return os.path.basename(s["cwd"].rstrip("/"))


def matches_project(s, query):
    """Every word of query (any case) is in the session's project name or folder."""
    text = (project(s) + " " + (s["cwd"] or "")).lower()
    return all(w in text for w in query.lower().split())


def render_claude(sessions, show_all, by, markdown=False):
    if by == "host":
        groups = [(h, h) for h in sorted({s["node"] for s in sessions})]
        key_of = "node"
    else:
        groups, key_of = (CLAUDE_STATES, "state") if by == "state" else (CLAUDE_METHODS, "method")
    seen, unique = set(), []
    for s in sorted(sessions, key=lambda s: s["end"], reverse=True):
        if not show_all and s["method"] == "DONE":
            continue
        key = (s["node"], s["cwd"]) if s["method"] == "GIT" else s["id"]
        if key not in seen:  # one git row per folder
            seen.add(key)
            unique.append(s)
    rows = {s["id"]: [s["node"], s["id"][:8], project(s), ago(s["end"]), next_step(s)] for s in unique}
    head = ["Host", "Hash", "Project", "Last", "Next step"]
    w = [min(cap, max([len(head[i])] + [len(r[i]) for r in rows.values()])) for i, cap in enumerate([16, 8, 28, 4])]
    rest = max(30, shutil.get_terminal_size((140, 24)).columns - sum(w) - 10)
    fmt = "  %-{}s  %-{}s  %-{}s  %-{}s  %s".format(*w)
    order = [m for m, _ in CLAUDE_METHODS]
    lines = []
    for key, label in groups:
        group = [s for s in unique if s[key_of] == key]
        if not group:
            continue
        group.sort(key=lambda s: (order.index(s["method"]), s["end"]) if by == "host" else (s["node"], s["end"]))
        title = "%s (%d)" % (label, len(group))
        if markdown:
            lines += ["", "## " + title, "", "| " + " | ".join(head) + " |", "|---|---|---|---|---|"]
            lines += ["| " + " | ".join(one_line(x, 300).replace("|", "/") for x in rows[s["id"]]) + " |"
                      for s in group]
        else:
            lines += ["", title, fmt % tuple(head),
                      fmt % tuple("-" * n for n in w + [min(rest, 40)])]
            for s in group:
                r = rows[s["id"]]
                lines.append(fmt % (one_line(r[0], w[0]), r[1], one_line(r[2], w[2]), r[3], one_line(r[4], rest)))
    return "\n".join(lines)


def claude_sessions(cfg, only, days):
    """Classified sessions of every target host; returns (sessions, errors, skipped, targets)."""
    targets, skipped = targets_for(cfg, only)
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(lambda t: claude_on_host(t[0], cfg, t[1], days), targets))
    exclude = [re.compile(p, re.I) for p in cfg["claude_exclude"]]
    names = {d: n for n, d in claude_accounts(cfg).items()}
    sessions, errors = [], [r for r in results if "error" in r]
    for data in results:
        for s in data.get("sessions", []):
            if not any(p.search(s["cwd"]) or p.search(s["first_prompt"]) for p in exclude):
                sessions.append(dict(s, node=data["node"], home=data["home"],
                                     account=names.get(s.get("config"), s.get("config"))))
    sessions = claude_dedupe(sessions)
    for s in sessions:
        s["state"] = claude_classify(s)
        s["method"] = claude_method(s)
    return sessions, errors, skipped, targets


def claude_main(argv):
    ap = argparse.ArgumentParser(prog="claude-explorer",
                                 description="List the Claude Code sessions on every host and group them "
                                             "by how to finish them")
    ap.add_argument("--days", type=float, default=60, help="look back this many days (default %(default)g)")
    ap.add_argument("--hosts", help="comma-separated host names")
    ap.add_argument("--all", action="store_true", help="also list finished sessions")
    ap.add_argument("--by", choices=["method", "state", "host"], default="method",
                    help="group by how to finish it (default), session state, or host")
    ap.add_argument("--markdown", metavar="FILE", help="also write the report as Markdown to FILE")
    ap.add_argument("--json", action="store_true", help="print the raw session data as JSON")
    ap.add_argument("--project", action="append", default=[], metavar="WORDS",
                    help="only sessions whose project or folder has all these words (\"tail mon\"); repeatable")
    a = ap.parse_args(argv[1:])
    cfg = load_config()
    only = set(x for x in a.hosts.split(",") if x) if a.hosts else None
    sessions, errors, skipped, targets = claude_sessions(cfg, only, a.days)
    if a.project:
        sessions = [s for s in sessions if any(matches_project(s, q) for q in a.project)]

    if a.json:
        print(json.dumps({"sessions": sessions, "errors": errors,
                          "skipped": [p["name"] for p in skipped]}, indent=1))
    else:
        print("Claude sessions on %s (last %g days)" % (", ".join(n["name"] for n, _ in targets) or "no host",
                                                            a.days))
        print(render_claude(sessions, a.all, a.by) or "\nnothing pending")
    if a.markdown:
        with open(os.path.expanduser(a.markdown), "w") as f:
            f.write("# Claude sessions — %s\n" % time.strftime("%Y-%m-%d %H:%M"))
            f.write(render_claude(sessions, a.all, a.by, markdown=True) + "\n")
    for r in errors:
        print("%s: ERROR: %s" % (r["node"], r["error"]), file=sys.stderr)
    if skipped:
        print("not checked: " + ", ".join(
            "%s (%s)" % (p["name"], p["os"] if p["online"] else "offline") for p in skipped), file=sys.stderr)


# ---------------------------------------------------------------------------------------------
# Todoist: one task per pending Claude session; finishing it (in Todoist or for real) earns points.
# ---------------------------------------------------------------------------------------------
TODOIST_API = os.environ.get("TODOIST_API_URL", "https://api.todoist.com/api/v1")
TODO_STATE = os.path.join(STATE_DIR, "todo.json")
# Points per way of finishing a session; RUNNING ("WAIT") and DONE sessions get no task.
TODO_POINTS = {"COMMAND": 10, "GIT": 10, "ANSWER": 15, "RESTART": 25, "CLOSE": 5}
TODO_VERBS = {"COMMAND": "Run", "GIT": "Commit/push", "ANSWER": "Answer", "RESTART": "Restart", "CLOSE": "Close"}
LEVEL_POINTS = 100


class TodoistError(Exception):
    pass


# Pauses between attempts when Todoist has a temporary problem (about 2 minutes in all).
RETRY_WAITS = (2, 5, 15, 30, 60)
RETRY_CODES = {408, 429, 500, 502, 503, 504}


def todoist(token, method, path, body=None):
    # The same request ID on every attempt, so Todoist can drop a repeat of a write it already did.
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
               "X-Request-Id": str(uuid.uuid4())}
    data = json.dumps(body).encode() if body is not None else None
    for attempt, wait in enumerate(RETRY_WAITS + (None,)):
        req = urllib.request.Request(TODOIST_API + path, method=method, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            error = "HTTP %d %s" % (exc.code, exc.read()[:200].decode(errors="replace"))
            if exc.code not in RETRY_CODES:
                raise TodoistError("%s %s: %s" % (method, path, error))
            after = exc.headers.get("Retry-After", "")
            if wait is not None and after.isdigit():
                wait = max(wait, min(int(after), 300))
        except (urllib.error.URLError, OSError) as exc:  # network trouble, timeouts
            error = str(exc)
        if wait is None:
            raise TodoistError("%s %s: %s (gave up after %d attempts)" % (method, path, error, attempt + 1))
        print("Todoist: %s %s: %s; retrying in %ds" % (method, path, error, wait), file=sys.stderr)
        time.sleep(wait)


def todoist_all(token, path):
    """Follow next_cursor through a paginated listing."""
    items, cursor = [], None
    while True:
        sep = "&" if "?" in path else "?"
        page = todoist(token, "GET", path + (sep + "cursor=" + urllib.parse.quote(cursor) if cursor else ""))
        items += page.get("results", [])
        cursor = page.get("next_cursor")
        if not cursor:
            return items


def todo_project(token, name):
    for p in todoist_all(token, "/projects"):
        if p["name"] == name:
            return p["id"]
    return todoist(token, "POST", "/projects", {"name": name})["id"]


def todo_key(s):
    # One git task per folder, as in the report; otherwise one task per session.
    return "%s:%s" % (s["node"], s["cwd"] if s["method"] == "GIT" else s["id"])


def todo_task(s):
    content = "%s [%s] %s: %s" % (TODO_VERBS[s["method"]], s["node"], project(s), one_line(next_step(s), 200))
    description = "Folder: %s on %s\nResume: cd %s && %s\nWorth %d points (claude-explorer todo)" % (
        s["cwd"] or s["home"], s["node"], shlex.quote(s["cwd"] or s["home"]), resume_cmd(s.get("config")),
        TODO_POINTS[s["method"]])
    return content, description


def load_todo_state():
    try:
        with open(TODO_STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"tasks": {}, "log": []}


def save_todo_state(state):
    os.makedirs(os.path.dirname(TODO_STATE), exist_ok=True)
    tmp = TODO_STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, TODO_STATE)


def streak(log):
    """Consecutive days, ending today or yesterday, with at least one finished task."""
    days = {e["date"] for e in log}
    day = time.time()
    if time.strftime("%Y-%m-%d", time.localtime(day)) not in days:
        day -= 86400
    n = 0
    while time.strftime("%Y-%m-%d", time.localtime(day)) in days:
        n += 1
        day -= 86400
    return n


def render_score(state):
    total = sum(e["points"] for e in state["log"])
    today = time.strftime("%Y-%m-%d")
    level, into = divmod(total, LEVEL_POINTS)
    return "%d points (today %d)  level %d %s %d/%d  streak %d day(s)  %d task(s) open" % (
        total, sum(e["points"] for e in state["log"] if e["date"] == today), level + 1,
        bar(100.0 * into / LEVEL_POINTS), into, LEVEL_POINTS, streak(state["log"]), len(state["tasks"]))


def todo_sync(token, cfg, sessions, reachable, days, dry_run):
    """Mirror pending sessions into Todoist; returns (state, [log entries earned now], [created], [closed])."""
    state = load_todo_state()
    state.setdefault("dismissed", {})
    # A day of margin: a session a little younger than --days may have dropped out between two runs.
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - (days - 1) * 86400))
    pending = {todo_key(s): s for s in sorted(sessions, key=lambda s: s["end"])
               if s["method"] in TODO_POINTS}
    project_id = todo_project(token, cfg["todoist_project"]) if not dry_run else None
    open_ids = {t["id"] for t in todoist_all(token, "/tasks?project_id=" + project_id)} if not dry_run else None
    earned, created, closed = [], [], []

    def award(key, task, why):
        entry = {"date": time.strftime("%Y-%m-%d"), "time": time.strftime("%H:%M"), "points": task["points"],
                 "what": task["content"], "how": why}
        earned.append(entry)
        state["log"].append(entry)
        del state["tasks"][key]

    for key, task in list(state["tasks"].items()):
        if dry_run:
            if key not in pending and key.split(":", 1)[0] in reachable:
                closed.append(task["content"])
            continue
        if task["id"] not in open_ids:
            # Gone from the project: ticked off in Todoist (points) or deleted there (none).
            t = todoist(token, "GET", "/tasks/" + task["id"])
            if t and t.get("checked") and not t.get("is_deleted"):
                award(key, task, "checked in Todoist")
            else:
                del state["tasks"][key]
                state["dismissed"][key] = True  # deleted in Todoist: don't add it back
        elif key not in pending and key.split(":", 1)[0] in reachable:
            todoist(token, "POST", "/tasks/%s/close" % task["id"])
            closed.append(task["content"])
            if task.get("end", "") < cutoff:
                del state["tasks"][key]  # older than --days, so not seen: not proof it was finished
            else:
                # The session no longer needs anything: it was finished outside Todoist.
                award(key, task, "finished in the session")
    state["dismissed"] = {k: v for k, v in state["dismissed"].items() if k in pending}
    for key, s in pending.items():
        if key in state["dismissed"]:
            continue
        content, description = todo_task(s)
        if key in state["tasks"]:
            task = state["tasks"][key]
            if task["content"] != content and not dry_run:
                todoist(token, "POST", "/tasks/" + task["id"], {"content": content, "description": description})
                task.update(content=content, points=TODO_POINTS[s["method"]])
            task["end"] = s["end"]
            continue
        created.append(content)
        if not dry_run:
            t = todoist(token, "POST", "/tasks", {"content": content, "description": description,
                                                   "project_id": project_id})
            state["tasks"][key] = {"id": t["id"], "content": content, "points": TODO_POINTS[s["method"]],
                                   "end": s["end"]}
    if not dry_run:
        save_todo_state(state)
    return state, earned, created, closed


def todo_main(argv):
    ap = argparse.ArgumentParser(prog="claude-explorer todo",
                                 description="Turn pending Claude Code sessions into Todoist tasks and earn "
                                             "points for finishing them")
    ap.add_argument("--days", type=float, default=60, help="look back this many days (default %(default)g)")
    ap.add_argument("--hosts", help="comma-separated host names")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, without touching Todoist")
    ap.add_argument("--score", action="store_true", help="only show points, level and streak")
    ap.add_argument("--log", action="store_true", help="list the finished tasks and their points")
    a = ap.parse_args(argv[1:])
    if a.score or a.log:
        state = load_todo_state()
        if a.log:
            for e in state["log"]:
                print("%s %s  +%-3d %s (%s)" % (e["date"], e["time"], e["points"], e["what"], e["how"]))
        print(render_score(state))
        return
    cfg = load_config()
    token = os.environ.get("TODOIST_API_TOKEN") or cfg["todoist_token"]
    if not token and not a.dry_run:
        sys.exit("claude-explorer: set TODOIST_API_TOKEN or \"todoist_token\" in %s\n"
                 "  (token: Todoist > Settings > Integrations > Developer)" % CONFIG_PATH)
    only = set(x for x in a.hosts.split(",") if x) if a.hosts else None
    sessions, errors, skipped, targets = claude_sessions(cfg, only, a.days)
    reachable = {n["name"] for n, _ in targets} - {r["node"] for r in errors}
    try:
        state, earned, created, closed = todo_sync(token, cfg, sessions, reachable, a.days, a.dry_run)
    except TodoistError as exc:
        sys.exit("claude-explorer: Todoist: %s" % exc)
    prefix = "would " if a.dry_run else ""
    for content in created:
        print("%sadd    %s" % (prefix, content))
    for content in closed:
        print("%sclose  %s" % (prefix, content))
    for e in earned:
        print(c("+%-3d", 0) % e["points"] + "  %s (%s)" % (e["what"], e["how"]))
    if not (created or closed or earned):
        print("Todoist is up to date")
    print(render_score(state))
    for r in errors:
        print("%s: ERROR: %s (its tasks were left alone)" % (r["node"], r["error"]), file=sys.stderr)


# ---------------------------------------------------------------------------------------------
# Dispatch: Todoist tasks labelled @claude are handed to an unattended Claude Code agent on a host.
# ---------------------------------------------------------------------------------------------
AGENT_LAUNCHER = r'''
import base64, getpass, json, os, shutil, subprocess, sys

RUN = os.path.expanduser("~/.local/state/claude-explorer/agents")
LEGACY_RUN = os.path.expanduser("~/.local/state/tailmon/agents")  # agents started by tailmon
NOTE = ("You were started unattended from a Todoist task by `claude-explorer dispatch`; nobody is watching. "
        "You can read and edit files, but you have no shell. Don't stop to ask questions: do what you can. "
        "If the task needs a command run or a decision made, end your reply with exactly what the user "
        "should run or decide. Your final reply is posted as a comment on the task.")
NOTE_SHELL = ("You were started unattended from a Todoist task by `claude-explorer dispatch`; nobody is watching. "
              "You can read and edit files, and you have a restricted shell: only commands starting with %s. "
              "Anything else is refused, and so is any command using $VARIABLES, $(...) or backquotes: write "
              "every value literally (your user name is %s, your home directory is %s). Never cancel jobs. "
              "You cannot wait for long jobs: submit them with sbatch, check they are queued, and end your reply "
              "with the job IDs, where their output will be and how to check it. Don't stop to ask questions: "
              "do what you can, and end with exactly what the user should run or decide. Your final reply is "
              "posted as a comment on the task.")

def emit(value):
    print(json.dumps(value))

op = sys.argv[1]
if op == "accounts":  # which config dirs hold a login
    emit({d: os.path.exists(os.path.join(os.path.expanduser(d), ".credentials.json")) for d in sys.argv[2:]})
    raise SystemExit
sid = sys.argv[2]
out_path = os.path.join(RUN, sid + ".json")
if op == "check" and not os.path.exists(out_path):
    out_path = os.path.join(LEGACY_RUN, sid + ".json")
if op == "start":
    folder, config = os.path.expanduser(sys.argv[3]), sys.argv[4]
    name, prompt = (base64.b64decode(x).decode() for x in sys.argv[5:7])
    shell = json.loads(base64.b64decode(sys.argv[7]).decode()) if len(sys.argv) > 7 else {"allow": []}
    if shell["allow"]:
        tools = (["--allowedTools"] + ["Bash(%s:*)" % c for c in shell["allow"]]
                 + ["--disallowedTools"] + ["Bash(%s:*)" % c for c in shell["deny"]])
        note = NOTE_SHELL % (", ".join("`%s`" % c for c in shell["allow"]), getpass.getuser(),
                             os.path.expanduser("~"))
    else:
        tools, note = ["--disallowedTools", "Bash"], NOTE
    env = dict(os.environ)
    if config != "~/.claude":  # the default; setting it would also move ~/.claude.json
        env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(config)
    # Non-interactive ssh shells often lack ~/.local/bin on the PATH.
    extra = [os.path.expanduser(p) for p in ("~/.local/bin", "~/.claude/local", "~/.npm-global/bin")]
    claude = shutil.which("claude", path=os.pathsep.join([os.environ.get("PATH", "")] + extra))
    created = not os.path.isdir(folder)
    if not claude:
        emit({"error": "claude not found on this host"})
    else:
        os.makedirs(folder, exist_ok=True)  # the task may be to start something new there
        os.makedirs(RUN, exist_ok=True)
        with open(out_path, "w") as out:
            p = subprocess.Popen([claude, "-p", prompt, "--output-format", "json", "--permission-mode", "acceptEdits",
                                  "--session-id", sid, "--name", name, "--append-system-prompt", note] + tools,
                                 cwd=folder, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                 start_new_session=True)  # survives the ssh session
        emit({"pid": p.pid, "created": created})
elif op == "check":
    try:
        with open("/proc/%s/cmdline" % sys.argv[3], "rb") as f:
            running = sid.encode() in f.read()
    except OSError:
        running = False
    try:
        with open(out_path, errors="replace") as f:
            output = f.read()[-400000:]
    except OSError:
        output = ""
    emit({"running": running, "output": output})
'''
DISPATCH_STATE = os.path.join(STATE_DIR, "dispatch.json")


def remote_python(node, cfg, is_self, args):
    """Command running `python3 -I - args...` on the node (directly when it is this machine)."""
    if is_self:
        return [sys.executable, "-I", "-"] + args
    ssh, host = ssh_to(node, cfg)
    # ssh passes the command through the remote login shell.
    return ssh + [host] + [shlex.quote(value) for value in ["python3", "-I", "-"] + args]


def ssh_to(node, cfg):
    """(ssh command with options, user@host) for reaching node."""
    user = cfg["users"].get(node["name"], cfg["default_user"])
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
           "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=5",
           "-o", "ServerAliveCountMax=3"]
    if cfg.get("identity"):
        ssh += ["-i", os.path.expanduser(cfg["identity"])]
    if node["dns"] or node["ip"]:
        ssh += ["-o", "HostName=%s" % (node["dns"] or node["ip"])]
    return ssh, "%s@%s" % (user, node["name"]) if user else node["name"]


def launcher(node, cfg, is_self, args):
    try:
        r = subprocess.run(remote_python(node, cfg, is_self, args), input=AGENT_LAUNCHER, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    if r.returncode != 0:
        return {"error": ((r.stderr.strip().splitlines() or ["exit %d" % r.returncode])[-1])[:160]}
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"error": "unreadable output"}


def relabel(token, task_id, drop, add):
    task = todoist(token, "GET", "/tasks/" + task_id)
    if task:
        drop = {d.lower() for d in drop}
        labels = [l for l in task.get("labels", []) if l.lower() not in drop]
        todoist(token, "POST", "/tasks/" + task_id, {"labels": labels + [add]})


def agent_result(output):
    """The JSON result `claude -p --output-format json` printed; warnings may come before it."""
    for line in reversed(output.strip().splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                pass
    return None


def agent_report(run, output):
    """(outcome, Todoist comment) for an agent that stopped; outcome is "done", "failed" or "limit"."""
    resume = "Resume: `ssh %s`, then `cd %s && %s`" % (run["node"], run["dir"],
                                                      resume_cmd(run.get("config"), run["sid"]))
    where = "Agent on %s, account %s%s" % (run["node"], run.get("account", "default"),
                                          ", restricted shell" if run.get("shell") else "")
    result = agent_result(output)
    if result is None:
        outcome = "limit" if LIMIT_RE.search(output[-500:]) else "failed"
        return outcome, "%s, stopped without a result:\n```\n%s\n```\n%s" % (
            where, output.strip()[-3000:] or "(no output)", resume)
    text = result.get("result", "").strip()
    if LIMIT_RE.search(text[-300:]):
        return "limit", "%s, hit its usage limit; the task will be retried on another account.\n\n%s" % (
            where, resume)
    return "failed" if result.get("is_error") else "done", "%s, in %s (%s, %d turns, $%.2f):\n\n%s\n\n%s" % (
        where, run["dir"], result.get("subtype", "?"), result.get("num_turns", 0),
        result.get("total_cost_usd", 0), text[:12000], resume)


def dispatch_main(argv):
    ap = argparse.ArgumentParser(prog="claude-explorer dispatch",
                                 description="Hand Todoist tasks labelled @claude to Claude Code agents on the "
                                             "tailnet, and post their reports back")
    ap.add_argument("--dry-run", action="store_true", help="show what would start, without starting anything")
    ap.add_argument("--status", action="store_true", help="only list the agents still running and the accounts resting")
    ap.add_argument("--report-only", action="store_true", help="post the reports of agents that stopped; start nothing")
    ap.add_argument("--accounts", action="store_true", help="show which Claude accounts are logged in on each host")
    a = ap.parse_args(argv[1:])
    try:
        with open(DISPATCH_STATE) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {"running": {}}
    state.setdefault("rest", {})
    cfg = load_config()
    accounts = claude_accounts(cfg)
    if a.status:
        for task_id, run in state["running"].items():
            print("%-12s %-16s %-10s %s  %s  %s" % (run["started"], run["node"], run.get("account", "default"),
                                                     run["sid"][:8], run["dir"], run["content"]))
        for name, until in sorted(state["rest"].items()):
            if until > time.time():
                print("account %s rests until %s (usage limit)" % (name, time.strftime("%H:%M", time.localtime(until))))
        print("%d agent(s) running" % len(state["running"]))
        return
    if a.accounts:
        targets, _ = targets_for(cfg)
        with cf.ThreadPoolExecutor(max_workers=16) as ex:
            found = list(ex.map(lambda t: launcher(t[0], cfg, t[1], ["accounts"] + list(accounts.values())), targets))
        for (n, _), logins in zip(targets, found):
            if "error" in logins:
                print("%-16s ERROR: %s" % (n["name"], logins["error"]))
            else:
                print("%-16s %s" % (n["name"], "  ".join(
                    "%s %s" % (name, "ok" if logins.get(d) else "-") for name, d in accounts.items())))
        print("log an account in on a host with:  CLAUDE_CONFIG_DIR=<dir> claude   (then /login)")
        return
    token = os.environ.get("TODOIST_API_TOKEN") or cfg["todoist_token"]
    if not token:
        sys.exit("claude-explorer: set TODOIST_API_TOKEN or \"todoist_token\" in %s" % CONFIG_PATH)
    targets, _ = targets_for(cfg)
    nodes = {n["name"].lower(): (n, is_self) for n, is_self in targets}
    me = next(n["name"].lower() for n, is_self in targets if is_self)
    label = cfg["dispatch_label"]
    logged_in = {}  # host -> names of the accounts logged in there

    def save():
        os.makedirs(os.path.dirname(DISPATCH_STATE), exist_ok=True)
        with open(DISPATCH_STATE + ".tmp", "w") as f:
            json.dump(state, f, indent=1)
        os.replace(DISPATCH_STATE + ".tmp", DISPATCH_STATE)

    try:
        # Agents that stopped: post their report and mark the task done (or failed); you close it.
        for task_id, run in list(state["running"].items()):
            if run["node"].lower() not in nodes:
                continue  # host offline: check next time
            n, is_self = nodes[run["node"].lower()]
            check = launcher(n, cfg, is_self, ["check", run["sid"], str(run["pid"])])
            if "error" in check or check["running"]:
                continue
            outcome, comment = agent_report(run, check["output"])
            if a.dry_run:
                print("would report  [%s %s] %s: %s (%s)" % (run["node"], run["sid"][:8], run["dir"], run["content"], outcome))
                continue
            try:
                current = todoist(token, "GET", "/tasks/" + task_id)
                if current:
                    if not run.get("reported"):  # posted on an earlier run whose relabel failed
                        todoist(token, "POST", "/comments", {"task_id": task_id, "content": comment})
                        run["reported"] = True
                        save()
                    # A usage limit isn't the task's fault: put it back in the queue for another account.
                    relabel(token, task_id, (label, label + "-running"),
                            label if outcome == "limit" else label + "-" + outcome)
                    if outcome != "limit" and cfg["dispatch_due_today"] and not current.get("due"):
                        todoist(token, "POST", "/tasks/" + task_id, {"due_string": "today"})
            except TodoistError as exc:
                print("not reported yet, will try again next run: %s (%s)" % (run["content"], exc), file=sys.stderr)
                continue
            if outcome == "limit":
                state["rest"][run.get("account", "default")] = time.time() + cfg["dispatch_rest_hours"] * 3600
            print("%-6s  [%s %s %s] %s: %s" % (outcome, run["node"], run.get("account", "default"), run["sid"][:8], run["dir"],
                                               run["content"]))
            del state["running"][task_id]
            save()

        if a.report_only:
            print("%d agent(s) running" % len(state["running"]))
            return

        # New @claude tasks: start an agent on the host named by another label, else here.
        # "@word" in the title counts as a label too, as Todoist leaves it as text for unknown labels.
        tasks = todoist_all(token, "/tasks")
        tag_re = re.compile(r"(?<!\S)@([\w.-]+)")
        lowered = label.lower()
        finished = {lowered + "-running", lowered + "-done", lowered + "-failed"}

        def tags(t):
            return [l.lower() for l in t.get("labels", [])] + [m.lower() for m in tag_re.findall(t["content"])]
        # Every task of the dispatch project is for an agent, labelled or not.
        project_ids = {p["id"] for p in todoist_all(token, "/projects")
                       if p["name"].lower() == (cfg["dispatch_project"] or "").lower()}
        wanted = [t for t in tasks if (lowered in tags(t) or t.get("project_id") in project_ids)
                  and not finished & set(tags(t))]
        if not wanted:
            seen = sorted({l for t in tasks for l in tags(t)})
            print("no open task labelled @%s%s among %d open tasks (labels seen: %s)" % (
                label, " or in project %s" % cfg["dispatch_project"] if project_ids else "", len(tasks),
                ", ".join("@" + l for l in seen) or "none"))
        for task in wanted:
            if task["id"] in state["running"]:
                continue
            hosts = [l for l in tags(task) if l in nodes]
            name = hosts[0] if hosts else me
            pinned = [acc for acc in accounts if acc.lower() in tags(task)]
            title = tag_re.sub(lambda m: "" if m.group(1).lower() in nodes or m.group(1).lower() == lowered
                               or m.group(1).lower() in (acc.lower() for acc in accounts)
                               else m.group(0), task["content"])
            title = re.sub(r"\s+", " ", title).strip()
            folder = "~"
            lines, after = [], []
            for line in (task.get("description") or "").splitlines():
                dep = re.match(r"\s*after:\s*(.+?)\s*$", line, re.I)
                m = re.match(r"\s*dir:\s*(.+?)\s*$", line, re.I) or (
                    re.match(r"\s*([~/]\S*)\s*$", line) if not lines and folder == "~" else None)
                if dep:  # "after: <part of another task's title, or its ID>"
                    after.append(dep.group(1))
                elif m:  # "dir: path", or a description that starts with a bare path
                    folder = m.group(1)
                elif line.strip() or lines:
                    lines.append(line)
            # Prerequisites still open must be @claude-done; closed ones count as finished.
            blocking = []
            for want in after:
                for t in tasks:
                    if t["id"] != task["id"] and (t["id"] == want or want.lower() in t["content"].lower()) \
                            and lowered + "-done" not in tags(t):
                        blocking.append("%s%s" % (t["content"][:60], " (failed)" if lowered + "-failed" in tags(t)
                                                  else ""))
            if blocking:
                print("waiting  [%s] %s: %s (after: %s)" % (name, folder, title, "; ".join(blocking)))
                continue
            # The least busy account logged in on that host, not resting after a usage limit.
            n, is_self = nodes[name]
            if name not in logged_in:
                found = launcher(n, cfg, is_self, ["accounts"] + list(accounts.values()))
                logged_in[name] = {acc for acc, d in accounts.items() if found.get(d)}
            busy = {}
            for run in state["running"].values():
                busy[run.get("account", "default")] = busy.get(run.get("account", "default"), 0) + 1
            free = [acc for acc in (pinned or accounts) if acc in logged_in[name]
                    and state["rest"].get(acc, 0) <= time.time() and busy.get(acc, 0) < cfg["dispatch_max"]]
            if not free:
                print("waiting  [%s] %s: %s (no free account: %s)" % (name, folder, title, ", ".join(
                    "%s %s" % (acc, "not logged in" if acc not in logged_in[name] else "resting"
                               if state["rest"].get(acc, 0) > time.time() else "busy")
                    for acc in (pinned or accounts))))
                continue
            account = min(free, key=lambda acc: busy.get(acc, 0))
            if a.dry_run:
                print("would start  [%s %s%s] %s: %s" % (name, account, ", shell" if shell_for(cfg, name) else "",
                                                         folder, title))
                busy[account] = busy.get(account, 0) + 1
                state["running"]["dry-%s" % task["id"]] = {"account": account}
                continue
            sid = str(uuid.uuid4())
            prompt = "\n\n".join(x for x in [title, "\n".join(lines).strip()] if x)
            allow = shell_for(cfg, n["name"])
            started = launcher(n, cfg, is_self, ["start", sid, folder, accounts[account],
                                                 base64.b64encode(("todo: " + title)[:80].encode()).decode(),
                                                 base64.b64encode(prompt.encode()).decode(),
                                                 base64.b64encode(json.dumps(
                                                     {"allow": allow, "deny": SHELL_DENY}).encode()).decode()])
            if "error" in started:
                print("%s: ERROR: %s (%s)" % (n["name"], started["error"], task["content"]), file=sys.stderr)
                continue
            state["running"][task["id"]] = {"node": n["name"], "sid": sid, "pid": started["pid"], "dir": folder,
                                            "content": title, "started": time.strftime("%m-%d %H:%M"),
                                            "account": account, "config": accounts[account], "shell": bool(allow)}
            save()
            try:
                todoist(token, "POST", "/comments", {"task_id": task["id"], "content":
                        "Agent started on %s with account %s in %s%s (session %s)%s." % (
                            n["name"], account, folder, " (folder created)" if started.get("created") else "", sid,
                            ", with a shell limited to: " + ", ".join(allow) if allow else ", without a shell")})
                relabel(token, task["id"], (label,), label + "-running")
            except TodoistError as exc:  # the agent runs anyway; its report will still be posted
                print("started, but Todoist wasn't updated: %s (%s)" % (title, exc), file=sys.stderr)
            print("started  [%s %s %s] %s: %s" % (n["name"], account, sid[:8], folder, title))
    except TodoistError as exc:
        sys.exit("claude-explorer: Todoist: %s" % exc)
    print("%d agent(s) running" % len([k for k in state["running"] if not k.startswith("dry-")]))


# ---------------------------------------------------------------------------------------------
# Videos: gather the screen recordings of a terminal (CLI tutorials, demos) from every host into one folder.
# ---------------------------------------------------------------------------------------------
VIDEO_AGENT = r'''
import base64, json, os, shutil, subprocess, sys, tempfile
EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".ogv", ".m4v", ".gif", ".cast"}
SKIP = {"node_modules", "site-packages", "miniforge3", "miniconda3", "anaconda3", "nvhpc", "snap", "spack",
        "__pycache__", "venv", "Trash"}
W, H = 960, 540

def scan(roots, min_kb):
    out = []
    for root in roots:
        root = os.path.expanduser(root)
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if not x.startswith(".") and x not in SKIP]
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext not in EXTS:
                    continue
                p = os.path.join(d, f)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if st.st_size < (1024 if ext == ".gif" else min_kb) * 1024 and ext != ".cast":
                    continue
                out.append({"path": p, "size": st.st_size, "mtime": int(st.st_mtime)})
    return out

def text_lines(buf):
    """Most evenly spaced text lines found in one of 4 column bands: runs of 4-24 rows full of sharp edges,
    separated by quiet rows, of nearly the same height. Night scenes and renderings have few or none."""
    best = 0
    for band in range(4):
        x0, x1 = band * W // 4, (band + 1) * W // 4
        edges = []
        for y in range(H):
            row = buf[y * W + x0:y * W + x1]
            edges.append(sum(1 for a, b in zip(row, row[1:]) if abs(a - b) > 50))
        heights, y = [], 0
        while y < H:
            if edges[y] >= 4:
                top = y
                while y < H and edges[y] >= 4:
                    y += 1
                if 4 <= y - top <= 24:
                    heights.append(y - top)
            else:
                y += 1
        if heights:
            mid = sorted(heights)[len(heights) // 2]
            best = max(best, sum(1 for h in heights if abs(h - mid) <= 2))
    return best

def frame_score(buf):
    """[share of the 8x6 tiles with a flat dark background, sharp text and no soft camera gradients,
    evenly spaced text lines]; video_frame_ok decides from these."""
    tw, th = W // 8, H // 6
    good = 0
    for ty in range(6):
        for tx in range(8):
            px = [buf[(ty * th + y) * W + tx * tw + x] for y in range(th) for x in range(tw)]
            hist = [0] * 256
            for v in px:
                hist[v] += 1
            mode = max(range(256), key=lambda i: sum(hist[max(0, i - 6):i + 7]))
            n = len(px)
            flat = sum(hist[max(0, mode - 6):mode + 7]) / n
            text = sum(c for i, c in enumerate(hist) if abs(i - mode) > 60) / n
            soft = sum(1 for y in range(0, th, 2) for a, b in zip(px[y * tw:(y + 1) * tw], px[y * tw + 1:(y + 1) * tw])
                       if 3 <= abs(a - b) <= 20) / ((th + 1) // 2 * (tw - 1))
            good += mode < 90 and flat >= 0.55 and 0.01 <= text <= 0.45 and soft <= 0.18
    share = good / 48
    return [round(share, 2), text_lines(buf) if share >= 0.1 else 0]

def score(path):
    if path.lower().endswith(".cast"):
        return {"frames": "cast"}
    if not shutil.which("ffmpeg"):
        return {"error": "no ffmpeg"}
    dur = 0.0
    try:
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60).stdout)
    except (ValueError, subprocess.TimeoutExpired):
        pass
    notes = []
    for t in ([dur * f for f in (0.15, 0.4, 0.65, 0.9)] if dur > 2 else [0]):
        try:
            buf = subprocess.run(["ffmpeg", "-v", "error", "-ss", "%.2f" % t, "-i", path, "-frames:v", "1",
                                  "-vf", "scale=%d:%d" % (W, H), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120).stdout
        except subprocess.TimeoutExpired:
            continue
        if len(buf) != W * H:
            continue
        notes.append(frame_score(buf))
    if not notes:
        return {"error": "no frame decoded"}
    return {"frames": notes, "duration": round(dur)}

mode = sys.argv[1]
if mode == "scan":
    print(json.dumps({"home": os.path.expanduser("~"), "ffmpeg": bool(shutil.which("ffmpeg")),
                      "tmp_free": shutil.disk_usage(tempfile.gettempdir()).free,
                      "files": scan(json.loads(sys.argv[3]), int(sys.argv[2]))}))
elif mode == "score":
    print(json.dumps({p: score(p) for p in json.loads(base64.b64decode(sys.argv[2]))}))
elif mode == "score-stdin":  # the video arrives on stdin from a host without ffmpeg
    fd, tmp = tempfile.mkstemp(suffix=sys.argv[2])
    try:
        with os.fdopen(fd, "wb") as f:
            shutil.copyfileobj(sys.stdin.buffer, f, 1 << 20)
        print(json.dumps(score(tmp)))
    finally:
        os.unlink(tmp)
'''
VIDEO_CACHE = os.path.join(STATE_DIR, "videos.json")
VIDEO_SCORER = 3  # bump when frame_score's measurements change: cached ones of other versions are redone


def video_frame_ok(m):
    """A frame looks like a terminal: a dark text area and evenly spaced text lines (many lines suffice)."""
    share, lines = m
    return (share >= 0.2 and lines >= 10) or lines >= 20


def video_score(frames):
    if frames == "cast":
        return 1.0
    return round(sum(map(video_frame_ok, frames)) / len(frames), 2)
VIDEO_MANIFEST = ".claude-explorer-videos.json"


def video_cmd(node, cfg, is_self, args, stdin_script=True):
    """Run VIDEO_AGENT on node; with stdin_script=False the script is passed with -c so stdin stays free."""
    if stdin_script:
        return remote_python(node, cfg, is_self, args)
    code = "import base64;exec(base64.b64decode(%r))" % base64.b64encode(VIDEO_AGENT.encode()).decode()
    if is_self:
        return [sys.executable, "-I", "-c", code] + args
    ssh, host = ssh_to(node, cfg)
    return ssh + [host] + [shlex.quote(v) for v in ["python3", "-I", "-c", code] + args]


def video_run(node, cfg, is_self, args, timeout):
    try:
        r = subprocess.run(video_cmd(node, cfg, is_self, args), input=VIDEO_AGENT, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    if r.returncode != 0:
        return {"error": ((r.stderr.strip().splitlines() or ["exit %d" % r.returncode])[-1])[:160]}
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"error": "unreadable output"}


def video_pipe_score(src, analyzer, cfg, path):
    """Score a video of a host without ffmpeg by streaming it to the analyzer host."""
    if src[1]:
        reader = None
        stdin = open(path, "rb")
    else:
        ssh, host = ssh_to(src[0], cfg)
        reader = subprocess.Popen(ssh + [host, "cat " + shlex.quote(path)], stdout=subprocess.PIPE)
        stdin = reader.stdout
    try:
        r = subprocess.run(video_cmd(analyzer[0], cfg, analyzer[1], ["score-stdin", os.path.splitext(path)[1]],
                                     stdin_script=False),
                           stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1800)
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    finally:
        stdin.close()
        if reader:
            reader.kill()
            reader.wait()
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"error": (r.stderr.decode(errors="replace").strip().splitlines() or ["failed"])[-1][:160]}


def video_name(dest, manifest, key, path, size):
    """File name in dest: the original name; a different video with the same name gets ' (2)', ' (3)'…"""
    if key in manifest:
        return manifest[key]["file"]
    stem, ext = os.path.splitext(os.path.basename(path))
    taken = {v["file"]: v["size"] for v in manifest.values()}
    for i in range(1, 1000):
        name = stem + ext if i == 1 else "%s (%d)%s" % (stem, i, ext)
        if taken.get(name) == size:  # same name and size: the same video already copied from elsewhere
            return None
        if name not in taken and not os.path.exists(os.path.join(dest, name)):
            return name
    return None


def videos_main(argv):
    ap = argparse.ArgumentParser(prog="claude-explorer videos",
                                 description="Find the videos on every host that look like a terminal "
                                             "(CLI tutorials, screen recordings) and copy them into one folder")
    ap.add_argument("--dest", default="~/Videos/CLI-tutorials", help="folder to gather them in (default %(default)s)")
    ap.add_argument("--hosts", help="comma-separated host names")
    ap.add_argument("--roots", default="~", help="comma-separated folders to search on each host (default %(default)s); "
                                                 "hidden folders and software trees are skipped")
    ap.add_argument("--min-score", type=float, default=0.25,
                    help="share of sampled frames that must look like a terminal (default %(default)g)")
    ap.add_argument("--min-kb", type=int, default=100, help="ignore smaller videos (default %(default)d KB)")
    ap.add_argument("--max-mb", type=int, default=2000,
                    help="largest video streamed to another host for scoring when its own host has no ffmpeg "
                         "(default %(default)d MB)")
    ap.add_argument("--scan-timeout", type=int, default=3600,
                    help="seconds allowed for listing one host's files (default %(default)d)")
    ap.add_argument("--dry-run", action="store_true", help="only list what would be copied")
    ap.add_argument("--all", action="store_true", help="also list the videos that don't look like a terminal")
    ap.add_argument("--rescore", action="store_true", help="ignore cached scores")
    a = ap.parse_args(argv[1:])
    cfg = load_config()
    only = set(x for x in a.hosts.split(",") if x) if a.hosts else None
    roots = [r for r in a.roots.split(",") if r]
    targets, skipped = targets_for(cfg, only)
    dest = os.path.expanduser(a.dest)

    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        scans = list(ex.map(lambda t: video_run(t[0], cfg, t[1], ["scan", str(a.min_kb), json.dumps(roots)],
                                                       a.scan_timeout),
                            targets))
    try:
        with open(VIDEO_CACHE) as f:
            cache = {} if a.rescore else json.load(f)
    except (OSError, ValueError):
        cache = {}
    analyzers = [t for t, s in zip(targets, scans) if s.get("ffmpeg")]
    tmp_free = {t[0]["name"]: s.get("tmp_free", 0) for t, s in zip(targets, scans)}
    analyzers.sort(key=lambda t: -tmp_free[t[0]["name"]])  # stream videos to the one with most room first
    has_ffmpeg = {t[0]["name"] for t in analyzers}
    by_key = {t[0]["name"]: t for t in targets}
    videos, todo = [], {}  # todo: host name -> [videos to score]
    for t, s in zip(targets, scans):
        if "error" in s:
            print("%s: ERROR: %s%s" % (t[0]["name"], s["error"], "; narrow it with --roots or raise --scan-timeout"
                                         if s["error"] == "timeout" else ""), file=sys.stderr)
            continue
        for v in s["files"]:
            v.update(host=t[0]["name"], key="%s:%s" % (t[0]["name"], v["path"]))
            hit = cache.get(v["key"])
            if hit and (hit["size"], hit["mtime"], hit.get("v")) == (v["size"], v["mtime"], VIDEO_SCORER):
                v.update(score=video_score(hit["frames"]))
            else:
                todo.setdefault(t[0]["name"], []).append(v)
            videos.append(v)
    print("%d video(s) on %s; scoring %d" % (len(videos), ", ".join(t[0]["name"] for t in targets),
                                              sum(len(x) for x in todo.values())), file=sys.stderr)

    def score_host(item):
        name, vs = item
        t = by_key[name]
        if name in has_ffmpeg:
            res = {}
            for i in range(0, len(vs), 20):  # in batches: one ssh call per 20 videos
                chunk = [v["path"] for v in vs[i:i + 20]]
                r = video_run(t[0], cfg, t[1], ["score", base64.b64encode(json.dumps(chunk).encode()).decode()],
                              60 + 60 * len(chunk))
                res.update(r if "error" not in r else {p: r for p in chunk})
            return [(v, res.get(v["path"], {"error": "missing"})) for v in vs]
        out = []
        for v in vs:
            if v["path"].lower().endswith(".cast"):
                out.append((v, {"frames": "cast"}))
            elif not analyzers:
                out.append((v, {"error": "no host with ffmpeg"}))
            elif v["size"] > a.max_mb << 20:
                out.append((v, {"error": "too big to stream for scoring (--max-mb)"}))
            else:
                r = {"error": "no host with ffmpeg has room for it"}
                for analyzer in analyzers:
                    if tmp_free[analyzer[0]["name"]] > v["size"] + (1 << 30):
                        r = video_pipe_score(t, analyzer, cfg, v["path"])
                        if "No space left" not in r.get("error", ""):
                            break
                out.append((v, r))
        return out

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for done in ex.map(score_host, todo.items()):
            for v, r in done:
                if "frames" in r:
                    v["score"] = video_score(r["frames"])
                    cache[v["key"]] = {"size": v["size"], "mtime": v["mtime"], "frames": r["frames"], "v": VIDEO_SCORER}
                else:
                    v["error"] = r["error"]
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(VIDEO_CACHE, "w") as f:
        json.dump(cache, f, indent=1)

    try:
        with open(os.path.join(dest, VIDEO_MANIFEST)) as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        manifest = {}
    picked = sorted((v for v in videos if v.get("score", 0) >= a.min_score), key=lambda v: (v["host"], v["path"]))
    if not a.dry_run:
        os.makedirs(dest, exist_ok=True)
    copied = 0
    for v in picked:
        name = video_name(dest, manifest, v["key"], v["path"], v["size"])
        target = os.path.join(dest, name) if name else None
        have = target and os.path.exists(target) and os.path.getsize(target) == v["size"]
        status = "duplicate" if not name else "have" if have else "copy"
        print("%-9s %4.0f%% %8.1f MB  %s:%s%s" % (status, 100 * v["score"], v["size"] / 2 ** 20, v["host"], v["path"],
                                               "  ->  " + name if name and status == "copy" else ""))
        if status != "copy":
            continue
        if a.dry_run:
            manifest[v["key"]] = {"file": name, "size": v["size"], "score": v["score"]}  # not saved
            copied += 1
            continue
        node, is_self = by_key[v["host"]]
        try:
            if is_self:
                shutil.copy2(v["path"], target)
            else:
                ssh, host = ssh_to(node, cfg)
                subprocess.run(["rsync", "-t", "--partial", "-s", "-e", " ".join(shlex.quote(x) for x in ssh),
                                "%s:%s" % (host, v["path"]), target], check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            print("  copy failed: %s" % exc, file=sys.stderr)
            continue
        manifest[v["key"]] = {"file": name, "size": v["size"], "score": v["score"]}
        with open(os.path.join(dest, VIDEO_MANIFEST), "w") as f:
            json.dump(manifest, f, indent=1)
        copied += 1
    if a.all:
        for v in sorted(videos, key=lambda v: (v["host"], v["path"])):
            if v.get("score", 0) < a.min_score:
                print("%-9s %4s  %8.1f MB  %s:%s" % ("no", "%.0f%%" % (100 * v["score"]) if "score" in v else "?",
                                                    v["size"] / 2 ** 20, v["host"], v["path"]))
    for v in videos:
        if "error" in v:
            print("not scored: %s:%s (%s)" % (v["host"], v["path"], v["error"]), file=sys.stderr)
    print("%d terminal video(s) found, %d %s to %s" % (len(picked), copied,
                                                      "would be copied" if a.dry_run else "copied", dest))
    if skipped:
        print("not checked: " + ", ".join(
            "%s (%s)" % (p["name"], p["os"] if p["online"] else "offline") for p in skipped), file=sys.stderr)


# ---------------------------------------------------------------------------------------------
# Move: carry Claude Code sessions (and the folders they work in) to another host, e.g. before
# maintenance. Only ever run by hand: it shows a plan, asks, copies, and leaves the originals alone.
# ---------------------------------------------------------------------------------------------
MOVE_AGENT = r'''
import json, os, subprocess, sys, tarfile

op, arg = sys.argv[1], json.loads(sys.argv[2])
HOME = os.path.expanduser("~")

def emit(value):
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()

def walk_one_fs(path):
    """os.walk that stays on path's filesystem: no sshfs or other mounts below it."""
    dev = os.lstat(path).st_dev
    for root, dirs, names in os.walk(path):
        dirs[:] = [d for d in dirs if os.lstat(os.path.join(root, d)).st_dev == dev]
        yield root, dirs, names

def size_of(path, cap):
    """(bytes, files, over): stops counting once over `cap` bytes, so a huge disk is cheap to rule out."""
    total = files = 0
    for root, dirs, names in walk_one_fs(path):
        for n in names:
            try:
                total += os.lstat(os.path.join(root, n)).st_size
            except OSError:
                continue
            files += 1
            if total > cap:
                return total, files, True
    return total, files, False

def git_of(path):
    try:
        r = subprocess.run(["git", "-C", path, "status", "--porcelain", "-b"], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, universal_newlines=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or os.path.realpath(path) == os.path.realpath(HOME):
        return None
    lines = r.stdout.splitlines()
    return {"branch": lines[0][3:].split("...")[0] if lines else "", "dirty": max(0, len(lines) - 1)}

if op == "inspect":
    out = {"home": HOME, "sessions": {}, "sizes": {}, "exists": {}, "logins": {}}
    for s in arg.get("sessions", []):
        config = os.path.expanduser(s["config"])
        paths = [s["path"], s["path"][:-6],  # transcript, and its folder of subagents / tool results
                 os.path.join(config, "file-history", s["id"]), os.path.join(config, "session-env", s["id"]),
                 os.path.join(os.path.dirname(s["path"]), "memory")]
        first = None  # the folder the session was started in: Claude Code files it under that one
        try:
            with open(s["path"], errors="replace") as f:
                for line in f:
                    if '"cwd"' in line:
                        try:
                            first = json.loads(line).get("cwd")
                        except ValueError:
                            continue
                        if first:
                            break
        except OSError:
            pass
        out["sessions"][s["id"]] = {"paths": [p for p in paths if os.path.exists(p)], "start": first}
    for p in arg.get("sizes", []):
        if os.path.isdir(p):
            size, files, over = size_of(p, arg.get("cap", 1 << 40))
            out["sizes"][p] = {"bytes": size, "files": files, "over": over, "git": git_of(p),
                               "mount": os.path.ismount(p)}
    for p in arg.get("exists", []):
        out["exists"][p] = os.path.lexists(p)
    out["writable"] = {}
    for p in arg.get("writable", []):  # can this folder be created (or written to)?
        q = p
        while not os.path.exists(q) and q != os.path.dirname(q):
            q = os.path.dirname(q)
        out["writable"][p] = os.access(q, os.W_OK)
    for c in arg.get("configs", []):
        out["logins"][c] = os.path.exists(os.path.join(os.path.expanduser(c), ".credentials.json"))
    emit(out)
elif op == "pack":  # tar.gz of these paths (files or folders) on stdout, names without the leading "/"
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as tar:
        for p in arg["paths"]:
            try:
                if not os.path.isdir(p) or os.path.islink(p):
                    tar.add(p, arcname=p.lstrip("/"))
                    continue
                for root, dirs, names in walk_one_fs(p):
                    for x in [root] + [os.path.join(root, n) for n in names] + \
                             [os.path.join(root, d) for d in dirs if os.path.islink(os.path.join(root, d))]:
                        try:
                            tar.add(x, arcname=x.lstrip("/"), recursive=False)
                        except OSError as e:
                            sys.stderr.write("pack: skipped %s\n" % e)
            except OSError as e:
                sys.stderr.write("pack: %s\n" % e)
elif op == "unpack":  # extract a tar from stdin under the allowed roots; never replace anything
    roots = [os.path.normpath(r) for r in arg["roots"]]
    written = skipped = failed = 0
    errors = []
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as tar:
        for m in tar:
            dest = os.path.normpath("/" + m.name)
            if not any(dest == r or dest.startswith(r + "/") for r in roots) or ".." in m.name.split("/"):
                failed += 1
                errors.append("outside the allowed folders: %s" % dest)
                continue
            if m.isdir():
                os.makedirs(dest, exist_ok=True)
                continue
            if os.path.lexists(dest):
                skipped += 1
                continue
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                if hasattr(tarfile, "tar_filter"):
                    tar.extract(m, "/", filter="tar")
                else:
                    tar.extract(m, "/")
                written += 1
            except (OSError, tarfile.TarError) as e:
                failed += 1
                errors.append(str(e))
    emit({"written": written, "skipped": skipped, "failed": failed, "errors": errors[:10]})
'''
MOVE_STATE = os.path.join(STATE_DIR, "moves.json")
MOVE_NUMBERS = os.path.join(STATE_DIR, "session-numbers.json")


def number_sessions(host, sessions):
    """Session id -> number on this host. A number, once given, is kept for good (also after the session
    is gone), so #12 means the same session in every listing; new sessions get the next ones."""
    try:
        with open(MOVE_NUMBERS) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = {}
    nums = dict(saved.get(host, {}))
    nxt = max(nums.values(), default=0) + 1
    for s in sorted(sessions, key=lambda s: (s["cwd"], s["end"])):
        if s["id"] not in nums:
            nums[s["id"]], nxt = nxt, nxt + 1
    if nums != saved.get(host):
        saved[host] = nums
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(MOVE_NUMBERS + ".tmp", "w") as f:
            json.dump(saved, f, indent=1)
        os.replace(MOVE_NUMBERS + ".tmp", MOVE_NUMBERS)
    return nums


def pick_sessions(sessions, nums, selectors, home):
    """Sessions named by number (12, #12, 12-15), id prefix (6+ hex characters) or words of their title,
    first request or folder ("osvc tax": every word must appear). Returns (chosen, [(selector, count)])."""
    def tilde(p):
        return "~" + p[len(home):] if under(p, home) else p

    chosen, counts = [], []
    for x in selectors:
        bare = x.strip().lstrip("#")
        rng = re.fullmatch(r"(\d{1,5})(?:-(\d{1,5}))?", bare)
        if rng and (x.strip().startswith("#") or len(bare) < 6):
            lo, hi = int(rng.group(1)), int(rng.group(2) or rng.group(1))
            hit = [s for s in sessions if lo <= nums.get(s["id"], 0) <= hi]
        elif re.fullmatch(r"[0-9a-f-]{6,36}", bare.lower()):
            hit = [s for s in sessions if s["id"].startswith(bare.lower())]
        else:
            words = bare.lower().split()
            hit = [s for s in sessions
                   if all(w in " ".join((s["title"], s["first_prompt"], tilde(s["cwd"]))).lower() for w in words)]
        counts.append((x, len(hit)))
        chosen += [s for s in hit if s not in chosen]
    return chosen, counts
MOVE_MAX_GB = 2.0


def move_cmd(node, cfg, is_self, op, arg):
    """Run MOVE_AGENT on node with -c, so stdin stays free for `unpack`."""
    code = "import base64;exec(base64.b64decode(%r))" % base64.b64encode(MOVE_AGENT.encode()).decode()
    args = ["python3", "-I", "-c", code, op, json.dumps(arg)]
    if is_self:
        return [sys.executable] + args[1:]
    ssh, host = ssh_to(node, cfg)
    return ssh + [host] + [shlex.quote(v) for v in args]


def move_inspect(node, cfg, is_self, arg):
    r = subprocess.run(move_cmd(node, cfg, is_self, "inspect", arg), stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=900)
    if r.returncode != 0:
        sys.exit("claude-explorer: %s: %s" % (node["name"], (r.stderr.strip().splitlines() or
                                                            ["exit %d" % r.returncode])[-1][:200]))
    return json.loads(r.stdout.strip().splitlines()[-1])


def project_dir_name(path):
    """Claude Code's folder name for a working directory under projects/."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def under(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def home_rewriter(old, new):
    """Rewrite the source home to the target's in transcript text (JSON lines): /home/a/x -> /home/b/x,
    but not /home/a.b or /home/ab."""
    if old == new:
        return None
    # Not after a letter, dot or dash, except the n / t / r of a JSON escape (\n/home/a/... in a command).
    pat = re.compile(r"(?:(?<![\w.-])|(?<=\\[ntr]))%s(?=/|\"|'|\\|\s|$|[:;,)\]}])" % re.escape(old))
    return lambda text: pat.sub(lambda m: new, text)


def move_stream(src, dst, cfg, paths, rename, roots, rewrite, label):
    """Pack `paths` on src, rename (and rewrite transcripts) here, unpack on dst. Returns dst's report."""
    import io
    import tarfile
    import threading
    pack = subprocess.Popen(move_cmd(src[0], cfg, src[1], "pack", {"paths": paths}), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    unpack = subprocess.Popen(move_cmd(dst[0], cfg, dst[1], "unpack", {"roots": roots}), stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pack_err = []
    threading.Thread(target=lambda: pack_err.extend(pack.stderr.read().decode(errors="replace").splitlines()),
                     daemon=True).start()
    moved, last = [0], [time.time()]

    def progress(n):
        moved[0] += n
        if sys.stderr.isatty() and time.time() - last[0] > 1:
            last[0] = time.time()
            sys.stderr.write("\r  %s: %s copied\033[K" % (label, human_bytes(moved[0])))
            sys.stderr.flush()

    try:
        with tarfile.open(fileobj=pack.stdout, mode="r|gz") as tin, \
                tarfile.open(fileobj=unpack.stdin, mode="w|gz") as tout:
            for m in tin:
                # Long names travel in PAX headers, which would win over the new name and size.
                m.pax_headers = {k: v for k, v in m.pax_headers.items() if k not in ("path", "linkpath", "size")}
                m.name = rename("/" + m.name).lstrip("/")
                if m.issym() and m.linkname.startswith("/"):
                    m.linkname = rename(m.linkname)
                elif m.islnk():  # hard links name another member of the archive
                    m.linkname = rename("/" + m.linkname).lstrip("/")
                if m.isfile():
                    data = tin.extractfile(m)
                    if rewrite and m.name.endswith(".jsonl"):
                        body = rewrite(data.read().decode("utf-8", "surrogateescape")).encode("utf-8", "surrogateescape")
                        m.size = len(body)
                        data = io.BytesIO(body)
                    tout.addfile(m, data)
                    progress(m.size)
                else:
                    tout.addfile(m)
    except (OSError, EOFError, tarfile.TarError) as e:
        pack.kill()
        pack_err.append(str(e))
    finally:
        try:
            unpack.stdin.close()
        except OSError:
            pass
    if sys.stderr.isatty():
        sys.stderr.write("\r\033[K")
    out, err = unpack.stdout.read(), unpack.stderr.read()  # stdin is already closed: no communicate()
    unpack.wait()
    pack.wait()
    try:
        report = json.loads(out.decode().strip().splitlines()[-1])
    except (ValueError, IndexError):
        report = {"written": 0, "skipped": 0, "failed": 1,
                  "errors": [(err.decode(errors="replace").strip().splitlines() or ["unpack failed"])[-1]]}
    if pack.returncode not in (0, None) or [e for e in pack_err if e.strip()]:
        report["failed"] = report.get("failed", 0) + 1
        report.setdefault("errors", []).extend([e for e in pack_err if e.strip()][-3:] or
                                               ["pack exited %d" % pack.returncode])
    report["bytes"] = moved[0]
    return report


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024.0


def find_host(cfg, name):
    """(node, is_self) for a host name, online, of an OS that runs the move agent."""
    try:
        me, peers = discover()
    except (OSError, subprocess.CalledProcessError, ValueError):
        local = os.uname().nodename.split(".")[0]
        return {"name": name, "dns": None, "ip": None, "os": "linux", "online": True}, name == local
    for n in [me] + peers:
        if n["name"] == name:
            if n is not me and not n["online"]:
                sys.exit("claude-explorer: %s is offline" % name)
            if n["os"] not in ("linux", "macOS"):
                sys.exit("claude-explorer: %s runs %s; moves need Linux or macOS" % (name, n["os"]))
            return n, n is me
    sys.exit("claude-explorer: no host named %s on the tailnet" % name)


def move_main(argv):
    ap = argparse.ArgumentParser(
        prog="claude-explorer move",
        description="Move Claude Code sessions from one host to another (before maintenance, say): the "
                    "transcripts, their checkpoints and memory, and the folders they work in. Shows the plan "
                    "and asks first; nothing on the source host is changed or deleted.")
    ap.add_argument("sessions", nargs="*", metavar="SESSION",
                    help="which sessions: numbers from the list (12, 12-15), words of their name, request or "
                         "folder (\"osvc tax\"), or ids (6+ first characters); none: every session of the host")
    ap.add_argument("--from", dest="src", required=True, metavar="HOST", help="host the sessions are on")
    ap.add_argument("--to", dest="dst", required=True, metavar="HOST", help="host to move them to")
    ap.add_argument("--folder", action="append", default=[], metavar="DIR",
                    help="only sessions working in DIR or below (~ is the source home); repeatable")
    ap.add_argument("--days", type=float, default=3650, help="only sessions active in the last N days")
    ap.add_argument("--max-gb", type=float, default=MOVE_MAX_GB,
                    help="don't copy a folder bigger than this (default %(default)g GB); its sessions still move")
    ap.add_argument("--no-folders", action="store_true", help="move the sessions only, copy no folders")
    ap.add_argument("--dry-run", action="store_true", help="show the plan and stop")
    ap.add_argument("--yes", action="store_true", help="don't ask before copying")
    a = ap.parse_args(argv[1:])
    cfg = load_config()
    if a.src == a.dst:
        ap.error("--from and --to are the same host")
    src, dst = find_host(cfg, a.src), find_host(cfg, a.dst)
    accounts = claude_accounts(cfg)
    names = {d: n for n, d in accounts.items()}

    # 1. The sessions of the source host (every transcript, not only the latest of a resumed chain).
    data = claude_on_host(src[0], cfg, src[1], a.days)
    if "error" in data:
        sys.exit("claude-explorer: %s: %s" % (a.src, data["error"]))
    src_home = data["home"]
    sessions = data["sessions"]
    for s in sessions:
        s["cwd"] = s["cwd"] or src_home
    nums = number_sessions(a.src, sessions)
    if a.sessions:
        sessions, counts = pick_sessions(sessions, nums, a.sessions, src_home)
        missing = [x for x, n in counts if not n]
        if missing:
            sys.exit("claude-explorer: no session matches %s on %s (see the list: --dry-run)" % (
                ", ".join(repr(x) for x in missing), a.src))
        for x, n in counts:
            if n > 1:
                print("%r matches %d sessions" % (x, n))
    if a.folder:
        roots = [os.path.normpath(f.replace("~", src_home, 1) if f.startswith("~") else f) for f in a.folder]
        sessions = [s for s in sessions if any(under(s["cwd"] or src_home, r) for r in roots)]
    if not sessions:
        sys.exit("claude-explorer: no session to move on %s" % a.src)
    for s in sessions:
        s["state"] = claude_classify(s)
    live = [s for s in sessions if s.get("live")]
    sessions = [s for s in sessions if not s.get("live")]

    # 2. What each session needs, on both hosts.
    cap = int(a.max_gb * 2**30)
    scratch = lambda p: under(p, "/tmp") and not under(p, src_home)  # Claude's scratchpads and the like
    folders = sorted({s["cwd"] for s in sessions if not scratch(s["cwd"]) and s["cwd"] != src_home})
    if a.no_folders:
        folders = []
    info = move_inspect(src[0], cfg, src[1], {"sessions": [{"id": s["id"], "path": s["path"], "config": s["config"]}
                                                           for s in sessions], "sizes": folders, "cap": cap})
    dst_probe = move_inspect(dst[0], cfg, dst[1], {"configs": sorted({s["config"] for s in sessions})})
    dst_home = dst_probe["home"]

    def translate(path):
        return dst_home + path[len(src_home):] if under(path, src_home) else path

    renames = {}  # source path prefix -> target path prefix, longest first when applied
    for s in sessions:
        old_dir = os.path.dirname(s["path"])
        # `claude --resume` finds a session from the folder it was started in (its projects/ folder name),
        # which may not be the last folder it worked in.
        start = (info["sessions"].get(s["id"]) or {}).get("start")
        project = next((p for p in (start, s["cwd"]) if p and project_dir_name(p) == os.path.basename(old_dir)), None)
        new_dir = os.path.dirname(translate(s["path"]))
        if project:
            new_dir = os.path.join(os.path.dirname(new_dir), project_dir_name(translate(project)))
        elif src_home != dst_home:
            print("warning: %s: can't tell which folder %s stands for; it keeps that name" % (
                s["id"][:8], os.path.basename(old_dir)), file=sys.stderr)
        renames[old_dir] = new_dir
        s["new_cwd"] = translate(s["cwd"])
        s["resume_in"] = translate(project or s["cwd"])
        s["new_path"] = os.path.join(new_dir, os.path.basename(s["path"]))
    renames[src_home] = dst_home
    order = sorted(renames, key=len, reverse=True)

    def rename(path):
        for p in order:
            if under(path, p):
                return renames[p] + path[len(p):]
        return path

    there_info = move_inspect(dst[0], cfg, dst[1], {"exists": [s["new_path"] for s in sessions] +
                                                    [translate(f) for f in folders],
                                                    "writable": [translate(f) for f in folders],
                                                    "sizes": [translate(f) for f in folders], "cap": 0})
    exists = there_info["exists"]
    copy = []
    folder_note = {}
    for f in folders:  # parents first: a copied parent brings its subfolders along
        size = info["sizes"].get(f)
        parent = next((g for g in copy if under(f, g)), None)
        if parent:
            folder_note[f] = "comes with %s" % parent
        elif exists.get(translate(f)):
            git = (there_info["sizes"].get(translate(f)) or {}).get("git")
            folder_note[f] = "already on %s%s, left as it is" % (
                a.dst, " (git %s, %d uncommitted there)" % (git["branch"], git["dirty"]) if git else "")
        elif size is None:
            folder_note[f] = "missing on %s, nothing to copy" % a.src
        elif size["over"]:
            folder_note[f] = "over %g GB: NOT copied (--max-gb, or copy it yourself)" % a.max_gb
        elif not there_info["writable"].get(translate(f)):
            folder_note[f] = "you can't create %s on %s: NOT copied" % (translate(f), a.dst)
        else:
            copy.append(f)
            folder_note[f] = "copy, %s in %d files%s" % (human_bytes(size["bytes"]), size["files"],
                                                         ", git %s, %d uncommitted" % (size["git"]["branch"],
                                                                                       size["git"]["dirty"])
                                                         if size["git"] else "")
    todo = [s for s in sessions if not exists.get(s["new_path"])]
    there = [s for s in sessions if exists.get(s["new_path"])]

    # 3. The plan.
    print("Move from %s (%s) to %s (%s)" % (a.src, src_home, a.dst, dst_home))
    def tilde(p):
        return "~" + p[len(src_home):] if under(p, src_home) else p

    def row(s, extra=""):
        return "  #%-4d %-46s %-11s %-4s %s%s" % (nums[s["id"]], one_line(s["title"] or s["first_prompt"], 46),
                                                 s["state"].lower(), ago(s["end"]), tilde(s["cwd"]), extra)

    print("\nSessions (%d to move%s):" % (len(todo), ", %d already there" % len(there) if there else ""))
    for s in sorted(todo, key=lambda s: nums[s["id"]]):
        print(row(s))
    if live:
        print("\nStill open on %s, NOT moved (exit them first, then run this again):" % a.src)
        for s in sorted(live, key=lambda s: nums[s["id"]]):
            print(row(s, "  (pid %s)" % s["live"]["pid"]))
    if folders:
        print("\nFolders:")
        for f in folders:
            shown = f if translate(f) == f else "%s -> %s" % (f, translate(f))
            print("  %s: %s" % (shown, folder_note[f]))
    homeless = sorted({s["cwd"] for s in todo if s["cwd"] != src_home and not exists.get(translate(s["cwd"]))
                       and not any(under(s["cwd"], f) for f in copy)})
    if homeless:
        print("\nThese sessions move, but their folder won't be on %s; copy it before resuming them:" % a.dst)
        for f in homeless:
            print("  " + f)
    no_login = [c for c, ok in dst_probe["logins"].items() if not ok]
    if no_login:
        print("\nNot logged in on %s: %s (log in there with `%s` and /login)" % (
            a.dst, ", ".join(names.get(c, c) for c in no_login),
            "; ".join(resume_cmd(c).replace(" --resume", "") for c in no_login)))
    if src_home != dst_home:
        print("\nPaths under %s are rewritten to %s in the transcripts." % (src_home, dst_home))
    total = sum(info["sizes"][f]["bytes"] for f in copy)
    if not todo and not copy:
        print("\nNothing to do.")
        return
    if a.dry_run:
        print("\nDry run: nothing copied.")
        return
    if not a.yes:
        try:
            answer = input("\nCopy %d session(s) and %d folder(s) (%s) to %s? [y/N] " % (
                len(todo), len(copy), human_bytes(total), a.dst))
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes", "o", "oui"):
            print("Nothing copied.")
            return

    # 4. Copy: sessions first (small), then each folder. The target never replaces an existing file.
    rewrite = home_rewriter(src_home, dst_home)
    failed = False
    session_paths = sorted({p for s in todo for p in (info["sessions"].get(s["id"]) or {}).get("paths", [])})
    # where the target may write: its home, and account dirs that live outside it
    roots = sorted({dst_home} | {os.path.dirname(rename(p)) for p in session_paths if not under(p, src_home)})
    if session_paths:
        r = move_stream(src, dst, cfg, session_paths, rename, roots, rewrite, "sessions")
        print("sessions: %d files written, %d already there%s" % (r["written"], r["skipped"],
                                                                 ", %d FAILED" % r["failed"] if r["failed"] else ""))
        for e in r.get("errors", []):
            print("  " + e, file=sys.stderr)
        failed |= bool(r["failed"])
    for f in copy:
        r = move_stream(src, dst, cfg, [f], rename, [translate(f)], None, f)
        print("%s: %s, %d files written%s" % (translate(f), human_bytes(r["bytes"]), r["written"],
                                             ", %d FAILED" % r["failed"] if r["failed"] else ""))
        for e in r.get("errors", []):
            print("  " + e, file=sys.stderr)
        failed |= bool(r["failed"])

    # 5. Check, remember, and say how to go on.
    check = move_inspect(dst[0], cfg, dst[1], {"exists": [s["new_path"] for s in todo]})["exists"]
    arrived = [s for s in todo if check.get(s["new_path"])]
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        with open(MOVE_STATE) as fh:
            log = json.load(fh)
    except (OSError, ValueError):
        log = []
    log.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "from": a.src, "to": a.dst,
                "sessions": [s["id"] for s in arrived], "folders": copy})
    with open(MOVE_STATE, "w") as fh:
        json.dump(log, fh, indent=1)
    print("\n%d of %d session(s) are on %s. Resume them there with:" % (len(arrived), len(todo), a.dst))
    for s in sorted(arrived, key=lambda s: (s["resume_in"], s["end"])):
        print("  #%-4d %s\n        ssh %s, then: cd %s && %s" % (
            nums[s["id"]], one_line(s["title"] or s["first_prompt"], 60), a.dst, shlex.quote(s["resume_in"]),
            resume_cmd(s["config"], s["id"])))
    print("\nThe originals are still on %s; nothing there was changed." % a.src)
    if failed or len(arrived) < len(todo):
        sys.exit(1)


def main():
    argv = sys.argv[1:]
    command = argv[0] if argv and argv[0] in ("triage", "todo", "dispatch", "videos", "move") else "triage"
    if argv and argv[0] == command:
        argv = argv[1:]
    migrate_state()
    {"triage": claude_main, "todo": todo_main, "dispatch": dispatch_main,
     "videos": videos_main, "move": move_main}[command]([command] + argv)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
