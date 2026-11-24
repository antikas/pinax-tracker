# Command reference

This page lists every `pinax` command with the options it takes, what it prints, the fields of its `--json` result, and the exit codes it ends with.

Read [What every command shares](#what-every-command-shares) first: the entries below rely on it for the writer, JSON output and the [common exit codes](#common-exit-codes). What happens on another clone after a write, and how to recover when a write is not published, is in [sharing-and-sync.md](sharing-and-sync.md). How state is derived from the log and what `verify` can prove is in [data-model-and-integrity.md](data-model-and-integrity.md).

## Words this page uses

- An **event** is one recorded change, a single line of JSON in the log.
- The **log** is every event of the tracker, kept under `.ergon/log/`.
- A **shard** is one file in `.ergon/log/`, named after its writer, holding that writer's events.
- A **payload** is the part of an event that holds the change itself, such as an item's id and title.
- A **sequence number** (`seq` in output) is the integer an event carries to order it, one more than the largest number in the log when it was written.
- The **writer** is the `role@host` name that every event records, set with `--actor`.
- The **projection** is the board, `.ergon/board.md`, and the item pages in `.ergon/items/`, which Pinax rebuilds from the log.
- A **claim** is an event saying that a writer has started an item; a **live claim** is one that no earlier claim has superseded and no release or expiry has ended yet.
- A **gate** is the reason an item is blocked, and an **owner gate** is the class of approval it waits for, where "owner" means the person who must approve, not the writer holding the claim.
- A **decision** is the recorded answer to a blocked item, and the **process** (`flow` or `strict`) decides who may record one.
- An **annulment** is an event written by `pinax annul` that cancels the effect of one earlier event; when that event is itself an annulment, what it cancelled counts again.
- The **publish sequence** is the run that a write command makes: fetch `origin`, append the event, rebuild the projection, commit, and push.
- A **legacy board** is the Markdown board of an earlier tracker whose items were moved into Pinax, kept as a frozen archive.

The commands, in the order a user usually meets them:

- Setting up: [`init`](#pinax-init)
- Adding and moving items: [`add`](#pinax-add), [`claim`](#pinax-claim), [`release`](#pinax-release), [`done`](#pinax-done), [`block`](#pinax-block), [`park`](#pinax-park), [`priority`](#pinax-priority), [`dep add` and `dep rm`](#pinax-dep-add-and-pinax-dep-rm), [`note add`](#pinax-note-add), [`annul`](#pinax-annul), [`status id state`](#pinax-status-id-state), [`dispatch`](#pinax-dispatch), [`reconcile`](#pinax-reconcile)
- Deciding and setting policy: [`decide`](#pinax-decide), [`policy process`](#pinax-policy-process), [`policy owner-gates`](#pinax-policy-owner-gates), [`policy decision-actors`](#pinax-policy-decision-actors), [`policy note-refs`](#pinax-policy-note-refs), [`policy claim-expiry`](#pinax-policy-claim-expiry)
- Reading state: [`status`](#pinax-status), [`ready`](#pinax-ready), [`next`](#pinax-next), [`board`](#pinax-board), [`report`](#pinax-report), [`metrics`](#pinax-metrics), [`replay`](#pinax-replay)
- Checking: [`verify`](#pinax-verify), [`policy check`](#pinax-policy-check), [`doctor`](#pinax-doctor)
- Several repositories: [`overview`](#pinax-overview), [`registry`](#pinax-registry)
- The web view: [`web`](#pinax-web)

The same commands in alphabetical order: [`add`](#pinax-add), [`annul`](#pinax-annul), [`block`](#pinax-block), [`board`](#pinax-board), [`claim`](#pinax-claim), [`decide`](#pinax-decide), [`dep`](#pinax-dep-add-and-pinax-dep-rm), [`dispatch`](#pinax-dispatch), [`doctor`](#pinax-doctor), [`done`](#pinax-done), [`init`](#pinax-init), [`metrics`](#pinax-metrics), [`next`](#pinax-next), [`note`](#pinax-note-add), [`overview`](#pinax-overview), [`park`](#pinax-park), [`policy check`](#pinax-policy-check), [`policy claim-expiry`](#pinax-policy-claim-expiry), [`policy decision-actors`](#pinax-policy-decision-actors), [`policy note-refs`](#pinax-policy-note-refs), [`policy owner-gates`](#pinax-policy-owner-gates), [`policy process`](#pinax-policy-process), [`priority`](#pinax-priority), [`ready`](#pinax-ready), [`reconcile`](#pinax-reconcile), [`registry`](#pinax-registry), [`release`](#pinax-release), [`replay`](#pinax-replay), [`report`](#pinax-report), [`status`](#pinax-status), [`status id state`](#pinax-status-id-state), [`verify`](#pinax-verify), [`web`](#pinax-web).

## What every command shares

### Running a command

The program is `pinax`, and `python -m pinax` runs the same entry point.

```
usage: pinax [-h] [--root PATH] [--version]
             {init,add,claim,status,done,block,park,decide,release,policy,priority,annul,dep,ready,next,verify,note,metrics,report,dispatch,replay,board,web,overview,registry,reconcile,doctor} ...
```

A subcommand is required: `pinax` on its own prints the usage and exits 2. `pinax --version` prints `pinax` followed by the installed version and exits 0, also outside a Git repository. Every command and subcommand accepts `-h, --help`, which prints its usage and options and exits 0. `--help` and `--version` print their text unchanged even when `--json` is also given.

You may shorten any long option to an unambiguous prefix, for example `--js` for `--json` or `--ro` for `--root`. A parser error, such as a missing required option or a value outside a fixed set of choices, prints the usage and the error on standard error and exits 2:

```
usage: pinax policy process [-h] --mode {flow,strict} [--actor ACTOR]
                            [--offline] [--json]
pinax policy process: error: argument --mode: invalid choice: 'loose' (choose from 'flow', 'strict')
```

In the messages on this page, `<id>` stands for an item id, `<writer>` for the writer, and `<root>` for the repository root, so a message that names the tracker reads `in <root>/.ergon`.

### Which repository a command acts on

Every command finds the repository root with `git rev-parse --show-toplevel` from the current directory, which also works in a linked worktree. When Git is missing, or the current directory is not inside a repository, the current directory is used.

To pin the root, pass the global `--root PATH` before the subcommand, or set the `PINAX_ROOT` environment variable. `--root` wins over `PINAX_ROOT`. When the root Pinax finds differs from the pinned one (compared as normalised paths, and without case on Windows only), the command refuses with exit 1 and the refusal name `root_mismatch`, and appends nothing.

```
pinax --root ~/src/app board
```

`pinax status --repo PATH` uses PATH directly and ignores the pin.

Most commands refuse a directory that has no `.ergon/log/`. They print `pinax: .ergon/log/ not found - run 'pinax init' first.` on standard error and exit 1. Four commands behave otherwise. `init` creates the tracker. `verify` exits 0, printing `pinax verify: .ergon/log/ not found - nothing to verify.` `status` outside a tracker shows the portfolio instead (see [`status`](#pinax-status)). `replay` has no tracker check: a ref with no log gives the empty state and exits 0.

### The writer and `--actor`

The writer is the `--actor` value recorded in every event. It has the form `role@host`, for example `alex@laptop`. Before a command runs, Pinax lower-cases the part after `@` and keeps the role exactly as typed. Without `--actor`, the writer is `operator@<host name>`, with the host lower-cased, so your machine's host name enters the log and is pushed with it.

The write commands (listed under [Which commands write events](#which-commands-write-events)) refuse a writer that is not `role@host`. It must have exactly one `@`, a non-empty role and host, and no whitespace on either side. The refusal exits 2 with the sync status `invalid_actor` and appends nothing. It prints a message on standard error, and the [failure report](#the-failure-report) on standard output, with or without `--json`:

```
pinax: the actor 'alex' is not a valid handle - every mutating command requires role@host (for example builder@laptop), so the event log can tell every writer apart across machines; no item was created.
```

```
{"actor": "alex", "appended": false, "event_type": "item.created", "item_id": null, "message": "pinax: the actor 'alex' is not a valid handle - ...; no item was created", "published": false, "root": "<root>", "status": "invalid_actor", "sync_status": "invalid_actor"}
```

The commands that append events without the publish sequence (listed under [Commands that never push](#commands-that-never-push)) do not check the writer's shape.

During a test run, which is when the environment variable `PINAX_TEST_RUN` is exactly `1`, Pinax refuses every append to a log outside the temporary directory, whoever the writer is and whatever the event. The command appends nothing, prints `pinax: refusing to write to <log dir> during a test run (PINAX_TEST_RUN=1): a test may write only to a log under <temporary directory>; unset PINAX_TEST_RUN to write to a real tracker` on standard error, and exits 10. With `--json` it prints one object with `published` false, `sync_status` `test_run_refused`, `exit_code`, `error` and `command`. With the variable unset, or set to anything else, this rule does not apply.

### Offline mode

Offline mode is the run of a write command that skips the fetch and the push. Request it with `--offline` or by setting `PINAX_OFFLINE` to exactly `1`. The command still appends, regenerates the projection and commits, and it reports `committed_local` with `offline: true`. It prints on standard error:

```
pinax: offline mode was requested (--offline or PINAX_OFFLINE=1); the event is recorded locally and not published.
```

Every write command accepts `--offline` except `claim`, which has no such option and ignores `PINAX_OFFLINE`.

Because offline mode reads only the local log, an item, claim or policy that exists only on the remote is invisible to the command. The same holds whenever `origin` cannot be reached or publishes no branch.

### Messages on standard error

Write commands print their notes on standard error, before their own output on standard output. Each command that reads the log also prints on standard error the warnings the log produces, so a warning repeats on every later command until its cause is gone. An edited event, for example, is reported like this:

```
Integrity violation: event id=<event id> does not match recomputed hash (payload or envelope tampered). seq=<n> actor=<writer> type=<event type>
```

### Did the change reach the remote?

With `--json`, read `published`: it is true when this run's push was accepted, or when a repeated `decide` finds its decision already on the remote. Exit code 0 alone does not say so, because a commit made only on your machine also ends with 0.

Without `--json`, read standard error. A write command that ends with 0 and did not push prints one of these notes there:

```
pinax: origin could not be reached; the event is recorded locally and not published.
pinax: offline mode was requested (--offline or PINAX_OFFLINE=1); the event is recorded locally and not published.
pinax: origin was reached and publishes no default branch; the event is recorded locally and not published.
pinax: the push was refused because <branch> is not the origin default branch <default branch>; the event is committed locally and not published.
pinax: <repository root> is not a git repository; the event is recorded locally and not published.
```

A write that ends with any other code prints the [failure report](#the-failure-report) on standard output, and its `pushed` and `published` fields say whether the push happened. One case prints neither: a repeated `decide` whose events are already committed, when HEAD holds nothing the remote lacks, ends with 0 and no note. A script that must know for certain should therefore pass `--json`.

The commands listed under [Commands that never push](#commands-that-never-push) change files and never publish, so their output has no `published` field. Their changes reach the remote only when you commit and push them yourself.

### JSON output

For a script, parse the JSON output, and note that `status --json` names its schema (`pinax.status.v1`).

Write commands given `--json` print exactly one JSON object on standard output on every exit, and send every other line to standard error. The exit code is the same with or without `--json`. A write command answers in JSON whenever `--json` appears on its command line, even in a place the parser refuses, such as `--json=1` or `--json` before the subcommand; the parser's error then comes back as JSON.

The object always carries two fields: `sync_status`, the name of the outcome (see the [common exit codes](#common-exit-codes)), and `published` (see [Did the change reach the remote?](#did-the-change-reach-the-remote)). In a success result, `ts` is the time the event records, in UTC as `YYYY-MM-DDTHH:MM:SSZ` from your machine's clock, and `type` is the type of the event written, such as `item.claimed`.

The object takes one of three shapes:

- On exit 0 it is the command's own result, listed under each command below.
- When the publish sequence ends with any other code, it is the [failure report](#the-failure-report). The failure report is always JSON, and Pinax prints it on standard output in that case even without `--json`.
- When the command stops before the publish sequence starts, it is `{"command", "error", "exit_code", "published": false, "sync_status": <refusal name>}`. For `log_damaged` it also has `file` and `line`. For example, outside a tracker:

```
{"command": "add", "error": "pinax: .ergon/log/ not found - run 'pinax init' first.", "exit_code": 1, "published": false, "sync_status": "not_a_tracker"}
```

Keys in write results are sorted, so they appear in alphabetical order. All JSON output is ASCII-escaped, and console output never fails on a character the console cannot show.

An interrupt with Ctrl-C prints a result with `sync_status: "interrupted"` and `exit_code: 130`, and the process then ends by the interrupt itself.

Commands that are not write commands print their errors as plain text on standard error. On exit 9 and exit 10, and when the text to record holds a character UTF-8 cannot encode, every command given `--json` also prints the refusal object on standard output. Two of them still print their JSON report on standard output when they exit 1 with findings: `doctor --json`, and `policy check --json` under `strict`.

### The failure report

The failure report is the JSON object a write command prints when its publish sequence ends with a code other than 0. It holds only the keys that apply to the outcome:

| Key | Meaning |
|---|---|
| `event_type` | the type of the event the command meant to write |
| `item_id` | the item the event names; `null` for an `add` refused before an id was made |
| `actor` | the writer |
| `root` | the path of the `.ergon` directory |
| `offline` | whether offline mode was requested |
| `status` | the outcome name, the same value as `sync_status` |
| `appended` | whether the event was added to the shard |
| `exit_code`, `error` | for `log_damaged`, the exit code and the message printed on standard error |
| `file`, `line` | for `log_damaged`, the shard that holds the damaged line and the line's number |
| `committed` | whether the event was committed |
| `pushed` | whether the push was accepted |
| `remote_branch` | the remote default branch, or `null` when none was found |
| `message` | one sentence on what happened |
| `event_id` | the id of the appended event |
| `seq` | its sequence number |
| `shard` | the path of the shard the event was added to |
| `commit_message` | the subject of the commit made or attempted |
| `follow_on_event_ids` | the ids of further events the same run appended, such as the `item.parked` of `decide --effect park` |
| `git_output` | Git's own output for the failure |
| `phase` | for exit 7, the step that failed: `stage` or `commit` |
| `head_branch` | the checked-out branch, when it is not the remote default branch |
| `attempts` | how many times the push was tried |
| `remote_head` | after a rejected push, the commit the fetched remote branch pointed to |
| `annulled` | when a `claim` annulled claims of its writer on the item, what it annulled: `{target_id, target_ids, reason, item_id, output}`. `target_ids` lists every claim annulled. After a rejected `claim`, `target_id` is the claim that could not be published; otherwise it is the first waiting claim. |
| `remote_ref` | the remote branch whose committed events could not be read |
| `holder_actor`, `holder_event_id`, `claimed_at` | for `claim_held`, the writer who holds the item, their claim and its time |
| `winner_event_id`, `winner_actor` | for `claim_superseded`, the claim that holds the item, and its writer; both `null` when nobody holds it |
| `superseded_event_id`, `superseded_actor` | for `claim_superseded`, this claim and its writer |
| `intended_ts` | for exit 6, the timestamp the command would have written |
| `remote_newest_ts` | for exit 6, the timestamp of the newest live event on the remote |
| `behind_s`, `tolerance_s` | for exit 6, how many seconds `intended_ts` is behind it, and the tolerance in force |
| `offending_event_id`, `offending_actor` | for exit 6, that newest event and its writer |
| `remedy` | for exit 6, the fix Pinax suggests: annul the future-dated event, or raise `PINAX_CLOCK_TOLERANCE_S` for one run |
| `expected_ts_form` | for an unreadable timestamp, the form expected, `YYYY-MM-DDTHH:MM:SSZ` |
| `published`, `sync_status` | as in every write result |

### Common exit codes

This table holds the exit codes of the write commands, with the `sync_status` or refusal name each one carries. Each write command entry either lists all its codes or points here. Every command, read or write, exits 2 on a parser error, and the entries do not repeat that. Every command that reads the log exits 9 on a damaged line, except `verify`, which exits 1, and `overview` and `status --portfolio`, which go on as described below. Every command that appends an event exits 10 during a test run whose log is not under the temporary directory. The entries do not repeat 9 and 10 either, except the complete list for `claim`. Read commands use 0 for success and 1 for a refusal, and each entry lists its own cases.

| Code | Meaning | `sync_status` or refusal name |
|---|---|---|
| 0 | The event is committed, and pushed when the checked-out branch is the remote default branch. A local-only commit (offline, unreachable remote, no default branch, another branch checked out, no Git repository) and a repeat that is already published also end with 0, and so does a `claim` on an item the writer already holds, once that claim is published. | `pushed`, `committed_local`, `remote_unreachable`, `no_default_branch`, `not_a_git_repository`, `already_published`, `already_claimed` |
| 1 | The command names an item, or for `annul` an event, that neither the local log nor the remote holds, and nothing is appended. Also the refusals named in the next column, and every unexpected error. Here `invalid_argument` is a refusal the command itself makes after parsing, such as a missing briefing file, a blank release reason, a release with no live claim, or text that holds a character UTF-8 cannot encode. | `unknown_target`, `final_annulment`, `not_a_tracker`, `root_mismatch`, `prefix_refused`, `invalid_dependency`, `invalid_argument`, `internal_error` |
| 2 | The writer is not `role@host` (`invalid_actor`, with the failure report). Also, as `invalid_argument`, a parser error and a blank value given to `decide`; an invalid decider given to `policy decision-actors` also exits 2. | `invalid_actor`, `invalid_argument` |
| 3 | `claim` only: another writer's claim holds the item, whatever its time, or your published claim does not hold it. With `claim_held` their claim was already in the local log or on the fetched remote, and no claim is appended. With `claim_superseded` this claim is published and does not hold the item: it lost a race, another claim of yours already holds the item, or it has already ended. | `claim_held`, `claim_superseded` |
| 4 | The remote's published branch could not be read, or the root lies inside another repository's history. For `claim`, also every local-only case that ends the other write commands with 0. | `remote_unreadable`, `foreign_repository`; under `claim` also the local-only names of exit 0 |
| 5 | The push was rejected on all three attempts, or the pull between attempts stopped on a conflict outside the projection or on a failed fetch. | `push_rejected` |
| 6 | `claim` only: the claim's timestamp is behind the newest live remote event by more than the tolerance, or the timestamp cannot be read in the form `YYYY-MM-DDTHH:MM:SSZ`. No claim is appended; the annulment of a waiting claim can be left uncommitted. | `clock_behind_remote`, `unreadable_timestamp` |
| 7 | The shard and projection could not be staged, or the commit was refused, for example by a hook. The appended event stays in the working tree, uncommitted. | `stage_failed`, `commit_refused` |
| 8 | The project policy refuses the event: under the `strict` process, a block or a decision that breaks its rules; under either process, a note reference that `note add` does not accept. Nothing is appended. | `policy_refused` |
| 9 | The local log or the remote's published log holds a line that cannot be used as an event. Nothing is appended, unless the damaged line reached the remote while the command was already publishing. Read commands that meet such a line also exit 9. | `log_damaged` |
| 10 | `PINAX_TEST_RUN` is `1` and the log is not under the temporary directory. Nothing is appended. Every command that appends an event exits this way. `pinax init` has by then created `.ergon/log/` and written `.ergon/.gitattributes` and `.ergon/.gitignore`. | `test_run_refused` |
| 130 | The command was interrupted. | `interrupted` |

The refusal names that can appear before the sequence starts are `invalid_argument`, `not_a_tracker`, `root_mismatch`, `prefix_refused`, `invalid_dependency`, `unknown_target`, `log_damaged`, `test_run_refused`, `internal_error` and `interrupted`. An unexpected exception is `internal_error`, with its traceback on standard error.

A line of the log cannot be used as an event when it is JSON and not an object, or when it is an event whose envelope is damaged; [data-model-and-integrity.md](data-model-and-integrity.md) gives the rule. A command that reads the log and meets such a line prints `pinax: the event log is damaged: <file> line <n>: <reason>. Run 'pinax verify' to list every damaged line.` on standard error and exits 9, with no traceback. `<file>` is the shard's path in the repository, or the ref and the path for a shard read from Git. With `--json`, read and write commands print one object with `published` false, `sync_status` `log_damaged`, `exit_code`, `error`, `file` and `line`. `verify` lists every such line and exits 1. `overview` and `status --portfolio` report that repository as damaged and go on with the others. `overview --remote` is different for the repository you run it in: it reads that repository's own log for the list of URLs, so a damaged line there ends it with exit 9.

An unknown item prints `pinax: unknown item '<id>' - no item with that id exists, so nothing was appended.` on standard error. Exit 7 prints one of two messages, followed by Git's or the hook's own output. The first is `pinax: the commit was refused, so event <event id> in <shard> is appended and not committed.` The second is `pinax: the shard and the projection could not be staged, so event <event id> in <shard> is appended and not committed.` [sharing-and-sync.md](sharing-and-sync.md) gives the recovery after exits 5 and 7 and after a claim's exit 4, and explains exit 6, whose `remedy` field names the fix.

### Running a command again after a failure

| Exit | What was written | Running the same command again |
|---|---|---|
| 1, 2, 6, 8, 9, 10 | nothing, except an annulment that a `claim` with exit 6 can leave uncommitted, and for 9 when the damaged line reached the remote while the command was publishing: then the event is committed, and published if the push was accepted | Safe once you have fixed the cause. After exit 9 with `"committed": true` in the report, do not run the command again: it would append a second event. Restore the line and, when `published` is false, push the commit you already have. The exception is `claim`: restore the line and run `pinax claim` again, which replaces the waiting claim with a new one at the current time. |
| 3 | `claim_held`: nothing, or only an uncommitted annulment of your waiting claim; `claim_superseded`: the claim, committed and pushed | Running `claim` again does not change who owns the item and writes nothing while the other claim is live. |
| 4 | nothing, except a `claim` on a branch other than the remote default, which is committed locally | Safe for every case that appended nothing, once the remote can be read or reached. |
| 5 | every command but `claim`: the event, committed locally; `claim`: the event and an uncommitted annulment that cancels it; a waiting claim it replaced is annulled in the commit | For `claim`, run it again. For the others, follow the recovery in [sharing-and-sync.md](sharing-and-sync.md), which publishes the event already committed. |
| 7 | the event, appended and uncommitted | Running the command again appends a second event; commit the waiting shard and projection by hand instead. `decide` and `claim` are the exceptions: a repeated `decide` commits the waiting events and appends nothing, and a repeated `claim` annuls its waiting claim and appends a new one. |

### Environment variables and time limits

| Variable | What it does |
|---|---|
| `PINAX_ROOT` | pins the expected repository root, like `--root` |
| `PINAX_OFFLINE` | set to exactly `1`, turns on offline mode for every write command except `claim` |
| `PINAX_CLOCK_TOLERANCE_S` | the seconds a `claim` may be behind the newest remote event; 5 when unset or not a non-negative number |
| `PINAX_TEST_RUN` | set to exactly `1`, refuses every append to a log outside the temporary directory, with exit 10; for test suites |
| `PINAX_ROOTS` | comma-separated directories that `overview` and `status --portfolio` scan |
| `PINAX_ROOTS_MAX_DEPTH` | how deep `overview` scans under each root |
| `GITHUB_TOKEN` | used by `overview --remote` when it falls back to the GitHub API |

Pinax sets no time limit on the fetch and push of the publish sequence, so they wait as long as Git does. `overview --remote` stops each Git call after 300 seconds and each request to the GitHub API after 30 seconds, and the local Git calls that `status` and `overview` make to read a branch stop after 5 seconds.

### Which commands write events

A write command is a command that runs the publish sequence. The write commands are `add`, `claim`, `done`, `block`, `park`, `decide`, `release`, `policy claim-expiry`, `policy process`, `policy owner-gates`, `policy decision-actors`, `policy note-refs`, `priority`, `annul`, `dep add`, `dep rm` and `note add`. Each one fetches `origin`, reads the local log together with the remote default branch, appends the event, regenerates the projection, commits, and pushes when the default branch is checked out. The commit holds only the writer's shard, `.ergon/board.md` and `.ergon/items`. Its subject is `pinax: <event type> <item id>`, for example `pinax: item.created app-4x56`, and an event that names no item has no id in its subject. Other staged work stays staged and out of the commit.

### Commands that never push

Nine commands change files in your repository and never push. None of them fetches, and none commits what it writes, with one exception in `doctor --reconcile`. Their changes reach other clones only when you commit and push them yourself.

| Command | What it writes | What it commits |
|---|---|---|
| `init` | the events `ergon.created` and `phase.opened`, `.ergon/.gitattributes`, `.ergon/.gitignore`, the projection and the pre-commit hook | nothing |
| `status id state` | the event `item.status_changed` and the projection | nothing |
| `dispatch --claim` | one `item.claimed` per listed item, and the projection | nothing |
| `registry add` | the event `registry.repo_added` | nothing |
| `registry rm` | the event `registry.repo_removed` | nothing |
| `reconcile` | the events `item.completed` and `item.parked`, the projection, and its action file | nothing |
| `doctor --reconcile` | the events `item.completed`, `item.parked` and `item.claim_released` for the stale claims you answer, and the projection | after you answer `y`, one commit of the `.ergon/` files that were uncommitted when it started; the events it then appends stay uncommitted |
| `verify --fix` | the projection | nothing |
| `overview --markdown` | `PORTFOLIO.md` at the repository root | nothing |

These commands write nothing: `status` (view form), `ready`, `next`, `board`, `report`, `metrics`, `replay`, `verify` without `--fix`, `policy check`, `registry list`, `overview` without `--markdown`, `web`, `doctor` without `--reconcile`, `reconcile --dry-run` and `dispatch` without `--claim`.

## Setting up

### `pinax init`

Creates the tracker in `.ergon/` and installs the pre-commit hook.

```
usage: pinax init [-h] [--actor ACTOR]
```

| Option | Takes | Default |
|---|---|---|
| `--actor ACTOR` | writer, `role@host` | `operator@<host name>` |

`init` creates `.ergon/log/` and writes `.ergon/.gitattributes` with exactly the lines `*.jsonl text eol=lf merge=union` and `.ergon/** text eol=lf`. It writes `.ergon/.gitignore` containing `!/log/*.jsonl`, appends `ergon.created` (seq 0, payload `{"repo": <directory name>}`) and `phase.opened` (seq 1, payload `{"phase": "init"}`), and regenerates the projection. After a fresh `init`, `git status` shows four untracked files: `.ergon/.gitattributes`, `.ergon/.gitignore`, `.ergon/board.md` and the writer's shard in `.ergon/log/`.

The hook is written as `pre-commit` into the directory that `git rev-parse --git-path hooks` names, with the absolute path of the interpreter that ran `init` built into it, and it is made executable on POSIX. There is no option to request or skip the hook. When that directory does not exist, for example outside Git, `init` skips the hook silently. `init` never installs a `post-commit` hook. The hook runs `pinax verify` before each commit, so a commit is refused (Git exits 1) while the board or an item page differs from the log.

**Prints:**

```
pinax: initialised .ergon/ in <repository root>
pinax: emitted ergon.created + phase.opened (actor=<writer>)
```

It has no `--json`.

**Exit codes:** 0, also when the tracker already exists.

**Refuses:** an existing hook that does not contain the text `Pinax drift lint` is left alone, and `init` prints on standard error `pinax init: .git/hooks/pre-commit already exists and is not a Pinax hook. Skipping hook installation. Run 'pinax verify' manually before commits.`

**Running it again:** when `.ergon/` already exists, `init` always rewrites `.ergon/.gitattributes` and `.ergon/.gitignore`. When the log already holds `ergon.created`, it also reinstalls the hook, prints `pinax: .ergon already initialised (idempotent - skipped event emit).`, appends nothing and leaves the projection as it is. When `.ergon/` exists and its log holds no `ergon.created`, it appends the two events with seq 0 and 1, whatever events the log already holds, and regenerates the projection.

`init` commits nothing and does not check the writer's shape. The next write command commits only its own shard and the projection, so commit `.ergon/` yourself after `init`, as the [README](../README.md) shows. Until you do, other clones receive neither `.ergon/.gitattributes` nor its rule that merges the log line by line.

## Adding and moving items

### `pinax add`

Creates a new item in the queued state.

```
usage: pinax add [-h] --title TITLE [--prefix PREFIX] [--actor ACTOR]
                 [--offline] [--json] [--allow-new-prefix]
```

| Option | Takes | Default |
|---|---|---|
| `--title TITLE` | text, required | none |
| `--prefix PREFIX` | text | `pnx` |
| `--actor ACTOR` | writer | `operator@<host name>` |
| `--offline` | flag | off |
| `--json` | flag | off |
| `--allow-new-prefix` | flag | off |

The event is `item.created` with payload `{item_id, title, prefix, status: "queued"}`. The item id is `<prefix>-<suffix>`, where the suffix is at least four characters of a hash, lengthened until it is unique in the local log.

```
pinax add --title "Write the install guide" --prefix app --actor alex@laptop
```

**Prints:**

```
pinax: created item <id> - "<title>" in <root>/.ergon
       event_id=<first 12 characters>... seq=<n> actor=<writer>
```

**JSON:** `actor`, `event_id`, `item_id`, `prefix`, `published`, `root`, `seq`, `status`, `sync_status`, `title`, `ts`. For example:

```
{"actor": "alex@laptop", "event_id": "rxailn35c3aqlod7hjlstkz6lispuz3clbn2ch6j5m3qqkpf5uma", "item_id": "app-4x56", "prefix": "app", "published": true, "root": "/home/alex/src/app/.ergon", "seq": 2, "status": "queued", "sync_status": "pushed", "title": "Write the install guide", "ts": "2026-11-23T14:45:30Z"}
```

**Exit codes:** the [common exit codes](#common-exit-codes).

**Refuses:** a prefix must be made of ASCII letters, digits, `-` and `_`, and start with a letter or a digit. Any other prefix is refused with exit 1 and `invalid_argument`, and nothing is appended. On a tracker that already has items, a prefix that never starts an existing id is refused with exit 1 and `prefix_refused`. The comparison is case-sensitive, the default `pnx` is checked like any other prefix, and an empty tracker accepts any prefix of that form. Pass `--allow-new-prefix` when the new prefix is intended. The message, on standard error, is:

```
pinax add: REJECTED - prefix 'web' has never appeared among this tracker's 4 existing item(s) at <root> (known prefixes: app). This looks like a tracker mis-bind (wrong repo root resolved from CWD) rather than a legitimate new prefix -- see 'pinax doctor' and the --root/PINAX_ROOT pin. If this IS a genuine first use of a new prefix, pass --allow-new-prefix.
```

When the writer is invalid, the report's `item_id` is null, because no id is made before that check.

### `pinax claim`

Records that a writer has started work on an item.

```
usage: pinax claim [-h] [--actor ACTOR] [--json] id
```

| Option | Takes | Default |
|---|---|---|
| `id` | item id, required | none |
| `--actor ACTOR` | writer | `operator@<host name>` |
| `--json` | flag | off |

The event is `item.claimed` with payload `{item_id}`. Because `pinax claim` checks your clock against the newest event on the remote, it needs the remote: it has no `--offline` option and ignores `PINAX_OFFLINE`. `pinax dispatch --claim` is different: it writes claims in the local log without the remote, and they stay unpublished until you commit and push them, or a later write command commits your shard (see [`dispatch`](#pinax-dispatch)). Such a claim waits, and `pinax claim` by the same writer never publishes it: it annuls the waiting claim and then decides as for a new claim, with a new time.

**Prints:**

```
pinax: claimed item <id> by <writer> in <root>/.ergon
       event_id=<first 12 characters>... seq=<n>
```

When the writer's published claim already holds the item, it appends nothing and prints `pinax: item <id> is already claimed by <writer> since <ts>; nothing was appended`. When it annulled a waiting claim, it also prints `pinax: the waiting claim <event id> was annulled`, and the line above ends with `no claim was appended`.

**JSON:** `actor`, `event_id`, `item_id`, `published`, `root`, `seq`, `sync_status`, `ts`, `type`. With `already_claimed` there is also `appended`, which is false, and `event_id`, `seq` and `ts` are those of the claim that holds the item. When a waiting claim was annulled there is also `annulled`, as in [the failure report](#the-failure-report).

**Exit codes:** this is the complete list for `claim`.

- 0 when the claim is pushed and the remote, read again after the push, shows that this claim holds the item. When a claim of this writer was waiting, unpublished, it is annulled and replaced by a new claim at the current time. Also 0, with `already_claimed`, when this writer's published claim already holds the item: no second claim is appended. A claim from another machine with an earlier timestamp can still arrive later and take the item; [sharing-and-sync.md](sharing-and-sync.md) explains how that is settled.
- 1 for an unknown item (`unknown_target`) and for the refusals before the publish sequence starts.
- 2 for a writer that is not `role@host` (`invalid_actor`), or a parser error.
- 3 when another writer holds the item, or when your published claim does not hold it. Ownership does not change. With `claim_held`, their live claim was already in the local log or on the fetched remote: no claim is appended, and nothing is committed or pushed. The report's `holder_actor`, `holder_event_id` and `claimed_at` name that claim, and its `message` is `item <id> is already claimed by <holder> since <ts>, so nothing was appended`. When a claim of yours was waiting, its annulment stays in your shard, uncommitted. The report's `annulled` then names that claim, and the `message` ends with `so no claim was appended; the waiting claim <event id> was annulled`. With `claim_superseded`, this claim is published and does not hold the item. When another writer's claim holds it, the report's `winner_actor` and `winner_event_id` name the owner and their claim. When that claim is earlier than yours, the `message` is `the claim on <id> is superseded by an earlier claim from <owner>, so the item is not owned by this actor`. Two rarer cases also end this way: another claim of yours reached the remote first and holds the item, or your claim was released or had expired when the remote was read again. In the second case nobody holds the item, and `winner_actor` and `winner_event_id` are `null`.
- 4 when there is no reachable `origin`, no published default branch or no Git repository, and also when the remote branch cannot be read or the root lies inside another repository. With no reachable `origin`, no published default branch, or no Git repository, nothing is appended, and standard error says, for example, `pinax: origin could not be reached, so nothing was appended.`
- 4 also with a branch other than the remote default checked out. In that case the claim is appended and committed on your machine, and it stays in place. Standard error carries `pinax: the push was refused because <branch> is not the origin default branch <default branch>; the event is committed locally and not published.` The failure report on standard output has `message` `the sync publishes only <default branch> and <branch> is checked out`.
- 5 when the push was rejected. The claim then annuls its own event in its shard, with an uncommitted annulment, and prints `pinax: event <id> was annulled in <shard> so an unpublished record is never carried forward.`
- 6 from the clock guard. A waiting claim of yours on the item is annulled all the same, and the report's `annulled` names it. The tolerance is 5 seconds, or the number of seconds in `PINAX_CLOCK_TOLERANCE_S`.
- 7 when the commit is refused or cannot be staged; the claim stays in the shard, uncommitted.
- 9 when the local log or the remote's published log holds a line that cannot be used as an event (`log_damaged`).
- 10 during a test run, when the log is not under the temporary directory (see [The writer](#the-writer-and---actor)).
- 130 when interrupted.

### `pinax release`

Ends a live claim on an item.

```
usage: pinax release [-h] --reason REASON [--actor ACTOR] [--offline] [--json]
                     id
```

| Option | Takes | Default |
|---|---|---|
| `id` | item id, required | none |
| `--reason REASON` | text, required | none |
| `--actor ACTOR` | writer | `operator@<host name>` |
| `--offline` | flag | off |
| `--json` | flag | off |

The event is `item.claim_released` with payload `{item_id, reason}`. Any writer may release any claim.

**Prints:**

```
pinax: claim on item <id> by <owner> released (reason='<reason>') by <writer> in <root>/.ergon
       event_id=<first 12 characters>... seq=<n>
```

**JSON:** `actor`, `event_id`, `item_id`, `published`, `reason`, `released_owner`, `root`, `seq`, `sync_status`, `ts`, `type`.

**Exit codes:** the [common exit codes](#common-exit-codes).

**Refuses:** an empty or blank reason (exit 1, `invalid_argument`), and an unknown item (exit 1, `unknown_target`). It also refuses an item with no live claim in the local log (exit 1, `invalid_argument`, with `pinax: item <id> carries no live claim, so there is nothing to release.`). Because that last check reads the local log, a claim that exists only on the remote and has not been pulled is refused as having no live claim.

### `pinax done`

Marks an item done and stores its briefing. A briefing is the text that the person who finished the work writes about it.

```
usage: pinax done [-h] --briefing FILE [--actor ACTOR] [--offline] [--json] id
```

| Option | Takes | Default |
|---|---|---|
| `id` | item id, required | none |
| `--briefing FILE` | path to a UTF-8 text file, required | none |
| `--actor ACTOR` | writer | `operator@<host name>` |
| `--offline` | flag | off |
| `--json` | flag | off |

Pinax reads FILE as UTF-8 text, turning CRLF and CR line endings into LF, and stores the whole text in the `item.completed` event with payload `{item_id, briefing}`. The item page shows it under `## Briefing`. The file itself is not committed.

```
pinax done app-4x56 --briefing note.md
```

**Prints:**

```
pinax: item <id> marked done by <writer> in <root>/.ergon
       briefing=<n> chars event_id=<first 12 characters>... seq=<n>
```

**JSON:** `actor`, `briefing_chars`, `event_id`, `item_id`, `published`, `root`, `seq`, `sync_status`, `ts`, `type`.

**Exit codes:** the [common exit codes](#common-exit-codes).

**Refuses:** a missing file, with exit 1 and `invalid_argument`. A file that cannot be read, or is not valid UTF-8, ends with an unexpected error (exit 1, `internal_error`).

### `pinax block`

Moves an item to blocked and records why.

```
usage: pinax block [-h] --gate {scope,decision,destructive,proposal}
                   [--owner-gate CLASS] [--actor ACTOR] [--offline] [--json]
                   id
```

| Option | Takes | Default |
|---|---|---|
| `id` | item id, required | none |
| `--gate {scope,decision,destructive,proposal}` | one of the four gates, required | none |
| `--owner-gate CLASS` | owner-gate class, text | none |
| `--actor ACTOR` | writer | `operator@<host name>` |
| `--offline` | flag | off |
| `--json` | flag | off |

The event is `item.blocked` with payload `{item_id, gate}`, with `owner_gate` added only when you give a class. When the class matches one on the project's list, the listed spelling is stored; otherwise the value is stored with its spaces collapsed.

```
pinax block app-4x56 --gate decision --owner-gate publication
```

**Prints:**

```
pinax: item <id> blocked (gate=<gate>) by <writer> in <root>/.ergon
       event_id=<first 12 characters>... seq=<n>
```

When a class is recorded, the parenthesis reads `(gate=<gate>, owner gate: <class>)`.

**JSON:** `actor`, `event_id`, `gate`, `item_id`, `owner_gate` when one was recorded, `published`, `root`, `seq`, `sync_status`, `ts`, `type`.

**Exit codes:** the [common exit codes](#common-exit-codes).

**Refuses:** under the `strict` process, a block with gate `decision` or `destructive` must name a class on the list, and a class given on any gate must be on the list. The match ignores case and spacing. Otherwise the command exits 8 with `policy_refused`, prints the [failure report](#the-failure-report) on standard output, and prints one of these on standard error:

```
pinax: strict process: a block with gate <gate> needs an owner-gate class. Give one of these with --owner-gate: <class>; <class>; .... Nothing was appended.
pinax: strict process: '<class>' is not an owner-gate class. Give one of these ...
```

Under `flow` any class is accepted.

**Leaving blocked:** an item stays blocked until a command gives it another status, and a claim does not change that. Run `pinax status <id> queued` or `pinax status <id> ready` to return it to the queue; this changes the local log only, so commit and push it yourself. `done`, `park` and `decide --effect park` also move it out of blocked. `decide --effect release` records a decision and leaves the item blocked.

**Waiting for one person:** an owner gate names a class of approval and no person, and under the default `flow` process any writer may record the decision. To make a blocked item wait for named people, set `pinax policy process --mode strict` and list them with `pinax policy decision-actors --decider <role@host>`.

### `pinax park`

Moves an item to parked, with a reason.

```
usage: pinax park [-h] --reason REASON [--actor ACTOR] [--offline] [--json] id
```

| Option | Takes | Default |
|---|---|---|
| `id` | item id, required | none |
| `--reason REASON` | text, required | none |
| `--actor ACTOR` | writer | `operator@<host name>` |
| `--offline` | flag | off |
| `--json` | flag | off |

The event is `item.parked` with payload `{item_id, reason}`. An empty reason is accepted.

**Prints:**

```
pinax: item <id> parked (reason='<reason>') by <writer> in <root>/.ergon
       event_id=<first 12 characters>... seq=<n>
```

**JSON:** `item_id`, `reason`, `event_id`, `seq`, `actor`, `ts`, `type`, `root`, `published`, `sync_status`.

**Exit codes:** the [common exit codes](#common-exit-codes).

### `pinax priority`

Sets an item's rank, where a lower rank is more urgent.

```
usage: pinax priority [-h] [--actor ACTOR] [--offline] [--json] id rank
```

| Option | Takes | Default |
|---|---|---|
| `id` | item id, required | none |
| `rank` | an integer, `top` or `bump`, required | none |
| `--actor ACTOR` | writer | `operator@<host name>` |
| `--offline` | flag | off |
| `--json` | flag | off |

An integer rank may be negative. `top` is one below the lowest rank any item holds in the local log, or 0 when no item has a rank. `bump` is one below the item's own rank, or the same as `top` when it has none. The event is `item.priority_set` with payload `{item_id, priority}`. An explicit rank always comes before the length of an item's chain of blocked items when `next` chooses.

```
pinax priority app-4x56 top
```

**Prints:**

```
pinax: item <id> priority set to <rank> (from '<rank argument>') by <writer> in <root>/.ergon
       event_id=<first 12 characters>... seq=<n>
```

**JSON:** `actor`, `event_id`, `item_id`, `priority` (the stored rank), `published`, `rank_arg` (the argument as typed, as text), `root`, `seq`, `sync_status`, `ts`, `type`.

**Exit codes:** the [common exit codes](#common-exit-codes). Any other value in place of `rank`, and an unknown item, exit 1 with nothing appended.

### `pinax dep add` and `pinax dep rm`

Adds or removes a typed link between two items.

```
usage: pinax dep [-h] {add,rm} ...
```

```
usage: pinax dep add [-h] [--to TO_ID] [--type TYPE] [--blocks TO_ID]
                     [--actor ACTOR] [--offline] [--json]
                     from_id
```

| Option | Takes | Default |
|---|---|---|
| `from_id` | item id, required | none |
| `--to TO_ID` | item id, used with `--type` | none |
| `--type TYPE` | one of `blocks`, `discovered-from`, `parent-child`, `related`, `supersedes` | none |
| `--blocks TO_ID` | item id; the same as `--to TO_ID --type blocks` | none |
| `--actor ACTOR` | writer | `operator@<host name>` |
| `--offline` | flag | off |
| `--json` | flag | off |

```
usage: pinax dep rm [-h] [--to TO_ID] [--type TYPE] [--blocks TO_ID]
                    [--actor ACTOR] [--offline] [--json]
                    from_id
```

`pinax dep rm` takes the same arguments and options as `dep add`. The events are `dep.added` and `dep.removed` with payload `{from_id, to_id, type}`, and the commit subject names `from_id`. `pinax dep add app-4x56 --blocks app-m3xd` means `app-m3xd` cannot be ready until `app-4x56` is done. Only `blocks` links affect readiness and `next`. A `parent-child` link also feeds `--under` and the roll-up of a parent, and the other types are recorded and shown on item pages only. Removing a link of one type leaves links of other types between the same items in place.

**Prints**, with `added` or `removed` and the link type in place of `<type>`:

```
pinax: dep added: <from_id> --<type>--> <to_id> in <root>/.ergon
       event_id=<first 12 characters>... seq=<n> actor=<writer>
```

**JSON:** `actor`, `dep_type`, `event_id`, `from_id`, `operation` (`add` for `dep add`), `published`, `root`, `seq`, `sync_status`, `to_id`, `ts`, `type`.

**Exit codes:** the [common exit codes](#common-exit-codes).

**Refuses**, each with exit 1 and `invalid_dependency`: both `--to` and `--blocks`, neither of them, `--to` without `--type`, and `from_id` equal to the destination. An unknown type is a parser error, exit 2. An unknown item at either end is exit 1 with `unknown_target`.

### `pinax note add`

Attaches a reference to an item.

```
usage: pinax note [-h] {add} ...
```

```
usage: pinax note add [-h] --ref REF [--caption CAPTION] [--actor ACTOR]
                      [--offline] [--json]
                      item_id
```

| Option | Takes | Default |
|---|---|---|
| `item_id` | item id, required | none |
| `--ref REF` | a repository-relative path, a web address, or text that starts with a prefix the repository accepts, required | none |
| `--caption CAPTION` | text, at most 200 characters | none |
| `--actor ACTOR` | writer | `operator@<host name>` |
| `--offline` | flag | off |
| `--json` | flag | off |

The event is `note.added` with payload `{item_id, ref, caption}`, where `caption` is present only when given. A note changes no status, no item page, board, status view or report shows it, `board --json` holds it under `notes`, and the history page of `pinax web` lists its event.

```
pinax note add app-4x56 --ref briefs/install.md --caption "The brief"
```

**Prints:** `pinax: note.added on <id> -> <ref> ('<caption>') in <root>/.ergon`

**JSON:** only `event_id`, `item_id`, `published`, `ref`, `root`, `sync_status`.

**Exit codes:** the [common exit codes](#common-exit-codes).

**Accepted references:** under either process, REF is one of these:

- a repository-relative path, whether or not a file is there yet. It is relative to the repository root, uses `/` as separator, and names something below that root, so it is not empty or only `.`. It is not absolute on any system, so it has no leading `/` or `\`, no drive letter and no UNC form. It has no `..` segment, and each of its other segments can be a file name on every system Pinax supports: none holds a control character or one of `\ : * ? " < > |`, and none ends with a space or, apart from a `.` segment, with a dot;
- a URL with the scheme `http` or `https` and a host;
- text that starts with a prefix set with [`pinax policy note-refs`](#pinax-policy-note-refs).

The prefixes are read from the local log together with the remote, so offline only the local log counts. A note already in the log stays valid whatever its reference.

**Refuses:** any other REF, with exit 8 and `policy_refused`, and a caption over 200 characters, with exit 1. The first refusal prints, with the listed prefixes in place of `none set` when there are some:

```
pinax note add: the reference '<ref>' is not a repository-relative path, not an http or https address, and does not start with a prefix the repository accepts (none set; see 'pinax policy note-refs')
```

### `pinax annul`

Cancels the effect of one earlier event by appending an annulment. An annulment is an event that names the event it cancels.

To undo an annulment, run `pinax annul` on the annulment's own id. The event it cancelled then counts again, unless another annulment in effect names it too. Annulling the second annulment cancels the event again.

One kind of annulment cannot be undone: the one a rejected `claim` writes for its own event, or that Pinax writes for a waiting claim, because that claim was never published. `pinax annul` refuses that target with exit code 1 and the `sync_status` `final_annulment`, appends nothing, and prints `pinax: event <id> is the annulment a rejected claim wrote for itself and cannot be annulled, because its claim was never published, so nothing was appended.` on standard error. Pinax recognises it by its fixed reason, `the event could not be published, so it never took effect`, and by having the same writer as the claim it names.

```
usage: pinax annul [-h] --reason REASON [--actor ACTOR] [--offline] [--json]
                   event_id
```

| Option | Takes | Default |
|---|---|---|
| `event_id` | the target event's id, required | none |
| `--reason REASON` | text, required | none |
| `--actor ACTOR` | writer | `operator@<host name>` |
| `--offline` | flag | off |
| `--json` | flag | off |

The event is `event.annulled` with payload `{target_id, reason}`, and the commit subject is `pinax: event.annulled <target id>`. The cancelled event's line stays in the log. Annulling an item's only `item.created` removes the item and its page, and annulling that annulment brings both back. An empty reason is accepted by the command, but the annulment it writes is invalid: it cancels nothing and makes `verify` fail. Annulling the same event again appends a second annulment and exits 0, with the same effect as one.

**Prints:**

```
pinax: event <target id> annulled (reason='<reason>') by <writer> in <root>/.ergon
       event_id=<first 12 characters>... seq=<n>
```

When the target is an annulment, and the event that annulment cancelled counts again as a result, one line follows:

```
pinax: event <id of the restored event> is in effect again
```

**JSON:** `actor`, `event_id`, `published`, `reason`, `root`, `seq`, `sync_status`, `target_id`, `ts`, `type`. For example:

```
{"actor": "alex@laptop", "event_id": "nw65r36jz7q4jpj5xy4s5waoce6oejkk7gdefyyyi7n4iadmp66a", "published": true, "reason": "created by mistake", "root": "/home/alex/src/app/.ergon", "seq": 3, "sync_status": "pushed", "target_id": "kuhxndvbl5mnvytzid7psxdosn63xvxkh4drlycd3mpl2bneocra", "ts": "2026-11-23T15:36:50Z", "type": "event.annulled"}
```

**Exit codes:** the [common exit codes](#common-exit-codes).

**Refuses:** an id that names no event in the local log or on the remote, with exit 1. An item id is not an event id and is refused the same way. An event that exists only on the remote is accepted.

### `pinax status id state`

Sets an item's status directly, in the local log only.

```
usage: pinax status [-h] [--actor ACTOR] [--json] [--repo PATH] [--portfolio]
                    [--since Nd] [--all] [--all-branches] [--under ITEM_ID]
                    [id] [state]
```

| Option | Takes | Default |
|---|---|---|
| `id` | item id | none |
| `state` | one of `queued`, `ready`, `building`, `blind-verify`, `adjudicate`, `done`, `blocked`, `parked` | none |
| `--actor ACTOR` | writer | `operator@<host name>` |
| `--json` | flag | off |

This is the setter form of `status`; the other options belong to the view form. The event is `item.status_changed`. The command regenerates the projection and does not fetch, commit, push or check the writer's shape, so afterwards `git status` shows the shard, the board and the item page as modified (see [Commands that never push](#commands-that-never-push)). No transition is guarded, so this form can move an item from any status to any other, including back from `done`.

```
pinax status app-4x56 ready --actor alex@laptop
```

**Prints:**

```
pinax: item <id> status -> <state> (by <writer>)
       event_id=<first 12 characters>... seq=<n>
```

**JSON:** `item_id`, `status`, `event_id`, `seq`, `actor`, `ts`, `type`.

**Exit codes:** 0 on success. An unknown status or an unknown item is exit 1.

**Refuses:** `--under` together with the setter form.

### `pinax dispatch`

Lists the ready items in the order `next` would pick them, and can claim them all locally.

```
usage: pinax dispatch [-h] [--max N] [--claim] [--actor ACTOR] [--json]
```

| Option | Takes | Default |
|---|---|---|
| `--max N` | integer; caps the list only when greater than 0 | no cap |
| `--claim` | flag | off |
| `--actor ACTOR` | writer | `operator@<host name>` |
| `--json` | flag | off |

With `--claim`, the command appends one `item.claimed` per listed item, with consecutive seq numbers and one timestamp, and with `actor` added to each payload. It does this locally, with no fetch, commit, push, `role@host` check or clock guard, so these claims are not published until you commit and push the shard, or a later write command by the same writer does (see [Commands that never push](#commands-that-never-push)). Before such a command pushes, it annuls each waiting claim of its writer on an item that another writer's published claim holds. It prints `pinax: the waiting claim <event id> on <item id> was annulled, because <holder> already holds the item on the remote` on standard error.

**Prints** a header and one numbered line per item:

```
pinax dispatch: <n> item(s)
  1. <id>  [<status>]  <title>
```

With `--max N` the header ends with ` (capped at <N>)`, and with `--claim` it ends with ` [claimed]`. With nothing ready it prints `pinax dispatch: ready queue is empty - nothing to dispatch.`

**JSON:** an array of `{created_at, id, owner, status, title}`, where `owner` is `""` for an unclaimed item.

**Exit codes:** 0 on success, and 1 outside a tracker.

### `pinax reconcile`

Imports done and park lines written by hand into an action file while the tracker was out of reach.

```
usage: pinax reconcile [-h] [--file PATH] [--actor ACTOR] [--dry-run] [--json]
```

| Option | Takes | Default |
|---|---|---|
| `--file PATH` | path to the action file | `BACKLOG-OFFLINE.md` at the repository root |
| `--actor ACTOR` | writer recorded as the importer; never replaces the writer on a line | `operator@<host name>` |
| `--dry-run` | flag; reports what would be imported, rejected or skipped, and touches nothing | off |
| `--json` | flag | off |

The action file accepts two line forms, and rejects every other line with a reason:

```
- <ts> <actor> done <item-id> [| caption]
- <ts> <actor> park <item-id> | reason
```

Each imported event keeps the line's writer and timestamp and records `imported_by`, `source: "BACKLOG-OFFLINE.md"` (also when `--file` names another file), `source_line_hash` and `imported_at`. The timestamp is not checked. A line already imported, with the same hash, is not imported again. Unknown items are rejected. A done line for a done item, and a park line for a done item, are skipped. Processed lines are moved, with leading and trailing spaces removed, into dated `## Reconciled <date> (by <actor>)` and `## Rejected <date>` sections of the file, each followed by a line `  => <detail>`. Nothing is deleted. `reconcile` does not commit or push.

**Prints**, when there is no action file, with or without `--dry-run`: `pinax reconcile: no offline file at <repository root>/BACKLOG-OFFLINE.md - nothing to reconcile.`

**JSON:** `{file, dry_run, imported, already_imported, rejected, skipped}`.

**Exit codes:** inside a tracker, a missing action file is exit 0 with an empty result. Outside a tracker the command exits 1 with the not-found message before it looks for the file.

## Deciding and setting policy

### `pinax decide`

Records the answer to a blocked item, and either leaves its status alone or parks it.

```
usage: pinax decide [-h] --option OPTION --label LABEL --effect {release,park}
                    --reason REASON [--actor ACTOR] [--offline] [--json]
                    id
```

| Option | Takes | Default |
|---|---|---|
| `id` | item id, required | none |
| `--option OPTION` | a short key you choose for the chosen option, such as `A`; Pinax keeps no list of options and only refuses a blank key; required | none |
| `--label LABEL` | the chosen option's label, required, never blank | none |
| `--effect {release,park}` | `release` records the decision and changes nothing else; `park` also parks the item; required. The effect `release` has no link to `pinax release`, which ends a claim. | none |
| `--reason REASON` | why this was decided, required, never blank | none |
| `--actor ACTOR` | writer | `operator@<host name>` |
| `--offline` | flag | off |
| `--json` | flag | off |

Option, label and reason are stored exactly as typed, without trimming. The word after `--option`, `--label`, `--effect`, `--reason` or `--actor` is always taken as that value, even when it starts with `-`, so `--reason --offline` stores the text `--offline`.

```
pinax decide app-4x56 --option B --label "Drop the page" --effect park --reason "Not this quarter"
```

The event is `item.decided` with payload `{item_id, option, label, effect, reason}`. With `--effect release`, that one event is all: the item's status, gate, owner-gate class, owner and claim time stay as they were. With `--effect park`, a second event `item.parked` with the same reason follows in the same shard and the same commit, with seq one higher and the same timestamp, and the item is then parked exactly as `pinax park` would leave it. Neither effect ends a claim by itself. A claim does expire, though, at the first event of any kind recorded after its expiry time, and a decision counts as such an event. `decide` does not check that the item is blocked. To move a blocked item back to work after a decision, see [Leaving blocked](#pinax-block). Both effects commit with the subject `pinax: item.decided <id>`. The latest decision appears on the item page under `## Decision` and as `decision` on the item's row in `status --json`; it does not appear on the board.

A repeat is a decision whose option, label, effect, reason and writer (host compared lower-cased) equal the latest decision on the item, and, for park, where the item is still parked. A repeat appends nothing. When the remote already records it, the command exits 0 with `already_published`, `published: true` and no commit or push. When an earlier run's commit was refused, it regenerates the projection and commits the waiting events. When an earlier run's push was rejected, it pushes. Offline, or on a branch other than the remote default, it reports `committed_local`. In every repeat `appended` is false. A different reason, a different writer, or a park on an item that has since left `parked` records a new decision.

**Prints:**

```
pinax: decision recorded on <id>; status unchanged: <status> (gate <gate>)
       decided_event_id=<first 12 characters>... seq=<n>
```

for release, where ` (gate <gate>)` appears only when the item is blocked;

```
pinax: decision recorded on <id>; item parked (reason='<reason>') by <writer>
       decided_event_id=<first 12 characters>... parked_event_id=<first 12 characters>...
```

for park; and `pinax: decision already recorded on <id>; nothing appended; status: <status>` for a repeat, with ` (gate <gate>)` when the item is blocked.

**JSON:** one line with sorted keys: `actor`, `appended`, `effect`, `gate` (always present, and `""` unless the item is blocked), `item_id`, `label`, `option`, `published`, `reason`, `root` (the path of the `.ergon` directory), `status`, `sync_status`, `type` (`item.decided`). When the decision is appended, `decided_event_id`, `seq` and `ts` are added, and an appended park also adds `parked_event_id`. For example:

```
{"actor": "alex@laptop", "appended": true, "decided_event_id": "nkukpqtcjlpkb3pg7namsqmwb34uvnaovybi6kcofyboetgvig4q", "effect": "release", "gate": "decision", "item_id": "app-4x56", "label": "Keep the old price", "option": "A", "published": true, "reason": "Agreed on the call", "root": "/home/alex/src/app/.ergon", "seq": 9, "status": "blocked", "sync_status": "pushed", "ts": "2026-11-23T14:45:33Z", "type": "item.decided"}
```

**Exit codes and refusals,** in the order the checks run. The first failure ends the command.

| Check | Exit | Name | Message on standard error |
|---|---|---|---|
| a parser error, such as a missing option or an effect other than `release` or `park` | 2 | `invalid_argument` | the usage and the parser's error |
| no tracker | 1 | `not_a_tracker` | `pinax: .ergon/log/ not found - run 'pinax init' first.` |
| a blank option, label or reason (the first one in that order) | 2 | `invalid_argument` | `pinax: the <option\|label\|reason> is blank, so nothing was appended.` |
| writer not `role@host` | 2 | `invalid_actor` | |
| root inside another repository | 4 | `foreign_repository` | |
| remote branch unreadable | 4 | `remote_unreadable` | |
| unknown item | 1 | `unknown_target` | `pinax: unknown item '<id>' - no item with that id exists, so nothing was appended.` |
| refused under `strict` | 8 | `policy_refused` | see below |
| the shard and projection could not be staged, or the commit was refused | 7 | `stage_failed`, `commit_refused` | |
| three rejected pushes | 5 | `push_rejected` | |

Exits 3 and 6 never occur for `decide`. A blank option under `--json` prints:

```
{"command": "decide", "error": "pinax: the option is blank, so nothing was appended.", "exit_code": 2, "published": false, "sync_status": "invalid_argument"}
```

**Who may decide:** under `flow`, any writer, even when a decision-actor list is set. Under `strict`, only a writer on the latest list set with `policy decision-actors`, with hosts compared lower-cased, and nobody when no list is set. The process and the list are read from the local log together with the remote, so offline only the local log counts. The strict refusals print:

```
pinax: strict process: no decision-actor list is set, so no actor may decide. Set one with pinax policy decision-actors --decider <role@host>. Nothing was appended.
pinax: strict process: <writer> is not on the decision-actor list (<writer>; <writer>). Set the list with pinax policy decision-actors --decider <role@host>. Nothing was appended.
```

### `pinax policy process`

Sets the project's process to `flow` or `strict`.

```
usage: pinax policy [-h]
                    {claim-expiry,process,owner-gates,decision-actors,note-refs,check} ...
```

```
usage: pinax policy process [-h] --mode {flow,strict} [--actor ACTOR]
                            [--offline] [--json]
```

| Option | Takes | Default |
|---|---|---|
| `--mode {flow,strict}` | the process, required | none |
| `--actor ACTOR` | writer | `operator@<host name>` |
| `--offline` | flag | off |
| `--json` | flag | off |

The event is `policy.process_set` with payload `{process}`. It names no item, so the commit subject has no id. The latest such event decides the process, and with none the process is `flow`. A strict process pushed from another clone binds your next write once Pinax reads the remote.

**Prints:**

```
pinax: process set to <mode> by <writer> in <root>/.ergon
       event_id=<first 12 characters>... seq=<n>
```

Setting `strict` adds this line, which goes to standard error under `--json`:

```
pinax: every clone, and every older pinax on PATH, must run 0.5.0 or later before this is used; an older client will report drift, and after upgrading, pinax verify --fix repairs it.
```

**JSON:** `process`, `event_id`, `seq`, `actor`, `ts`, `type`, `root`, `published`, `sync_status`.

**Exit codes:** the [common exit codes](#common-exit-codes). A mode other than `flow` or `strict` is a parser error, exit 2, and nothing is appended.

### `pinax policy owner-gates`

Replaces the list of owner-gate classes that `strict` checks against.

```
usage: pinax policy owner-gates [-h] --class TEXT [--actor ACTOR] [--offline]
                                [--json]
```

| Option | Takes | Default |
|---|---|---|
| `--class TEXT` | one class; repeat the option for each, at least one | none |
| `--actor ACTOR` | writer | `operator@<host name>` |
| `--offline` | flag | off |
| `--json` | flag | off |

The event is `policy.owner_gate_classes_set` with payload `{classes}`. Until one is set, the list is: production deployment; publication; real personal data; irreversible or destructive act; material change of scope, cost or architecture; login-bound act or owner-held input; start signal for a material plan.

```
pinax policy owner-gates --class "production deployment" --class publication
```

**Prints:**

```
pinax: owner-gate classes set (<class>; <class>) by <writer> in <root>/.ergon
       event_id=<first 12 characters>... seq=<n>
```

**JSON:** `actor`, `classes`, `event_id`, `published`, `root`, `seq`, `sync_status`, `ts`, `type`.

**Exit codes:** the [common exit codes](#common-exit-codes).

**Refuses**, with exit 1: an empty list, an empty class, and a class repeated when case and spacing are ignored.

### `pinax policy decision-actors`

Replaces the list of writers allowed to decide under `strict`.

```
usage: pinax policy decision-actors [-h] --decider ROLE@HOST [--actor ACTOR]
                                    [--offline] [--json]
```

| Option | Takes | Default |
|---|---|---|
| `--decider ROLE@HOST` | one writer who may decide; repeat the option for each | none |
| `--actor ACTOR` | writer | `operator@<host name>` |
| `--offline` | flag | off |
| `--json` | flag | off |

The event is `policy.decision_actors_set` with payload `{actors}`. Each decider is stored with its host lower-cased, and a duplicate is kept once.

**Prints:**

```
pinax: decision actors set (<writer>) by <writer> in <root>/.ergon
       event_id=<first 12 characters>... seq=<n>
```

**JSON:** `actors`, `event_id`, `seq`, `actor`, `ts`, `type`, `root`, `published`, `sync_status`.

**Exit codes:** the [common exit codes](#common-exit-codes).

**Refuses:** a decider that is not a valid writer, with exit 2.

### `pinax policy note-refs`

Replaces the list of prefixes that a note reference may start with, besides being a repository-relative path or a web address.

```
usage: pinax policy note-refs [-h] [--prefix TEXT] [--actor ACTOR] [--offline]
                              [--json]
```

| Option | Takes | Default |
|---|---|---|
| `--prefix TEXT` | one prefix, without whitespace; repeat the option for each, at least one | none |
| `--actor ACTOR` | writer | `operator@<host name>` |
| `--offline` | flag | off |
| `--json` | flag | off |

The event is `policy.note_ref_prefixes_set` with payload `{prefixes}`. The latest such event holds the whole list, and with none the list is empty. A list pushed from another clone binds your next `note add` once Pinax reads the remote.

```
pinax policy note-refs --prefix wiki:// --prefix vault/
```

**Prints:**

```
pinax: note reference prefixes set (<prefix>; <prefix>) by <writer> in <root>/.ergon
       event_id=<first 12 characters>... seq=<n>
```

**JSON:** `actor`, `event_id`, `prefixes`, `published`, `root`, `seq`, `sync_status`, `ts`, `type`.

**Exit codes:** the [common exit codes](#common-exit-codes).

**Refuses**, with exit 1 and `invalid_argument`: an empty list, an empty prefix, a prefix that holds whitespace, and the same prefix given twice.

### `pinax policy claim-expiry`

Sets how many hours a claim holds an item without a release.

```
usage: pinax policy claim-expiry [-h] --hours HOURS [--actor ACTOR]
                                 [--offline] [--json]
```

| Option | Takes | Default |
|---|---|---|
| `--hours HOURS` | number, finite and greater than 0, required | none |
| `--actor ACTOR` | writer | `operator@<host name>` |
| `--offline` | flag | off |
| `--json` | flag | off |

The event is `policy.claim_expiry_set` with payload `{hours}`. A claim runs on the latest expiry set at or before it, or on 24 hours when none is set, so a new value governs only claims made after it.

**Prints:**

```
pinax: claim expiry set to <hours>h by <writer> in <root>/.ergon
       event_id=<first 12 characters>... seq=<n>
```

The hours are printed as a decimal number, for example `8.0h`.

**JSON:** `hours`, `event_id`, `seq`, `actor`, `ts`, `type`, `root`, `published`, `sync_status`.

**Exit codes:** the [common exit codes](#common-exit-codes).

**Refuses:** a value that is not finite and positive, with exit 1.

## Reading state

None of these commands append events. `board`, `report`, `ready` and `status` read only the working tree's log. When a local branch holds log commits that HEAD lacks, `board`, `report` and `ready` print a warning on standard error that names the branch and its added events:

```
pinax: WARNING - 1 unmerged branch carries tracker events not in this view:
         feature (+3 events)
       This fold is branch-scoped: it reflects only the current branch's committed .ergon log.
```

`status` puts the same fact into its own report on standard output, and into `warnings` under `--json`, as `warning: unmerged branch <branch> (+<n> events): current view is branch-scoped`.

Pass `--all-branches` to these commands to read those branches' committed shards too. An item found only on a branch is then marked `[from: <branch>]`.

### `pinax status`

Shows what is being built, what has shipped, what is parked, and what comes next.

```
usage: pinax status [-h] [--actor ACTOR] [--json] [--repo PATH] [--portfolio]
                    [--since Nd] [--all] [--all-branches] [--under ITEM_ID]
                    [id] [state]
```

| Option | Takes | Default |
|---|---|---|
| `--actor ACTOR` | writer; used by the setter form only | `operator@<host name>` |
| `--json` | flag | off |
| `--repo PATH` | path; used directly, ignoring the root pin (view form only) | the repository found from the current directory |
| `--portfolio` | flag; show the portfolio even inside a repository | off |
| `--since Nd` | whole number of days, with an optional `d`; shipped items completed in that window are shown | `7d` |
| `--all` | flag; show every shipped item | off |
| `--all-branches` | flag (repository view only) | off |
| `--under ITEM_ID` | item id (repository view only) | none |
| `id`, `state` | the setter form, see [`status id state`](#pinax-status-id-state) | none |

Without `--repo` or `--portfolio`, the command walks up from the current directory to the first directory that holds `.ergon/`. When it finds one, it shows that repository. Otherwise it shows the portfolio, built from `PINAX_ROOTS` or `~/src` and the registry of the first repository that scan finds. An item is listed under building when its status is `building`, `blind-verify` or `adjudicate`, or when it has a live claim and is not done, parked or blocked. A claimed row's `since` and `age_hours` count from the claim time. Queued and ready items with no live claim get no row.

**Prints**, for one repository:

```
pinax status

repo: app @ main
  building (0):
    (none)
  shipped (1):
    app-4x56  Write the install guide  done 2026-11-23T14:45:27Z
  parked / blocked (0):
    (none)
  next:
    app-m3xd  Review the pricing page
  ready queue: 1 item(s)
```

A building row reads `<id>  <title>  [<status>; <owner>; since <time>; claimed <hours>h ago]`, a blocked row ends `blocked: <gate>`, and with nothing ready the next line is `(none - ready queue empty)`. The portfolio view prints one such `repo:` block per repository, the current one first.

Outside any tracker, with no repository discovered, it prints:

```
pinax status

  No Pinax repos discovered from PINAX_ROOTS or the default roots.
  Run from a repo with .ergon/ or set PINAX_ROOTS to portfolio roots.
```

**JSON:** `{"repo": {...}, "schema": "pinax.status.v1", "scope": "repo", "under": <id or null>}`, or with `"scope": "portfolio"` a `"repos": [...]` array and an optional `"warnings"`. A repository entry has `branch`, `building`, `id`, `initialised`, `next` (`{id, title}`), `notices`, `parked`, `queue_depth`, `root`, `shipped_earlier_count`, `shipped_recent` and `warnings`, plus `all_branches` and `source_branches` with `--all-branches`. The rows are:

| List | Fields |
|---|---|
| `building` | `id`, `title`, `stage`, `owner`, `since`, `age_hours`, and `rollup` and `decision` when present |
| `shipped_recent` | `id`, `title`, `done_at`, `actor`, and `rollup` and `decision` when present |
| `parked` | `id`, `title`, `kind` (`parked` or `blocked`), `reason`, and `rollup` and `decision` when present |

**Exit codes:** 0, also outside a tracker. One positional argument alone, such as `pinax status app-4x56`, is exit 2. A negative `--since`, such as `--since -1d`, is a parser error, exit 2, with `pinax status: error: argument --since: expected one argument`.

**Refuses:** `--under` together with `--portfolio`.

### `pinax ready`

Lists the items that can be started now.

```
usage: pinax ready [-h] [--actor ACTOR] [--json] [--all-branches]
                   [--under ITEM_ID]
```

| Option | Takes | Default |
|---|---|---|
| `--actor ACTOR` | accepted and ignored | none |
| `--json` | flag | off |
| `--all-branches` | flag | off |
| `--under ITEM_ID` | item id | none |

An item is ready when its status is queued or ready, it has no live claim, every item that blocks it is done, and it is not on a cycle of `blocks` links. A blocker that is not in the log counts as not done. `--under ITEM_ID` keeps only the items below ITEM_ID through `parent-child` links, at any depth, without ITEM_ID itself, and blockers outside that set still count.

**Prints:** one line per item, `  <id>  <status>  <title>`, or `pinax: ready queue is empty.`

**JSON:** a bare array of ids, such as `["app-m3xd"]`. With `--under`, `{"ready": [...], "under": <id>}`. With `--all-branches`, `{"ready", "all_branches": true, "source_branches", "item_sources", "under"}`.

**Exit codes:** 0 on success. An unknown `--under` id is exit 1.

### `pinax next`

Names the one item to start next.

```
usage: pinax next [-h] [--actor ACTOR] [--json] [--under ITEM_ID]
```

| Option | Takes | Default |
|---|---|---|
| `--actor ACTOR` | accepted and ignored | none |
| `--json` | flag | off |
| `--under ITEM_ID` | item id | none |

`next` picks among the ready items by these keys, in this order. The keys are the phase, whether the item has a rank (ranked items first), the rank, the length of the item's chain of not-done blocked items (longest first), its creation time, its event id, and its id.

**Prints:** `pinax: next -> <id>  (<status>)  <title>`, or `pinax: ready queue is empty - no next item.` when nothing is ready.

**JSON:** `{"item_id", "prefix", "status", "title", "under"}`, for example `{"item_id": "app-m3xd", "prefix": "app", "status": "queued", "title": "Review the pricing page", "under": null}`, or `{"item_id": null, "under": ...}` when nothing is ready.

**Exit codes:** 0 on success. An unknown `--under` id is exit 1.

### `pinax board`

Prints the board.

```
usage: pinax board [-h] [--json] [--all-branches]
```

| Option | Takes | Default |
|---|---|---|
| `--json` | flag | off |
| `--all-branches` | flag | off |

**Prints:** the same Markdown as the committed `.ergon/board.md`. With `--all-branches`, rows found only on a branch carry the `[from: <branch>]` marker, which never appears in the committed file.

**JSON:** `{"repo": <directory name>, "state": <the whole fold state>}`, with briefings and notes included, plus `all_branches`, `source_branches` and `item_sources` with `--all-branches`.

**Exit codes:** 0 on success. A directory without a tracker is exit 1.

### `pinax report`

Summarises what shipped, what is parked, what failed, and what comes next.

```
usage: pinax report [-h] [--json] [--all-branches]
```

| Option | Takes | Default |
|---|---|---|
| `--json` | flag | off |
| `--all-branches` | flag | off |

Shipped items are those with status done, ordered by completion time and then id. Failed items are those with status blocked.

**Prints:**

```
pinax report

  shipped (0):
    (none)

  parked (1):
    app-m3xd                  Review the pricing page                   reason: Waiting for the designer

  failed/blocked (0):
    (none)

  next:
    app-4x56  Write the install guide
```

An empty section shows `(none)`, and with nothing ready the next section shows `(none - ready queue empty)`.

**JSON:** `{failed: [{id, title, gate}], next, notices, parked: [{id, title, reason}], shipped: [{actor, done_at, id, title}]}`, where `next` is `null` when nothing is ready, plus the branch keys with `--all-branches`.

**Exit codes:** 0 on success, and 1 outside a tracker.

### `pinax metrics`

Counts items and events.

```
usage: pinax metrics [-h] [--json]
```

| Option | Takes | Default |
|---|---|---|
| `--json` | flag | off |

**Prints:**

```
pinax metrics
  total_items      : 3
  events_total     : 15
  ready_queue_size : 1
  items_done       : 0
  items_parked     : 2
  items_blocked    : 0
  note_added_count : 1
  claim_superseded : 0
  by_status:
    parked               2
    queued               1
  gate_counts:
    decision             1
  park_reasons:
    app-m3xd             Waiting for the designer
    app-q9fe             Not this quarter
```

The `by_status`, `gate_counts` and `park_reasons` blocks are left out when they are empty.

**JSON:** `audit_verdicts`, `by_status`, `claim_superseded_count`, `cycle_times` (a list of `{created_at, done_at, elapsed_seconds, item_id}`), `events_total`, `gate_counts`, `items_blocked`, `items_done`, `items_parked`, `note_added_count`, `park_reasons`, `ready_queue_size`, `total_items`.

`events_total`, `gate_counts` and `note_added_count` count raw lines, so duplicate lines and annulled events are counted. `by_status` uses each item's stored status, so a claimed queued item counts as queued. Nothing in the package writes the events that `audit_verdicts` counts.

**Exit codes:** 0 on success, and 1 outside a tracker.

### `pinax replay`

Shows the tracker as it was at an earlier commit, branch or tag.

```
usage: pinax replay [-h] --at REF [--json]
```

| Option | Takes | Default |
|---|---|---|
| `--at REF` | Git branch, tag or commit, required | none |
| `--json` | flag | off |

The command reads the shards as they were committed at REF.

```
pinax replay --at HEAD~3
```

**Prints:**

```
pinax replay --at HEAD~3

  items (2):
    app-4x56                  done          Write the install guide
    app-m3xd                  queued        Review the pricing page

  next (as of this ref):
    app-m3xd  Review the pricing page
```

**JSON:** `{"at": REF, "state": <fold state>}`. A REF from before `init` gives an empty state.

**Exit codes:** 0 on success. A REF that does not resolve is exit 1, with `pinax replay: not a valid git ref in this repo: '<ref>'` on standard error.

## Checking

### `pinax verify`

Checks the log for edits and compares the board and item pages with a fresh rebuild.

```
usage: pinax verify [-h] [--fix]
```

| Option | Takes | Default |
|---|---|---|
| `--fix` | flag; rebuild the projection from the log, then check again | off |

`verify` first reports every line that cannot be used as an event, with its file, line number and reason, whether or not an annulment names its id. It checks every other event against its own id and every annulment for validity, then the link from each event to the one before it, then the projection. It also fails when `git check-ignore` says a new shard in `.ergon/log/` would be ignored. It compares the files in the working tree. [data-model-and-integrity.md](data-model-and-integrity.md) lists what these checks catch and what they cannot see.

`--fix` rebuilds the projection only when the log passes its checks, then checks again. Because it never changes an event, it cannot repair an edited event, and it leaves the projection untouched while the log fails. It cannot fix an ignored log either.

**Prints:** `pinax verify: OK - projection matches log.` on standard output when clean. On drift it prints on standard error:

```
pinax verify: DRIFT DETECTED - projection differs from log.
  Drifted files:
    .ergon/board.md

  Run 'pinax verify --fix' to regenerate, or run any state-changing
  command (which regenerates atomically).
```

The message ends with one more line, which says the projection must never be edited by hand.

A drifted path is followed by ` (missing)` or ` (stale)` where that applies. `--fix` with nothing to fix prints the plain OK line. With `--fix` and drift, standard error shows `pinax verify: DRIFT DETECTED - regenerating projection (--fix)...` with the drifted files, and standard output shows `pinax verify --fix: OK - projection regenerated and matches log.` When an event fails its check, standard error shows:

```
pinax verify: EVENT LOG INTEGRITY FAILURE - 1 non-annulled event(s) no longer match their stored content hash or name a predecessor missing from their chain.
  Invalid events:
    id=<event id> seq=<n> actor=<writer> type=<event type> at <file> line <n>

  The append-only event log was modified after these events were written.
  'pinax verify --fix' only regenerates projections and will not rewrite
  event history. Restore the exact original event bytes. A valid ordinary
  event may be formally annulled before replacement; a malformed annulment
  record itself must be restored from known-good history.
```

When a line cannot be used as an event, standard error shows:

```
pinax verify: EVENT LOG INTEGRITY FAILURE - 1 line(s) of the event log cannot be used as events.
  Damaged lines:
    <file> line <n>: <reason>

  Restore each line from known-good history. Annulling an event does
  not recover a damaged line, and every other command refuses until
  the lines are restored.
```

When a `.gitignore` rule hides the log, standard error shows:

```
pinax verify: LOG SWALLOWED BY GITIGNORE - .ergon/log/pinax-doctor-probe.jsonl would be git-ignored right now.
  A .gitignore rule is shadowing the event log shard directory: new shard files
  will be silently local-only, never committed, invisible across worktrees/
  branches because Git ignores it. Run 'pinax init' again to (re)install the
  .ergon/.gitignore negation, or fix your repo's .gitignore.
```

With no log, it prints `pinax verify: .ergon/log/ not found - nothing to verify.` on standard error.

**Exit codes:** 0 when clean, after a successful `--fix`, and when `.ergon/log/` does not exist. 1 on a failed integrity check, on drift, and when the log is ignored by Git; `--fix` also exits 1 when an event fails its check.

### `pinax policy check`

Lists blocked items that lack a valid owner-gate class.

```
usage: pinax policy check [-h] [--json]
```

The command reads the local log only. It lists blocked items whose gate is `decision` or `destructive` and whose recorded class is missing or not on the list. An item that is no longer blocked is not listed.

**JSON:** `{process, classes, unclassed: [{id, title, gate, owner_gate}], ok}`.

**Exit codes:** 0 under `flow`, whatever it lists. Under `strict`, 1 when it lists any item, and 0 otherwise.

### `pinax doctor`

Reports problems in the tracker and its install, and can guide you through fixing them.

Run it when Pinax points you to it. That happens when `status` or `report` prints `notices: <n> claim-reconciliation notice(s) - run 'pinax doctor' for detail`. It also happens when a write warns that a `.gitignore` rule is swallowing the event log, and when `add` refuses a new prefix as a possible mis-bound tracker. Run it too after any command listed under [Commands that never push](#commands-that-never-push), because section [1] lists the files and events still uncommitted.

```
usage: pinax doctor [-h] [--stale-hours N] [--legacy-board PATH] [--now TS]
                    [--reconcile] [--actor ACTOR] [--json]
```

| Option | Takes | Default |
|---|---|---|
| `--stale-hours N` | number of hours | the claim expiry in force, else 24 |
| `--legacy-board PATH` | a legacy board file, or a directory of `*.md` files with an `id` in their frontmatter | `<repo>/board`, when it exists |
| `--now TS` | UTC time `YYYY-MM-DDTHH:MM:SSZ` to measure claim ages from | the current UTC time |
| `--reconcile` | flag; the guided fix; cannot be combined with `--json` | off |
| `--actor ACTOR` | writer for the events `--reconcile` appends | `operator@<host name>` |
| `--json` | flag | off |

The report has six sections, numbered from 0:

| Section | What it reports |
|---|---|
| [0] | install health |
| [1] | uncommitted files under `.ergon/`, and the events in them that HEAD lacks |
| [2] | stale claims: live claims on items that are not done, parked or blocked, at least `--stale-hours` old, or with a claim time that cannot be read |
| [3] | items whose done state disagrees with the legacy board, matched by the frontmatter `id` ignoring case |
| [4] | whether the log is ignored by Git |
| [5] | events committed in HEAD that are not on the last-fetched remote default branch |

`doctor` never fetches. Each uncommitted file, each stale claim and each legacy contradiction counts as one finding, and an ignored log adds one more. Events missing from the remote are reported and do not count. In section [0], the hook resolution mode says how the pre-commit hook will find Pinax (for example `executable`, the `pinax` on your `PATH`), and the replace guard says that console output replaces characters it cannot show. The field `editable install target` reports `(not detected)` for an installed package, and `null` under `--json`, because it looks for a package named `pinax` while Pinax is installed as `pinax-tracker`.

With `--reconcile`, `doctor` asks `[y/N]` before one ordinary `git commit` of the uncommitted `.ergon/` paths, renames and deletions included. It refuses while a merge, cherry-pick, revert, rebase or unmerged entry is in progress. It then asks `[d]one / [p]ark / [r]elease / [s]kip` for each stale claim, and appends each answer as an event in the local log, using `--now` as the event time when given. A done answer takes a one-line briefing, park and release each ask for a reason, and release appends `item.claim_released`. When input ends, each question takes its safe default. These answers are appended and left uncommitted: the command ends with `doctor: appended <n> event(s) via the normal event path; projection regenerated. Commit .ergon/ with your normal git flow.`

**Prints**, on a clean tracker:

```
pinax doctor: <repository root>
  reference time: 2026-11-23T14:45:36Z  stale threshold: 24h
  [0] install health:
        executable on PATH: /usr/local/bin/pinax
        hook resolution mode: executable
        console encoding: stdout=utf-8 errors=replace; stderr=utf-8 errors=replace; replace guard=yes
        editable install target: (not detected)
  [1] uncommitted shard events: none
  [2] stale claims: none
  [3] legacy-board cross-check: skipped (no legacy board found; use --legacy-board PATH)
  [4] log tracking (gitignore swallow check): OK (2/2 on-disk shard file(s) git-tracked)
  [5] unsynced shards: none ahead of origin/main
pinax doctor: no findings - tracker trails are clean.
```

**JSON:**

| Key | Fields |
|---|---|
| `findings` | the number of findings |
| `install_health` | `console {replace_guard, stderr_encoding, stderr_errors, stdout_encoding, stdout_errors}`, `editable_install_target`, `executable`, `executable_on_path`, `hook_resolution_mode` |
| `legacy` | `checked`, `contradictions`, `path` |
| `log_tracking` | `available`, `ignored`, `probe_path`, `shards_on_disk`, `shards_tracked` |
| `now` | the reference time |
| `stale_claims` | a list |
| `stale_hours` | the threshold |
| `uncommitted` | `available`, `events` (each `{actor, id, item_id, seq, shard, ts, type}`), `files` (each `{path, state}`) |
| `unsynced` | `available`, `events`, `remote_branch` |

**Exit codes:** 0 with no findings and 1 with findings. Also 1 with no tracker and with a `--now` it cannot read. `--reconcile` ends with 0.

**Refuses:** `--reconcile` together with `--json`, with exit 1.

## Several repositories

[portfolio-and-web.md](portfolio-and-web.md) explains how to use these commands together. `pinax status --portfolio` is described under [`status`](#pinax-status).

### `pinax overview`

Summarises every tracker it can find, the current repository included.

```
usage: pinax overview [-h] [--roots ROOTS] [--max-depth MAX_DEPTH] [--json]
                      [--markdown] [--remote]
```

| Option | Takes | Default |
|---|---|---|
| `--roots ROOTS` | comma-separated directories to scan for `.ergon/` | `PINAX_ROOTS`, else `~/src` |
| `--max-depth MAX_DEPTH` | how deep to scan under each root | `PINAX_ROOTS_MAX_DEPTH`, else 3 |
| `--json` | flag; wins over `--markdown` | off |
| `--markdown` | flag; write `PORTFOLIO.md` at the repository root and print one line saying so | off |
| `--remote` | flag; read the pushed tip of each registered remote | off |

The view holds the current repository (the hub), each registry entry with a path, and each repository found by scanning the roots for `.ergon/` directories. The hub is shown even when the roots hold nothing, and a `~` in `--roots` is expanded to your home directory. The scan skips `.git`, `.ergon`, `node_modules`, `__pycache__`, `.venv`, `venv`, `.tox`, `.mypy_cache`, `.pytest_cache` and linked directories. It does not descend into a repository it has found, drops bare repositories, keeps one entry for a set of linked worktrees, and lists a directory reached by two paths once. A repository's id is its directory name, made unique with its parent's name and then a number. `overview` never writes into a repository it discovers.

`--markdown` writes `PORTFOLIO.md` at the hub root, with a note not to edit it, the same body, the time it was made, and each repository's HEAD commit (`(no git)` when it has none). It commits nothing.

`--remote` reads, for each registry entry with a URL, sorted by id, the tip of the remote's default branch. It uses `git ls-remote --symref`, then a shallow clone with no blobs and no checkout into a temporary directory that it deletes afterwards. For a GitHub URL, when Git fails, it falls back to the GitHub contents API, with no sign-in unless `GITHUB_TOKEN` is set. Only pushed work is visible, the hub is not in the view, and `--roots` and `--max-depth` are ignored. Each Git call times out after 300 seconds and each HTTP request after 30 seconds.

**Prints:** `# Pinax portfolio overview`, one `## <id>` section per repository, and `## Needs attention (cross-repo)`, which lists each blocked and each parked item across the repositories, and in the remote view each remote that could not be read:

```
# Pinax portfolio overview

## app

- status: ok · 2 items · blocked=1 queued=1
- next: app-4x56  Write the install guide
- parked (0): (none)
- blocked (1):
  - app-zchg · Choose the payment provider · gate: decision

## Needs attention (cross-repo)

- app/app-zchg · Choose the payment provider · blocked: gate=decision
```

With `--markdown` it prints only `pinax: wrote PORTFOLIO.md (<n> repo(s), generated <time>)`. With `--remote`, an empty remote shows `(nothing pushed)`, and an unreachable one is listed under needs attention.

**JSON:** `{"repos": [...]}`. Each entry has `blocked [{gate, id, title}]`, `by_status`, `id`, `initialised`, `next {id, title}`, `notices`, `parked [{id, title, reason}]`, `path`, `total_items`. A repository without `.ergon/log` has `initialised: false`. Under `--remote` each entry also has `url` and `sha`, and an unreachable remote has `error`.

**Exit codes:** `--remote` with no URL entries prints a warning, shows an empty view and exits 0. `--remote` exits 9 on a damaged line in the current repository's own log. `--remote --markdown` is exit 1 and writes nothing.

### `pinax registry`

Records other repositories by path or URL, so that `overview` and `status --portfolio` can find them.

```
usage: pinax registry [-h] {add,rm,list} ...
```

```
usage: pinax registry add [-h] --id ID [--path PATH] [--url URL]
                          [--actor ACTOR] [--json]
```

```
usage: pinax registry rm [-h] --id ID [--actor ACTOR] [--json]
```

```
usage: pinax registry list [-h] [--json]
```

| Option | Takes | Default |
|---|---|---|
| `--id ID` | text matching `^[a-z0-9][a-z0-9_-]*$`, required for `add` and `rm` | none |
| `--path PATH` | an existing directory (`add` only) | none |
| `--url URL` | a Git remote URL, read by `overview --remote` (`add` only) | none |
| `--actor ACTOR` | writer (`add` and `rm`) | `operator@<host name>` |
| `--json` | flag | off |

`registry add` needs at least one of `--path` and `--url`. A path is stored as an absolute path with forward slashes, so your local path enters the log. The latest add or remove for an id wins. `registry add` and `registry rm` append `registry.repo_added` with payload `{repo_id, path, url}` and `registry.repo_removed` with payload `{repo_id}` to the local log. They do not commit, push or regenerate the projection, and they do not check the writer's shape.

```
pinax registry add --id app-docs --url https://example.com/app-docs.git
```

Repeating an `add` or an `rm` for the same id appends another event and exits 0.

**Prints:**

```
pinax: registry repo added: <id> -> <path>
       event_id=<first 12 characters>... seq=<n> actor=<writer>
```

for `add` with `--path`. With `--url` the target reads `url=<url>`, and with both it shows both, so the example above prints `-> url=https://example.com/app-docs.git`. `rm` prints `pinax: registry repo removed: <id>` followed by the same event line. `registry list` prints `pinax registry`, a blank line, and one line per entry with its id and its path, its `url=<url>`, or both.

**JSON:** for `add`, `actor`, `event_id`, `operation` (`add`), `path`, `repo_id`, `seq`, `ts`, `url` (`null` when not given). For `rm`, `operation` is `remove`. For `list`, `{"registry": {<id>: {added_at, added_by, path, url}}}`, where `path` and `url` appear only when set.

**Exit codes:** 0 on success. A parser error, such as a missing `--id`, is exit 2, and every other failure is exit 1.

## The web view

### `pinax web`

Serves one repository's board, item pages and event history, read-only, on your own machine.

```
usage: pinax web [-h] [--port PORT]
```

| Option | Takes | Default |
|---|---|---|
| `--port PORT` | integer from 0 to 65535 on the loopback address | `0`, which lets the operating system choose a free port |

The listener is always `127.0.0.1`, and there is no option to change it. The repository is the Git top level of the current directory, and a `--root` or `PINAX_ROOT` pin that names another directory makes the command refuse with exit 1. The routes are `/` for the board, `/history` for every event, and `/items/<id>` for one item page, and the query string is ignored. Each request reads the working-tree log again, so a refresh shows new events, uncommitted ones included. Only GET and HEAD are served. [portfolio-and-web.md](portfolio-and-web.md) describes the pages and the server's limits.

Every page is served with `Content-Type: text/html; charset=utf-8`, `Cache-Control: no-store`, `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'` and `X-Content-Type-Options: nosniff`. The page titles are `Pinax board`, `Pinax event history` and `Pinax item <id>`.

**Prints:** one line on standard output, `http://127.0.0.1:<port>/`, at once. Requests are not logged, but each request reads the log, so its warnings appear on standard error.

**Exit codes:** Ctrl-C closes the listener and exits 0. A port outside 0 to 65535, or not an integer, is a parser error with `--port must be an integer from 0 to 65535`, exit 2. A port that cannot be bound prints `pinax: cannot start web view on 127.0.0.1:<port>: <error>` and exits 1. A missing tracker prints `pinax: .ergon/log/ not found - run 'pinax init' first.` and exits 1.

**Refuses**, per request:

- any other path, such as `/board`, `/item/<id>` or `/history/`, with 404 and a page titled `Pinax page not found`;
- an unknown item, with 404 and `<id> was not found in this repository.`;
- a request without exactly one `Host` header equal to `127.0.0.1:<port>` or `localhost:<port>` (or the host name alone when the port is 80, compared without case), with 400 and a page titled `Invalid address`; `[::1]` is not accepted;
- any method other than GET and HEAD, with 405, `Allow: GET, HEAD` and an empty body.

## What the commands do not do

No command rewrites or deletes an event line, and a correction is always a new event, such as an annulment from `annul`. No command rebases, force-pushes, resets, deletes a branch or bypasses a hook with `--no-verify`. The commit that records an event holds only the writer's shard and the projection. After a rejected push, Pinax runs `git merge --no-edit FETCH_HEAD`, and that merge commit carries the remote's changes.

Because no command checks whether a status change makes sense, any write can move an item from any status to any other. Any writer may claim, release, decide under `flow`, and change any policy. Since any writer can set the process back to `flow`, `strict` guards against mistakes and gives no access control.

The nine commands under [Commands that never push](#commands-that-never-push) leave their changes unpublished. Their changes reach other clones only when you commit and push them.
