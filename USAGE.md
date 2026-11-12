# Claude Explorer

Find, triage and dispatch [Claude Code](https://claude.com/claude-code) work across all your machines.

- **Triage.** Lists the Claude Code sessions on every host and groups the unfinished ones by what you have to
  do to finish them: run a command, push git, answer, restart, wait or close.
- **Todoist tasks and points.** `todo` turns pending sessions into [Todoist](https://todoist.com) tasks and
  gives you points for finishing them.
- **Agents from Todoist.** `dispatch` goes the other way: Todoist tasks labelled `@claude` are handed to
  unattended Claude Code agents on your machines, spread over several Claude accounts (for example the seats
  of a Team plan), and their reports come back as comments.
- **Moving sessions.** `move` carries Claude Code sessions, and the folders they work in, to another
  host before a machine goes down for maintenance. It only runs when you type it, and asks first.
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
`claude-explorer todo` from cron or a systemd timer (see [Running from cron](#running-from-cron)).

## Todoist tasks done by agents

```sh
claude-explorer dispatch --dry-run   # which @claude tasks would start, and where
claude-explorer dispatch             # start agents, post the reports of those that stopped
claude-explorer dispatch --report-only  # post the reports of agents that stopped, start nothing
claude-explorer dispatch --status    # agents still running, and accounts resting after a usage limit
claude-explorer dispatch --accounts  # which Claude accounts are logged in on each host
```

Give a Todoist task the label `@claude`, or put it in the project `Claude-Tasks`, and run
`claude-explorer dispatch` (by hand, or from cron). It starts `claude -p` on a host,
detached, with the task title and description as the request:

- **Labels:** real Todoist labels or `@words` typed in the title (`@claude @asus Fix the plots`) both count,
  in any case.
- **Where:** a second label naming a host (`@asus`) picks the machine, otherwise the local one. A line
  `dir: ~/Documents/project` in the description, or a description that starts with a path, picks the folder
  (created if missing), otherwise the home directory.
- **What it may do:** read and edit files (`--permission-mode acceptEdits`). By default it has no shell
  (`--disallowedTools Bash`): when the task needs a command or a decision, it says so in its report. On hosts
  listed in `dispatch_shell` it gets a restricted shell (see below).
- **While it runs:** the label becomes `@claude-running`, and a comment gives the host, folder and session ID.
  The triage shows the session like any other.
- **Order:** a line `after: <part of another task's title>` in the description makes the task wait until
  every open task matching it is `@claude-done`.
- **When it stops:** the next run posts its final reply as a comment, with the cost and the command to resume
  the session, sets the label to `@claude-done` (or `@claude-failed`), and makes the task due today so it
  shows up in Todoist's Today view. You read it and close the task.

[TODO.md](TODO.md) is the full guide to writing tasks for agents.

At most `dispatch_max` agents run at once per account (default 3); the others wait for the next run. To run
a task again, remove its `@claude-done` or `@claude-failed` label.

### Running jobs: a restricted shell on chosen hosts

To let agents submit and check SLURM jobs themselves, list the hosts in the config:

```json
{"dispatch_shell": {"biatri-1": true, "biatri-2": true}}
```

`true` allows commands starting with `sbatch`, `squeue`, `sacct`, `sinfo`, `scontrol show job`, `ls`, `cat`,
`head`, `tail`, `grep`, `wc`, `diff`, `mkdir`, `cp` and `scp`; a list (`["sbatch", "squeue"]`) allows exactly those.
Everything else is refused, without asking, because nobody is there to approve it. Even when a prefix would
allow them, `rm`, `mv`, `scancel`, `sudo`, `ssh`, `curl`, `wget`, `chmod` and `git push` are refused,
and so is any command containing `$VARIABLES`, `$(...)` or backquotes: the agent is told its user name and
home directory, and writes them out. Chains such as `squeue && rm x` are refused when any part is.

An agent can't wait for long jobs: it submits them, checks they are queued, and ends its report with the job
IDs and how to check them. `scp` uses the host's own SSH keys and overwrites files at the destination without
asking, so list it only for hosts on a network you trust. Hosts not listed keep agents without a shell.

### Running from cron

Cron's `PATH` doesn't include `~/.local/bin`, where `claude-explorer` is linked, so set it in the line:

```
0 8 * * * PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin claude-explorer dispatch >> $HOME/.local/state/claude-explorer/dispatch.log 2>&1
0 * * * * PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin claude-explorer todo >> $HOME/.local/state/claude-explorer/todo.log 2>&1
```

Cron has no SSH agent either: the hosts must accept a key ssh finds on its own (`~/.ssh/config` or the
default key files). A run that falls on a time when the machine is off is skipped, not caught up. Reports of
agents that finished are posted on the next run, so a daily `dispatch` posts them the following day.

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

## Terminal videos in one folder

`videos` looks through every host for videos (`.mp4 .mkv .webm .mov .avi .ogv .m4v`, GIFs over 1 MB and
asciinema `.cast` files) and keeps the ones that look like a terminal: CLI tutorials, screencasts of Claude
Code sessions, demos. They are all copied into one folder.

```sh
claude-explorer videos --dry-run            # list what would be copied
claude-explorer videos                      # copy into ~/Videos/CLI-tutorials
claude-explorer videos --all --dry-run      # also list the videos that were rejected, with their score
claude-explorer videos --roots ~,/media/thibault --dest ~/CLI-videos
```

Four frames of each video are decoded at 960x540 with ffmpeg. Each frame is cut into 48 tiles; a tile
looks like a terminal if it has a flat dark background, sharp text and none of the soft gradients of camera
footage. The frame's columns are also searched for evenly spaced lines of text. A frame counts as terminal
with at least 20 % such tiles and 10 text lines, or with 20 text lines, so a terminal beside a browser still
counts while night scenes and dark renderings don't. A video is kept when at least `--min-score` of its
frames count (default 25 %). Hosts without ffmpeg stream their videos over SSH to the host with ffmpeg and
the most free temporary space (videos over `--max-mb` are skipped). The measurements are cached in
`~/.local/state/claude-explorer/videos.json`, so later runs only look at new or changed files. Copies of the
same video found in several folders (same name and size) are copied once.

Every video keeps its file name; a different video with the same name gets ` (2)`. A video with the same
name and size as one already copied counts as a duplicate and isn't copied again. Where each file came from
is recorded in `.claude-explorer-videos.json` in the destination folder. Light-themed terminals aren't
detected.

## Moving sessions to another host

Before a machine goes down for maintenance, `move` carries its Claude Code sessions to another one, so you
can go on with `claude --resume` there. It only ever runs when you type it: it prints the plan, asks, copies,
and leaves everything on the source host as it was.

```sh
claude-explorer move --from bumblefoot --to biatri-1 --dry-run       # the plan: every session, numbered
claude-explorer move --from bumblefoot --to biatri-1                 # every session, after asking
claude-explorer move 12 40-45 --from bumblefoot --to luzicka         # by number from the list
claude-explorer move "osvc tax" --from bumblefoot --to asus          # by words of the name
claude-explorer move --from bumblefoot --to luzicka --folder ~/Documents/TailMon   # one project's sessions
```

A session is chosen by its **number** in the list (`12`, `#12`, or a range like `40-45`), by **words** of
its name (the title, including one given with `/rename`), its first request or its folder (`"osvc tax"`:
every word must appear, and all matching sessions are taken), or by the first 6 or more characters of
its **id**. Numbers are kept for good on each host
(`~/.local/state/claude-explorer/session-numbers.json`): a session keeps its number from one run to the
next, and new sessions get the next free ones. Whatever you pick, the plan lists the sessions first, and
nothing is copied until you answer yes.

For each session it copies the transcript, its subagents and tool results, its rewind checkpoints
(`file-history`), its `session-env` and the project's memory, into the same Claude account (config dir) on
the target. The folder a session works in comes along unless:

- **it already exists on the target.** It's left as it is and never merged. The plan shows its git branch
  and uncommitted changes there, so you can sync with git yourself.
- **it's over `--max-gb`** (default 2 GB).
- **you can't create it on the target**, e.g. `/media/...`.
- **it's in `/tmp`**, a scratchpad.
- **you gave `--no-folders`.**

A folder is copied whole, with its `.git` and uncommitted changes, but it never goes into other mounts
below it (sshfs and the like). Subfolders come along with their parent. The plan lists the sessions whose
folder won't be on the target, so you can copy those yourself before resuming them.

When the home folders differ (`/home/thibault` here, `/home/thibault.derrien` there), paths under the old
home are rewritten in the transcripts, and each session is filed under the project folder `claude --resume`
looks in: the one it was started in. Your own files are copied unchanged.

Sessions still open on the source host (running or idle, with a live `claude` process) are refused: exit
them and run `move` again. The target never replaces a file that already exists, so running `move` twice
copies nothing new, and memory files already on the target win. If the target's account isn't logged in,
the plan says so. At the end, `move` checks that every transcript arrived, prints the `ssh` / `cd` /
`claude --resume` line for each, and logs the move in `~/.local/state/claude-explorer/moves.json`.

The copy goes through the machine running `move` (a tar stream from one SSH connection to the other), so
the two hosts need no SSH access to each other. Linux and macOS hosts. Nothing is ever deleted: once
you've checked the sessions on the new host, remove the originals yourself if you want to. Not moved: the
`↑` prompt history (`~/.claude/history.jsonl`), running jobs and processes, and files outside the
session's folder.

## How TailMon and ClaudeExplorer fit together

Coupling matrix (design structure matrix): the same components label rows and columns, and
**M<sub>ij</sub> = 1 when component i uses component j**.

```
                      TailMon │  ClaudeExplorer  │      external
                         1    │   2    3    4    │  5   6   7   8   9
                       ┌──────┼──────────────────┼──────────────────
 1 TailMon             │  ■   │   ·    ·    ·    │  ·   1   ·   1   ·
                       ├──────┼──────────────────┼──────────────────
 2 CE triage           │  ·   │   ■    ·    ·    │  ·   1   ·   1   1
 3 CE todo             │  ·   │   1    ■    ·    │  ·   1   1   1   1
 4 CE dispatch         │  ·   │   ·    ·    ■    │  ·   1   1   1   1
                       ├──────┼──────────────────┼──────────────────
 5 cron 08:00          │  ·   │   ·    ·    1    │  ■   ·   ·   ·   ·
 6 Tailscale + SSH     │  ·   │   ·    ·    ·    │  ·   ■   ·   ·   ·
 7 Todoist             │  ·   │   ·    ·    ·    │  ·   ·   ■   ·   ·
 8 tailmon.json        │  ·   │   ·    ·    ·    │  ·   ·   ·   ■   ·
 9 claude-explorer.json│  ·   │   ·    ·    ·    │  ·   ·   ·   ·   ■
```

1 is `tailmon` with all its commands (monitoring, `search`, `grep`, `mount`, …); 2–4 are `claude-explorer`,
`claude-explorer todo` and `claude-explorer dispatch` (CE = ClaudeExplorer); 5 is a cron line running
`claude-explorer dispatch`.

- **No coupling between the two programs.** Both off-diagonal blocks between TailMon (row/column 1) and
  ClaudeExplorer (2–4) are empty: each runs without the other.
- **What they share** is outside both: Tailscale + SSH to reach the machines (column 6), and
  `~/.config/tailmon.json` (column 8), which ClaudeExplorer still reads because it can hold the Todoist token.
  With the token in `~/.config/claude-explorer.json`, column 8 empties for ClaudeExplorer.
- **Inside ClaudeExplorer,** `todo` builds on the triage (M<sub>32</sub>); `dispatch` stands alone.
- **Cron** calls `claude-explorer dispatch` directly (M<sub>54</sub>).

## Configuration

Optional, in `~/.config/claude-explorer.json`. Any key you set overrides the built-in default. The same keys
are also read from `~/.config/tailmon.json`, if it exists; `claude-explorer.json` wins.

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
| `dispatch_due_today`  | For `dispatch`: make a task due today when its report is posted, unless it has a due date (default `true`) |
| `dispatch_shell`      | For `dispatch`: hosts where agents get a restricted shell: `true` or a list of commands (default none) |

SSH connects with `BatchMode=yes` (it never prompts) and `StrictHostKeyChecking=accept-new`. With Tailscale,
the host's tailnet address is pinned as `HostName`, so `Host <name>` entries in `~/.ssh/config` still apply.

State (points, open tasks, running agents) is kept in `~/.local/state/claude-explorer/`. State left by
`tailmon todo` and `tailmon dispatch` is copied there on the first run, and agents started by tailmon are
still followed.

## Limits

- Grouping relies on keywords in each session's last reply, so a few sessions end up in the wrong group.
- Offline machines and Windows machines are skipped.

## License

[PolyForm Noncommercial 1.0.0](LICENSE.md). Free for personal use and for charitable, educational, public
research and government organizations. Any commercial use, including inside a company, needs a commercial
license: contact the author through [GitHub](https://github.com/tjyderrien).
