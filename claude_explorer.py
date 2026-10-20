#!/usr/bin/env python3
"""Triage Claude Code sessions across every online Linux machine of the tailnet.

Sends agent.py over ssh to each machine, gathers its session summaries, and sorts
them into: running, needs input, cut off, interrupted, unfinished, not pushed, done.

Usage:
    ./claude_explorer.py                 # actionable sessions of the last 60 days
    ./claude_explorer.py --all           # include finished ones
    ./claude_explorer.py --days 14 --hosts asus,biatri-1
    ./claude_explorer.py --markdown report.md --json sessions.json

Optional config ~/.config/claude-explorer.json:
    {"exclude": ["Downloads", "bluetooth"],   # regexes on cwd or first prompt: hide
     "ssh_users": {"biatri-1": "thibault.derrien"}}
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

AGENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.py")
CONFIG = os.path.expanduser("~/.config/claude-explorer.json")

# Order is the report order; first matching rule wins.
STATES = [
    ("RUNNING", "Running now"),
    ("INPUT", "Needs your input"),
    ("LIMIT", "Cut off by usage limit"),
    ("INTERRUPTED", "Stopped mid-task"),
    ("UNFINISHED", "Announced more work, never reported back"),
    ("GIT", "Done, but git work not committed/pushed"),
    ("OPEN", "Idle session still open"),
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


def load_config():
    try:
        return json.load(open(CONFIG))
    except (OSError, ValueError):
        return {}


def tailnet_hosts():
    """(ssh target, is_self) for the local machine and every online Linux peer."""
    data = json.loads(subprocess.run(["tailscale", "status", "--json"], capture_output=True,
                                     text=True, check=True).stdout)
    hosts = [(data["Self"]["DNSName"].split(".")[0], True)]
    for peer in (data.get("Peer") or {}).values():
        if peer.get("Online") and peer.get("OS") == "linux":
            hosts.append((peer["DNSName"].split(".")[0], False))
    return hosts


def collect(host, is_self, days, users):
    cmd = [sys.executable, "-I", "-", "--days", str(days)]
    if not is_self:
        target = "%s@%s" % (users[host], host) if host in users else host
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target,
               "python3 -I - --days %s" % days]
    try:
        with open(AGENT) as agent:
            out = subprocess.run(cmd, stdin=agent, capture_output=True, text=True, timeout=180)
        if out.returncode != 0:
            return {"host": host, "error": (out.stderr.strip().splitlines() or ["failed"])[-1]}
        data = json.loads(out.stdout)
    except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
        return {"host": host, "error": str(exc)[:200]}
    data["ssh"] = host
    return data


def classify(s):
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


def one_line(text, limit):
    text = re.sub(r"\s+", " ", re.sub(r"[*`#|]", "", text)).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def gist(s):
    """The part of the last reply that says what is pending."""
    reply = re.sub(r"\s+", " ", s["last_reply"]).strip()
    sentences = re.split(r"(?<=[.?!:])\s+", reply)
    if s["state"] == "INPUT":
        asks = [x for x in sentences if ASK_RE.search(x)]
        return asks[-1] if asks else sentences[-1]
    if s["state"] == "LIMIT":
        return "Last request: " + s["last_prompt"]
    if s["state"] == "INTERRUPTED":
        return "Last request: " + s["last_prompt"]
    return " ".join(sentences[-2:])


def git_note(git):
    if not git:
        return ""
    bits = []
    if git.get("ahead"):
        bits.append("%d commit(s) not pushed" % git["ahead"])
    if git.get("dirty"):
        bits.append("%d uncommitted change(s)" % git["dirty"])
    if not git.get("upstream"):
        bits.append("no upstream")
    return "git %s: %s" % (git["branch"], ", ".join(bits)) if bits else ""


def dedupe(sessions):
    """Resumed/forked sessions repeat the same first prompt; keep the most recent."""
    groups = {}
    for s in sessions:
        groups.setdefault((s["host"], s["first_prompt"][:200]), []).append(s)
    kept = []
    for group in groups.values():
        latest = max(group, key=lambda s: s["end"])
        latest["live"] = latest.get("live") or next((s["live"] for s in group if s.get("live")), None)
        kept.append(latest)
    return kept


def age(ts):
    try:
        then = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
    except ValueError:
        return "?"
    days = (datetime.now(timezone.utc) - then).total_seconds() / 86400
    return "%dh ago" % (days * 24) if days < 1 else "%dd ago" % days


# How to finish an item, in report order (quickest first).
METHODS = [
    ("COMMAND", "Run a command yourself"),
    ("GIT", "Commit / push git"),
    ("ANSWER", "Answer the session"),
    ("RESTART", "Restart the task in a new session"),
    ("WAIT", "Running, just wait"),
    ("CLOSE", "Idle, can be closed"),
    ("DONE", "Done"),
]
CMD_RE = re.compile(r"(^|\n)\s*`*! \S|run (it|this|these|them) yourself|in your own terminal|"
                    r"you need to run|dans votre propre terminal|lance[rz]? (cette|la) commande", re.I)


def method(s):
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


def command_in(reply):
    """First shell command suggested in a reply (a `! cmd` line or a code block line)."""
    for line in reply.splitlines():
        line = line.strip().strip("`")
        if line.startswith("! "):
            return line[2:]
    blocks = re.findall(r"```[a-z]*\n(.*?)```", reply, re.S)
    for block in reversed(blocks):
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
        return git_note(s.get("git")).split(": ", 1)[-1]
    if how == "RESTART":
        return ("[%s] " % s["state"].lower()) + s["last_prompt"]
    if how in ("CLOSE", "DONE"):
        return s["title"] or s["first_prompt"]
    return gist(s)


def project(s):
    home = s.get("home", "~")
    if os.path.realpath(s["cwd"] or home) == os.path.realpath(home):
        return "~ " + (s["title"] or s["first_prompt"])
    return os.path.basename(s["cwd"].rstrip("/"))


def table(rows, markdown, w, rest):
    """rows: lists of [host, project, age, next]; w: widths of the first three columns."""
    head = ["Host", "Project", "Last", "Next step"]
    if markdown:
        out = ["| " + " | ".join(head) + " |", "|---|---|---|---|"]
        for r in rows:
            out.append("| " + " | ".join(one_line(c, 300).replace("|", "/") for c in r) + " |")
        return out
    fmt = "  %-{}s  %-{}s  %-{}s  %s".format(*w)
    out = [fmt % tuple(head), fmt % tuple("-" * n for n in w + [min(rest, 40)])]
    for r in rows:
        out.append(fmt % (one_line(r[0], w[0]), one_line(r[1], w[1]), r[2], one_line(r[3], rest)))
    return out


def render(sessions, errors, show_all, markdown, by="method"):
    if by == "host":
        groups = [(h, h) for h in sorted({s["host"] for s in sessions})]
    elif by == "state":
        groups = STATES
    else:
        groups = METHODS
    shown = [s for s in sessions if show_all or s["method"] != "DONE"]
    seen, unique = set(), []
    for s in sorted(shown, key=lambda s: s["end"], reverse=True):
        key = (s["host"], s["cwd"]) if s["method"] == "GIT" else s["id"]
        if key not in seen:  # one git row per folder
            seen.add(key)
            unique.append(s)
    rows = {s["id"]: [s["host"], project(s), age(s["end"]).replace(" ago", ""), next_step(s)]
            for s in unique}
    caps = [10, 28, 4]
    w = [min(cap, max([len(h)] + [len(r[i]) for r in rows.values()]))
         for i, (cap, h) in enumerate(zip(caps, ["Host", "Project", "Last"]))]
    rest = max(30, shutil.get_terminal_size((140, 24)).columns - sum(w) - 8)
    lines = []
    for key, label in groups:
        group = [s for s in unique if s[by] == key]
        if not group:
            continue
        group.sort(key=lambda s: (s["host"], s["end"]) if by != "host"
                   else ([m for m, _ in METHODS].index(s["method"]), s["end"]))
        title = "%s (%d)" % (label, len(group))
        lines.append("\n## " + title if markdown else "\n" + title + "\n" + "=" * len(title))
        lines.extend(table([rows[s["id"]] for s in group], markdown, w, rest))
    for host, data in sorted(errors.items()):
        lines.append("\n! %s unreachable: %s" % (host, data))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=float, default=60, help="look back this many days (60)")
    parser.add_argument("--hosts", help="comma-separated ssh names instead of the tailnet")
    parser.add_argument("--all", action="store_true", help="also list finished sessions")
    parser.add_argument("--markdown", metavar="FILE", help="also write the report as Markdown")
    parser.add_argument("--json", metavar="FILE", help="also write raw session data")
    parser.add_argument("--by", choices=["method", "state", "host"], default="method",
                        help="group by how to finish it (default), session state, or machine")
    args = parser.parse_args()

    config = load_config()
    if args.hosts:
        local = os.uname().nodename
        hosts = [(h, h == local) for h in args.hosts.split(",")]
    else:
        hosts = tailnet_hosts()
    with ThreadPoolExecutor(len(hosts)) as pool:
        results = list(pool.map(
            lambda h: collect(h[0], h[1], args.days, config.get("ssh_users", {})), hosts))

    exclude = [re.compile(p, re.I) for p in config.get("exclude", [])]
    sessions, errors = [], {}
    for data in results:
        if "error" in data:
            errors[data["host"]] = data["error"]
            continue
        for s in data["sessions"]:
            if any(p.search(s["cwd"]) or p.search(s["first_prompt"]) for p in exclude):
                continue
            s["host"], s["home"] = data["ssh"], data.get("home", "~")
            sessions.append(s)
    sessions = dedupe(sessions)
    for s in sessions:
        s["state"] = classify(s)
        s["method"] = method(s)

    print("Claude sessions on %s (last %g days)" % (", ".join(h for h, _ in hosts), args.days))
    print(render(sessions, errors, args.all, markdown=False, by=args.by))
    if args.markdown:
        with open(args.markdown, "w") as out:
            out.write("# Claude sessions — %s\n" % datetime.now().strftime("%Y-%m-%d %H:%M"))
            out.write(render(sessions, errors, args.all, markdown=True, by=args.by) + "\n")
    if args.json:
        with open(args.json, "w") as out:
            json.dump(sessions, out, indent=1)


if __name__ == "__main__":
    main()
