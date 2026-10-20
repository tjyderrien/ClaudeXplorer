"""Runs on each machine (sent over ssh stdin): summarizes local Claude Code sessions as JSON.

Standard library only, Python 3.9+. Usage: python3 -I - --days N < agent.py
"""
import glob
import json
import os
import re
import socket
import subprocess
import sys
import time

DAYS = 60
if "--days" in sys.argv:
    DAYS = float(sys.argv[sys.argv.index("--days") + 1])

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


def live_sessions():
    """Session id -> registry entry, for processes that are still alive."""
    live = {}
    for path in glob.glob(os.path.join(HOME, ".claude/sessions/*.json")):
        try:
            entry = json.load(open(path))
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
            out = subprocess.run(["git", "-C", cwd, "status", "-sb", "--porcelain"],
                                 capture_output=True, text=True, timeout=20)
            if out.returncode == 0:
                lines = out.stdout.splitlines()
                branch = lines[0][3:] if lines else ""
                ahead = re.search(r"(?:ahead|devant) (\d+)", branch)
                state = {"branch": branch.split("...")[0],
                         "dirty": len(lines) - 1,
                         "ahead": int(ahead.group(1)) if ahead else 0,
                         "upstream": "..." in branch}
        except (OSError, subprocess.SubprocessError):
            pass
    _git_cache[cwd] = state
    return state


def summarize(path, live):
    prompts, last_reply, title, cwd = [], "", "", ""
    first_ts = last_ts = ""
    last_kind = None  # "reply", "tool_use", "tool_result", "prompt"
    for line in open(path, errors="replace"):
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
        "start": first_ts, "end": last_ts or time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.gmtime(os.path.getmtime(path))),
        "prompts": len(prompts), "first_prompt": prompts[0][:400],
        "last_prompt": prompts[-1][:400], "last_reply": last_reply[-1500:],
        "last_kind": last_kind,
        "live": {"pid": entry["pid"], "status": entry.get("status"), "name": entry.get("name"),
                 "kind": entry.get("kind")} if entry else None,
        "git": git_state(cwd),
    }


def main():
    live = live_sessions()
    cutoff = time.time() - DAYS * 86400
    sessions = []
    for path in glob.glob(os.path.join(HOME, ".claude/projects/*/*.jsonl")):
        if os.path.getmtime(path) < cutoff:
            continue
        try:
            summary = summarize(path, live)
        except OSError:
            continue
        if summary:
            sessions.append(summary)
    disk = os.statvfs(HOME)
    json.dump({"host": socket.gethostname(), "home": HOME, "sessions": sessions,
               "home_free_gb": round(disk.f_bavail * disk.f_frsize / 1e9, 1)}, sys.stdout)


main()
