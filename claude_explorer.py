#!/usr/bin/env python3
"""claude-explorer — find, triage and dispatch Claude Code work across your machines.

Hosts are discovered with `tailscale status --json` (or given with --hosts). Each Linux host is read
over SSH by piping a small stdlib-only Python script to `python3 -` (nothing is installed remotely);
the local machine is read directly.

    claude-explorer               Claude Code sessions on every host, grouped by how to finish them
    claude-explorer todo          ... as Todoist tasks, with points for finishing them
    claude-explorer dispatch      hand Todoist tasks labelled @claude to Claude Code agents

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
    rows = {s["id"]: [s["node"], project(s), ago(s["end"]), next_step(s)] for s in unique}
    head = ["Host", "Project", "Last", "Next step"]
    w = [min(cap, max([len(head[i])] + [len(r[i]) for r in rows.values()])) for i, cap in enumerate([16, 28, 4])]
    rest = max(30, shutil.get_terminal_size((140, 24)).columns - sum(w) - 8)
    fmt = "  %-{}s  %-{}s  %-{}s  %s".format(*w)
    order = [m for m, _ in CLAUDE_METHODS]
    lines = []
    for key, label in groups:
        group = [s for s in unique if s[key_of] == key]
        if not group:
            continue
        group.sort(key=lambda s: (order.index(s["method"]), s["end"]) if by == "host" else (s["node"], s["end"]))
        title = "%s (%d)" % (label, len(group))
        if markdown:
            lines += ["", "## " + title, "", "| " + " | ".join(head) + " |", "|---|---|---|---|"]
            lines += ["| " + " | ".join(one_line(x, 300).replace("|", "/") for x in rows[s["id"]]) + " |"
                      for s in group]
        else:
            lines += ["", title, fmt % tuple(head),
                      fmt % tuple("-" * n for n in w + [min(rest, 40)])]
            for s in group:
                r = rows[s["id"]]
                lines.append(fmt % (one_line(r[0], w[0]), one_line(r[1], w[1]), r[2], one_line(r[3], rest)))
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
    a = ap.parse_args(argv[1:])
    cfg = load_config()
    only = set(x for x in a.hosts.split(",") if x) if a.hosts else None
    sessions, errors, skipped, targets = claude_sessions(cfg, only, a.days)

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


def todoist(token, method, path, body=None):
    req = urllib.request.Request(TODOIST_API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise TodoistError("%s %s: HTTP %d %s" % (method, path, exc.code, exc.read()[:200].decode(errors="replace")))
    except (urllib.error.URLError, OSError) as exc:
        raise TodoistError("%s %s: %s" % (method, path, exc))
    return json.loads(raw) if raw else {}


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
    user = cfg["users"].get(node["name"], cfg["default_user"])
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
           "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=5",
           "-o", "ServerAliveCountMax=3"]
    if cfg.get("identity"):
        ssh += ["-i", os.path.expanduser(cfg["identity"])]
    if node["dns"] or node["ip"]:
        ssh += ["-o", "HostName=%s" % (node["dns"] or node["ip"])]
    host = "%s@%s" % (user, node["name"]) if user else node["name"]
    # ssh passes the command through the remote login shell.
    return ssh + [host] + [shlex.quote(value) for value in ["python3", "-I", "-"] + args]


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
    ap.add_argument("--status", action="store_true", help="only list the agents still running")
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
            print("%-12s %-16s %-10s %s  %s" % (run["started"], run["node"], run.get("account", "default"),
                                                 run["dir"], run["content"]))
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
                print("would report  %s (%s)" % (run["content"], outcome))
                continue
            if todoist(token, "GET", "/tasks/" + task_id):
                todoist(token, "POST", "/comments", {"task_id": task_id, "content": comment})
                # A usage limit isn't the task's fault: put it back in the queue for another account.
                relabel(token, task_id, (label, label + "-running"),
                        label if outcome == "limit" else label + "-" + outcome)
            if outcome == "limit":
                state["rest"][run.get("account", "default")] = time.time() + cfg["dispatch_rest_hours"] * 3600
            print("%-6s  %s (account %s)" % (outcome, run["content"], run.get("account", "default")))
            del state["running"][task_id]
            save()

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
            lines = []
            for line in (task.get("description") or "").splitlines():
                m = re.match(r"\s*dir:\s*(.+?)\s*$", line, re.I) or (
                    re.match(r"\s*([~/]\S*)\s*$", line) if not lines and folder == "~" else None)
                if m:  # "dir: path", or a description that starts with a bare path
                    folder = m.group(1)
                elif line.strip() or lines:
                    lines.append(line)
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
                print("waiting  %s (no free account on %s: %s)" % (title, name, ", ".join(
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
            todoist(token, "POST", "/comments", {"task_id": task["id"], "content":
                    "Agent started on %s with account %s in %s%s (session %s)%s." % (
                        n["name"], account, folder, " (folder created)" if started.get("created") else "", sid,
                        ", with a shell limited to: " + ", ".join(allow) if allow else ", without a shell")})
            relabel(token, task["id"], (label,), label + "-running")
            print("started  [%s %s] %s: %s" % (n["name"], account, folder, title))
    except TodoistError as exc:
        sys.exit("claude-explorer: Todoist: %s" % exc)
    print("%d agent(s) running" % len([k for k in state["running"] if not k.startswith("dry-")]))


def main():
    argv = sys.argv[1:]
    command = argv[0] if argv and argv[0] in ("triage", "todo", "dispatch") else "triage"
    if argv and argv[0] == command:
        argv = argv[1:]
    migrate_state()
    {"triage": claude_main, "todo": todo_main, "dispatch": dispatch_main}[command]([command] + argv)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
