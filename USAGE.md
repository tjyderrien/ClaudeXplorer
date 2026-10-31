# Claude Explorer

Find, triage and dispatch [Claude Code](https://claude.com/claude-code) work across all your machines.

- **Triage.** Lists the Claude Code sessions on every host and groups the unfinished ones by what you have to
  do to finish them: run a command, push git, answer, restart, wait or close.
- **Todoist tasks and points.** `todo` turns pending sessions into [Todoist](https://todoist.com) tasks and
  gives you points for finishing them.
- **Agents from Todoist.** `dispatch` goes the other way: Todoist tasks labelled `@claude` are handed to
  unattended Claude Code agents on your machines, spread over several Claude accounts (for example the seats
  of a Team plan), and their reports come back as comments.
- **Zero install on the other hosts.** Hosts are discovered with `tailscale status --json` (or named with
  `--hosts`); each one is read over SSH by piping a small standard-library-only Python script to
  `python3 -`. Nothing is copied or left behind. The local machine is read directly.
- **Single file, no dependencies.** Python 3.9 or newer, and the `ssh` CLI.

## Requirements

- Python 3.9 or newer on every machine. Only the standard library is used.
- SSH login with a key, no password, to each machine (`ssh <host>` must work without prompting).
- [Tailscale](https://tailscale.com) to find the hosts automatically; without it, name them with `--hosts`.
- For `todo` and `dispatch`: a Todoist account and its API token.
- For `dispatch`: Claude Code installed and logged in on the hosts that run agents.

## Installation

```sh
ln -s "$PWD/claude_explorer.py" ~/.local/bin/claude-explorer
```

## Triage

```sh
claude-explorer                         # open sessions of the last 60 days, grouped by what to do
claude-explorer --by host               # one table per machine
claude-explorer --by state              # grouped by session state
claude-explorer --all --days 14         # include finished sessions, last 14 days
claude-explorer --hosts asus,biatri-1   # only these hosts (works without Tailscale too)
claude-explorer --markdown report.md    # also write the report as Markdown
claude-explorer --json                  # raw session data
```

| Group                             | Meaning                                                                         |
|-----------------------------------|---------------------------------------------------------------------------------|
| Run a command yourself            | The session asked you to run something (often `sudo` or `ssh-add`). The command is shown. |
| Commit / push git                 | Work is finished but the folder has uncommitted changes or unpushed commits.    |
| Answer the session                | The session ended on a question.                                                |
| Restart the task in a new session | Cut off by a usage limit, stopped mid-task, or promised to report back and never did. The last request is shown. |
| Running, just wait                | The session is working right now.                                               |
| Idle, can be closed               | The session is still open but has nothing pending.                              |
| Done                              | Shown only with `--all`.                                                        |

To pick a session back up, go to its folder on that machine and run `claude --resume`. Grouping relies on
keywords in each session's last reply, so a few sessions land in the wrong group. When a task was resumed in
several sessions, only the most recent is shown.

## Todoist tasks and points

```sh
export TODOIST_API_TOKEN=...      # Todoist > Settings > Integrations > Developer
claude-explorer todo --dry-run    # what would be added or closed
claude-explorer todo              # sync: one task per pending session, in the "Claude sessions" project
claude-explorer todo --score      # points, level and streak, without syncing
claude-explorer todo --log        # every finished task and what it earned
```

Each run adds a task for every pending session (except running ones) and checks the existing tasks:

- ticked off in Todoist: you earn its points;
- the session no longer needs anything (you pushed, answered, …) but the task is still open: the task is
  closed and you earn its points too;
- deleted in Todoist: no points, and it isn't added again;
- the session got older than `--days`: closed, no points.

| Task                  | Points |
|-----------------------|-------:|
| Run a command         | 10     |
| Commit / push git     | 10     |
| Answer the session    | 15     |
| Restart the task      | 25     |
| Close an idle session | 5      |

Every 100 points is a level; the streak counts consecutive days with at least one finished task. Hosts that
are offline or unreachable are left alone, so their tasks don't count as finished. To sync regularly, run
`claude-explorer todo` from cron or a systemd timer.

## Todoist tasks done by agents

```sh
claude-explorer dispatch --dry-run   # which @claude tasks would start, and where
claude-explorer dispatch             # start agents, post the reports of those that stopped
claude-explorer dispatch --status    # agents still running, and accounts resting after a usage limit
claude-explorer dispatch --accounts  # which Claude accounts are logged in on each host
```

Give a Todoist task the label `@claude`, or put it in the project `Claude-Tasks`, and run
`claude-explorer dispatch` (by hand, or every few minutes from cron). It starts `claude -p` on a host,
detached, with the task title and description as the request:

- **Labels:** real Todoist labels or `@words` typed in the title (`@claude @asus Fix the plots`) both count,
  in any case.
- **Where:** a second label naming a host (`@asus`) picks the machine, otherwise the local one. A line
  `dir: ~/Documents/project` in the description, or a description that starts with a path, picks the folder
  (created if missing), otherwise the home directory.
- **What it may do:** read and edit files (`--permission-mode acceptEdits`). It has no shell
  (`--disallowedTools Bash`): when the task needs a command or a decision, it says so in its report.
- **While it runs:** the label becomes `@claude-running`, and a comment gives the host, folder and session ID.
  The triage shows the session like any other.
- **When it stops:** the next run posts its final reply as a comment, with the cost and the command to resume
  the session, and sets the label to `@claude-done` (or `@claude-failed`). You read it and close the task.

At most `dispatch_max` agents run at once per account (default 3); the others wait for the next run. To run
a task again, remove its `@claude-done` or `@claude-failed` label.

### Several Claude accounts

Claude Code keeps each login in its own config directory, chosen with `CLAUDE_CONFIG_DIR`. List your
accounts in the config, then log each one in once on every host that should use it:

```json
{"claude_accounts": {"seat2": "~/.claude-seat2", "seat3": "~/.claude-seat3"}}
```

```sh
CLAUDE_CONFIG_DIR=~/.claude-seat2 claude      # then /login with that account
claude-explorer dispatch --accounts           # check: "ok" where it is logged in
```

`~/.claude` is always an account too, named `default` unless you name it in the list. Each task goes to the
least busy account logged in on its host; a label with an account name (`@seat2`) pins it. When an agent
hits its usage limit, the task goes back in the queue (label `@claude` again) and that account is left alone
for `dispatch_rest_hours` (default 1). Triage and `todo` read the sessions of every account, and resume
hints include the `CLAUDE_CONFIG_DIR` they need.

## Configuration

Optional, in `~/.config/claude-explorer.json`. Any key you set overrides the built-in default. When that
file doesn't exist, the same keys are read from `~/.config/tailmon.json`.

```json
{
  "users": {"gpu-box": "alice"},
  "default_user": "admin",
  "identity": "~/.ssh/id_ed25519",
  "exclude": ["printer-pi"],
  "claude_exclude": ["Downloads"],
  "todoist_token": "0123abcd...",
  "todoist_project": "Claude sessions",
  "claude_accounts": {"seat2": "~/.claude-seat2"},
  "dispatch_label": "claude",
  "dispatch_project": "Claude-Tasks",
  "dispatch_max": 3,
  "dispatch_rest_hours": 1.0
}
```

| Key                   | Meaning                                                                              |
|-----------------------|--------------------------------------------------------------------------------------|
| `users`               | SSH user per hostname                                                                |
| `default_user`        | SSH user for hosts not listed in `users` (default `null`: your `~/.ssh/config` / local username) |
| `identity`            | SSH private key passed with `-i` (default `null`: your usual SSH keys / agent)       |
| `exclude`             | Hostnames never contacted                                                            |
| `claude_exclude`      | Regular expressions; sessions whose folder or first request matches are hidden       |
| `todoist_token`       | Todoist API token (default `null`: use `TODOIST_API_TOKEN`)                          |
| `todoist_project`     | For `todo`: project the tasks go to, created if missing (default `Claude sessions`)  |
| `claude_accounts`     | Claude accounts: name → config dir (`CLAUDE_CONFIG_DIR`); `~/.claude` is always included |
| `dispatch_label`      | For `dispatch`: label that hands a task to an agent (default `claude`)               |
| `dispatch_project`    | For `dispatch`: project whose tasks all go to agents, labelled or not (default `Claude-Tasks`) |
| `dispatch_max`        | For `dispatch`: agents running at once, per account (default 3)                      |
| `dispatch_rest_hours` | For `dispatch`: hours an account is skipped after hitting its usage limit (default 1) |

SSH connects with `BatchMode=yes` (it never prompts) and `StrictHostKeyChecking=accept-new`. With Tailscale,
the host's tailnet address is pinned as `HostName`, so `Host <name>` entries in `~/.ssh/config` still apply.

State (points, open tasks, running agents) is kept in `~/.local/state/claude-explorer/`. State left by
`tailmon todo` and `tailmon dispatch` is copied there on the first run, and agents started by tailmon are
still followed.

## Limits

- Grouping relies on keywords in each session's last reply, so a few sessions end up in the wrong group.
- Offline machines and Windows machines are skipped.
