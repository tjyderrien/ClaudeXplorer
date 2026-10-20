# Claude Explorer

Lists the Claude Code sessions on every machine of your Tailscale network and groups them by how to finish them.

## Requirements

- Python 3.9 or newer on every machine. Only the standard library is used.
- Tailscale on the machine where you run it.
- SSH login with a key, no password, to each machine (`ssh <host>` must work without prompting).

Nothing needs to be installed on the other machines: `agent.py` is sent over SSH each time.

## Usage

```
./claude_explorer.py                      # open items from the last 60 days, grouped by method
./claude_explorer.py --by host            # one table per machine
./claude_explorer.py --by state           # grouped by session state
./claude_explorer.py --all --days 14      # include finished sessions, last 14 days
./claude_explorer.py --hosts asus,biatri-1
./claude_explorer.py --markdown report.md --json sessions.json
```

Without `--hosts`, it uses the online Linux machines from `tailscale status`, including this one.

## Groups

| Group | Meaning |
|---|---|
| Run a command yourself | The session asked you to run something (often `sudo` or `ssh-add`). The command is shown. |
| Commit / push git | Work is finished but the folder has uncommitted changes or commits not pushed. |
| Answer the session | The session ended on a question. |
| Restart the task in a new session | Cut off by a usage limit, stopped mid-task, or promised to report back and never did. The last request is shown. |
| Running, just wait | The session is working right now. |
| Idle, can be closed | The session is still open but has nothing pending. |
| Done | Shown only with `--all`. |

To pick a session back up, go to its folder on that machine and run `claude --resume`.

## Configuration

Optional file `~/.config/claude-explorer.json`:

```json
{
  "exclude": ["Downloads", "bluetooth"],
  "ssh_users": {"biatri-1": "thibault.derrien"}
}
```

- `exclude`: regular expressions. A session is hidden when its folder or first request matches one.
- `ssh_users`: the login to use on a host, when it isn't already set in `~/.ssh/config`.

## Limits

- Grouping relies on keywords in each session's last reply, so a few sessions end up in the wrong group.
- Offline machines and Windows machines are skipped.
- When a task was resumed in several sessions, only the most recent is shown.
