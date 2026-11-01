# ClaudeExplorer and Todoist

How ClaudeExplorer works with [Todoist](https://todoist.com), in both directions:

| Direction | Command | What it does |
|---|---|---|
| Claude → Todoist | `claude-explorer todo` | Every unfinished Claude Code session becomes a task in the project **Claude sessions**. Finishing it earns points. |
| Todoist → Claude | `claude-explorer dispatch` | Every task labelled **@claude**, or in the project **Claude-Tasks**, is handed to an unattended Claude Code agent. Its report comes back as a comment. |

## Setup

1. Get your API token: Todoist → Settings → Integrations → Developer.
2. Put it in `~/.config/claude-explorer.json` (or `~/.config/tailmon.json`, which is read too), or in the
   environment as `TODOIST_API_TOKEN`:

   ```json
   {"todoist_token": "0123abcd..."}
   ```

3. Check without changing anything: `claude-explorer dispatch --dry-run` and `claude-explorer todo --dry-run`.

The projects **Claude sessions** (for `todo`) and **Claude-Tasks** (for `dispatch`) are found by name; `todo`
creates its project if it's missing. Both names can be changed in the config (`todoist_project`,
`dispatch_project`).

## Writing a task for an agent

The **title** is the request. The **description** adds detail, plus a few special lines:

```
Title:        @claude @biatri-2 Converge the Al2O3 band structure (mesh and k-points)
Description:  dir: ~/quantumlap/07-Al2O3Sapphire
              after: Prepare primitive cell
              after: Introduce k-point path
              Compare with the literature gap and the Topological Quantum Chemistry database.
```

| Where | Syntax | Meaning |
|---|---|---|
| Label or title | `@claude` | Hand this task to an agent. Not needed for tasks in the project Claude-Tasks. |
| Label or title | `@<host>`, e.g. `@biatri-2` | Run on that machine. Default: the machine running `dispatch`. |
| Label or title | `@<account>`, e.g. `@seat2` | Use that Claude account (see `claude_accounts`). Default: the least busy one. |
| Description | `dir: <path>` | Folder to work in, on that machine; created if missing. A description that starts with a bare path works too. Default: the home directory. |
| Description | `after: <text>` | Wait until every open task whose title contains `<text>` (or whose ID is `<text>`) is `@claude-done`. Repeat the line for several prerequisites. |
| Description | anything else | Added to the request. |

Labels can be real Todoist labels or words typed in the title (`@claude @biatri-2 …`); the ones ClaudeExplorer
recognizes are removed from the request the agent sees. Case doesn't matter.

### Choosing the machine

An agent can read and edit files on its machine. Whether it can run commands depends on `dispatch_shell`:

| Machine | Use it for | Shell |
|---|---|---|
| `@biatri-2`, `@biatri-1` | Octopus and other SLURM jobs | Restricted: `sbatch`, `squeue`, `sacct`, `sinfo`, `scontrol show job`, file reading, `mkdir`, `cp`, `scp` |
| `@bumblefoot` (default when dispatch runs there) | DFTB+, preparing inputs, writing code and documents | None: it prepares, you run |
| other hosts | as above | None, unless listed in `dispatch_shell` |

An agent can't wait for a long job. It submits, checks the job is queued, and ends its report with the job IDs
and how to read the results. Make the next step a separate task with `after:`.

The files a task needs must be on its machine. With `scp` allowed, an agent on biatri-2 can fetch them
itself (`scp bumblefoot:<path> .`); say so in the description.

## What happens to a task

```
@claude ──▶ @claude-running ──▶ @claude-done      report posted, task due today
                │           └──▶ @claude-failed    report posted, task due today
                └── usage limit ──▶ @claude         back in the queue; that account rests dispatch_rest_hours
```

1. **Start.** A comment says where it runs: machine, account, folder, session ID and which commands it may
   use. The label becomes `@claude-running`.
2. **Report.** On the next `dispatch` run after the agent stopped, its final reply is posted as a comment,
   with the cost, the number of turns and the command to resume the session. The label becomes
   `@claude-done` or `@claude-failed`.
3. **Notification.** The task gets the due date *today*, so it shows up in Todoist's Today view and
   reminders. A due date you set yourself is kept. Turn this off with `"dispatch_due_today": false`.
4. **You** read the report and close the task. To run it again, remove `@claude-done` or `@claude-failed`.

At most `dispatch_max` agents (default 3) run at once per account; the other tasks wait for a later run.

## A pipeline of steps

Each step is a task; `after:` keeps them in order. For example:

| Task | Machine | Description |
|---|---|---|
| Identify space group of interest | bumblefoot | `dir: ~/…/07-Al2O3Sapphire` |
| Prepare primitive cell | bumblefoot | `after: Identify space group` |
| Introduce k-point path | bumblefoot | `after: Prepare primitive cell` |
| Converge band structure | biatri-2 | `dir: ~/quantumlap/07-Al2O3Sapphire`, `after: Introduce k-point path` |
| Linear response vs. Palik data | biatri-2 | `after: Converge band structure` |

With a daily `dispatch`, one step finishes per day. Run `claude-explorer dispatch` by hand to go faster.

## Running it

```sh
claude-explorer dispatch --dry-run       # what would start or be reported, and why tasks wait
claude-explorer dispatch                 # post reports, then start agents
claude-explorer dispatch --report-only   # post reports, start nothing
claude-explorer dispatch --status        # agents running, accounts resting
claude-explorer dispatch --accounts      # Claude accounts logged in on each machine
claude-explorer todo                     # sync Claude sessions into Todoist
claude-explorer todo --score             # points, level and streak
```

For cron, see "Running from cron" in [USAGE.md](USAGE.md).

## When something looks wrong

| Message | Meaning |
|---|---|
| `no open task labelled @claude …` | Nothing to start. The labels it saw are listed, to spot a typo. |
| `waiting … (no free account on <host> …)` | All accounts on that machine are busy, resting or not logged in. |
| `waiting … (after: …)` | A prerequisite isn't `@claude-done` yet; "(failed)" if it ended `@claude-failed`. |
| `Todoist: … retrying in Ns` | A temporary Todoist error; the call is retried for about 2 minutes. |
| `not reported yet, will try again next run` | Todoist kept failing; the report is posted on the next run, once. |

Points, open tasks and running agents are kept in `~/.local/state/claude-explorer/`.
