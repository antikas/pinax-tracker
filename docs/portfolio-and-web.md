# Several repositories, and the browser view

A project rarely stays in one repository. Once your work is spread over an application, a billing service and a documentation site, each with its own Pinax tracker, you have to open each repository in turn to learn what is next and what is blocked. `pinax overview` gives you one combined view: for each repository, its item counts by status, its next item, and its parked and blocked items, followed by one list of what needs attention across all of them. It does not list every open item, so you still read the rest on each repository's board. Use `pinax overview` for that summary, over the folders you name and the repositories you register with it. Use `pinax status --portfolio` for each repository's full status view, with work in progress and recently finished work, over the folders set in `PINAX_ROOTS`, or `~/src` when it is not set.

You may also want to read one board in a browser, and a colleague may ask to see it. `pinax web` serves one repository's board, item pages and history to a browser on this machine only, and it writes nothing. A colleague who wants the same pages pulls the repository and runs `pinax web` in their own clone, because the same log gives the same pages. They can also read the board without Pinax, because it is committed to the repository as `.ergon/board.md` and they can open that file on your Git host. The [README](../README.md) explains the tracker itself, and [commands.md](commands.md) lists every option and output.

## Part one: several repositories

### The example

Take three repositories in one folder, each with a tracker that `pinax init` has set up. Each holds two items, and one of the two is blocked on a decision:

- `~/work/app`, the application, with `app-zchg` "Choose the payment provider" (blocked) and `app-4x56` "Write the install guide";
- `~/work/billing`, the billing service, with `bil-locs` "Agree the refund policy" (blocked) and `bil-pg56` "Add invoice numbers";
- `~/work/docs-site`, the documentation site, with `doc-e2wu` "Pick the site theme" (blocked) and `doc-imp2` "Publish the quick start page".

The hub is the repository you run `pinax overview` in, here `~/work/app`. The scan roots are the folders that `pinax overview` searches for repositories with a `.ergon/` directory, and they are `~/src` unless you set others. The registry is a list of extra repositories that the hub keeps in its own log, described below. `pinax overview` reads the hub, every repository in the hub's registry that has a path, and every repository the scan finds.

### Read the combined view

The full form is:

```
pinax overview [-h] [--roots ROOTS] [--max-depth MAX_DEPTH] [--json] [--markdown] [--remote]
```

From `~/work/app`, scan the folder that holds all three:

```
pinax overview --roots ~/work
```

It prints:

```
# Pinax portfolio overview

## app

- status: ok · 2 items · blocked=1 queued=1
- next: app-4x56  Write the install guide
- parked (0): (none)
- blocked (1):
  - app-zchg · Choose the payment provider · gate: decision

## billing

- status: ok · 2 items · blocked=1 queued=1
- next: bil-pg56  Add invoice numbers
- parked (0): (none)
- blocked (1):
  - bil-locs · Agree the refund policy · gate: decision

## docs-site

- status: ok · 2 items · blocked=1 queued=1
- next: doc-imp2  Publish the quick start page
- parked (0): (none)
- blocked (1):
  - doc-e2wu · Pick the site theme · gate: decision

## Needs attention (cross-repo)

- app/app-zchg · Choose the payment provider · blocked: gate=decision
- billing/bil-locs · Agree the refund policy · blocked: gate=decision
- docs-site/doc-e2wu · Pick the site theme · blocked: gate=decision
```

The hub lies inside the scan root here, and it appears once. Each section is named by the repository's id. The hub and each scanned repository take their directory name as id, and when two directories share a name, Pinax adds the parent directory's name, and then a number if they still collide. A registry entry keeps the `--id` you gave it. The sections appear in order of id.

The status line reads `ok` for a tracker Pinax could read, followed by the item count and the count in each status. It reads `not initialised` for a repository that has no `.ergon/log`. It reads `damaged`, followed by the file, the line number and the reason, for a repository whose log holds a line that cannot be used as an event; with `--json` such an entry carries `damaged`, an object with `file`, `line` and `reason`, in place of the counts. When a repository has claim conflicts that Pinax has already settled, a `notices` line follows, with their number and a pointer to `pinax doctor`.

The last section lists, across all the repositories, each blocked item as `<repository id>/<item id> · <title> · blocked: gate=<gate>` and each parked item as `<repository id>/<item id> · <title> · parked: <reason>`. In the remote view, described below, it also lists each remote that could not be read.

With `--json`, the same view is one object, `{"repos": [...]}`. Each entry carries `id`, `path`, `initialised`, `total_items`, `by_status`, `next`, `parked`, `blocked` and `notices`, the number of settled claim conflicts, for example:

```
{"blocked": [{"gate": "decision", "id": "bil-locs", "title": "Agree the refund policy"}], "by_status": {"blocked": 1, "queued": 1}, "id": "billing", "initialised": true, "next": {"id": "bil-pg56", "title": "Add invoice numbers"}, "notices": 0, "parked": [], "path": "/home/alex/work/billing", "total_items": 2}
```

Pinax expands `~` in a root. To scan several folders, separate them with commas. To set the roots once, set `PINAX_ROOTS` and run `pinax overview` with no option, which gives the same view. In a POSIX shell:

```
export PINAX_ROOTS=~/work
pinax overview
```

In PowerShell:

```
$env:PINAX_ROOTS = "$HOME\work"
pinax overview
```

`PINAX_ROOTS`, ending in S, lists the folders to scan. `PINAX_ROOT`, with no S, is a different variable: it names the one repository that every command expects, as the browser view section explains.

A root that does not exist is skipped without an error: `pinax overview --roots ~/nowhere` exits 0 and shows the hub alone. The scan goes three levels deep unless you pass `--max-depth` or set `PINAX_ROOTS_MAX_DEPTH`. It skips `.git`, `.ergon`, `node_modules`, `__pycache__`, `.venv`, `venv`, `.tox`, `.mypy_cache` and `.pytest_cache`, and it does not look inside a repository it has found. It drops bare repositories and keeps one entry for a repository and its linked worktrees.

The scan does not follow a symbolic link it meets inside a root. Separately, Pinax compares repositories by the physical folder each path leads to. A repository reached by two paths, for example as the hub and again through a root that is a symbolic link or a Windows directory junction, therefore appears once.

### Register a repository the scan cannot reach

The registry is a list of extra repositories, each with an id and a path, a URL or both, that the hub keeps as events in its own log. Use it when the hub and the repositories you want to see do not share a scan root. Suppose you plan your work in a fourth repository, `~/planning`, outside `~/work`, with its own tracker set up by `pinax init`, and want it to show the billing service. From `~/planning`, run:

```
pinax registry add --id billing --path ~/work/billing --actor alex@laptop
```

It prints:

```
pinax: registry repo added: billing -> /home/alex/work/billing
       event_id=wm5cmsemkxwz... seq=2 actor=alex@laptop
```

The hub needs a tracker of its own. In a repository without one, the command exits 1 with `pinax: .ergon/log/ not found - run 'pinax init' first.`

The full forms are:

```
pinax registry add [-h] --id ID [--path PATH] [--url URL] [--actor ACTOR] [--json]
pinax registry rm [-h] --id ID [--actor ACTOR] [--json]
pinax registry list [-h] [--json]
```

The id must start with a lower-case letter or a digit, and may contain only lower-case letters, digits, `-` and `_`. The path must be an existing directory, and Pinax stores it as an absolute path with forward slashes. Pinax refuses an invalid id, a path that is not an existing directory, and an entry with neither a path nor a URL, each with exit code 1. A missing `--id` is refused with exit code 2. Running the same `add` again exits 0 and records the entry again, and a second `rm` of an id already removed also exits 0. The last change to an id wins.

Read the registry back with `pinax registry list`:

```
pinax registry

  billing               /home/alex/work/billing
```

With `--json` it prints `{"registry": {"billing": {"added_at": "2026-11-23T15:00:51Z", "added_by": "alex@laptop", "path": "/home/alex/work/billing"}}}`.

Now run `pinax overview` from `~/planning`, with `PINAX_ROOTS` not set and no tracker under `~/src`. It prints a `## billing` section and a `## planning` section, in order of id, and lists `billing/bil-locs` under `## Needs attention (cross-repo)`. If `PINAX_ROOTS` still names `~/work`, the scan adds `app` and `docs-site` and their blocked items to the same view.

`pinax registry add` and `pinax registry rm` append one event to your log in the hub and do nothing else. They do not commit, push or rebuild the board, and they do not check that the writer is written `role@host`. The event reaches other clones only when a later commit includes your shard, the file in `.ergon/log/` named after you. That is a commit you make yourself, or the next write command you run as the same writer, such as `pinax add`. The event records the absolute path from your machine, so that path is pushed with the log.

### Keep a copy in the hub

Write the view to a file at the root of the hub:

```
pinax overview --markdown
```

It prints one line, such as `pinax: wrote PORTFOLIO.md (2 repo(s), generated 2026-11-23T15:00:51Z)`, in place of the view. The file holds a note not to edit it by hand, the same body as the printed view, the time it was generated, and the current commit of each repository (`(no git)` where there is none). Because the command commits nothing, `git status --short` then shows `?? PORTFOLIO.md`. Commit the file yourself when you want others to see it. Because `--markdown` replaces an existing `PORTFOLIO.md` at the hub root without asking, commit or rename a hand-written file of that name before you run it. When you pass both `--json` and `--markdown`, `--json` wins and no file is written.

### Remove an entry

When you no longer want `billing` in the view, remove it from the registry:

```
pinax registry rm --id billing --actor alex@laptop
```

It prints `pinax: registry repo removed: billing`, followed by a line with the event id, its sequence number and the writer.

### See what has been pushed

The local view reads the working copies on your machine. To see what your colleagues can see, register the repository with the URL of its remote and ask for the remote view. Each `add` replaces the whole entry for its id, so pass `--path` and `--url` together. An `add` with only `--url` removes the path, and the repository then drops out of the local view.

```
pinax registry add --id billing --path ~/work/billing --url https://git.example.com/team/billing.git --actor alex@laptop
pinax overview --remote
```

Use your own remote's URL in place of this one. The tip is the newest commit on the remote's default branch. For each registry entry with a URL, in order of id, Pinax finds the tip with `git ls-remote --symref`. It then clones only that commit into a temporary directory, with no files checked out and no file contents fetched in advance, and reads the log files of that commit from Git, which fetches just those contents. It deletes the directory afterwards. For a GitHub URL, when Git cannot reach the remote, Pinax falls back to the GitHub contents API, without authentication unless `GITHUB_TOKEN` is set. Each Git call may take up to 300 seconds, and each HTTP request up to 30 seconds.

Each remote summary adds the `url` and the `sha` of the tip it read. A remote that cannot be read shows the status `unreachable` with the error, and is listed under needs attention. A remote with nothing on its default branch shows `(nothing pushed)`. When no registry entry has a URL, the command prints a warning and an empty view, and exits 0. Because the remote view reads only registry entries with a URL, it leaves out the hub and ignores `--roots` and `--max-depth`. `pinax overview --remote --markdown` exits 1 and writes nothing.

### The portfolio scope of `pinax status`

The full form of the command is:

```
pinax status [-h] [--actor ACTOR] [--json] [--repo PATH] [--portfolio] [--since Nd] [--all] [--all-branches] [--under ITEM_ID] [id] [state]
```

`pinax status --portfolio` prints the status view of every repository under `PINAX_ROOTS`, or `~/src` when it is not set, together with the current repository and the path entries of its registry. It has no `--roots` option. With `PINAX_ROOTS=~/work`, run from `~/work/app`, it shows `app`, `billing` and `docs-site` once each, starting with:

```
pinax status

repo: app @ main
  building (0):
    (none)
  shipped (0):
    (none)
  parked / blocked (1):
    app-zchg  Choose the payment provider  blocked: decision
  next:
    app-4x56  Write the install guide
  ready queue: 1 item(s)
```

`building` lists the items in progress, and `shipped` lists the items marked done in the last seven days; `--since Nd` changes that window, and `--all` shows every done item. Run from `~/planning`, outside the roots, the command shows `planning` first and then the three repositories under `~/work`.

When the scan finds no repository, the command prints only warnings: it shows neither the current repository nor its registry entries. `pinax status` run outside any tracker also gives the portfolio scope, and uses the registry of the first repository the scan finds. With `--json`, the result carries `"schema": "pinax.status.v1"`, `"scope": "portfolio"`, a `repos` list, and a `warnings` list when there are warnings. `--under` cannot be combined with `--portfolio`. The command only reads.

### Dispatch and reconcile stay inside one repository

`pinax dispatch` and `pinax reconcile` act on the current repository only, so run them in each repository where you need them:

```
pinax dispatch [-h] [--max N] [--claim] [--actor ACTOR] [--json]
pinax reconcile [-h] [--file PATH] [--actor ACTOR] [--dry-run] [--json]
```

`pinax dispatch` lists this repository's ready items in the order `pinax next` would pick them. `--max N` caps the list when N is above 0, and says so:

```
pinax dispatch: 1 item(s) (capped at 1)
  1. app-4x56  [queued]  Write the install guide
```

With `--json` the command prints an array of `{id, title, status, owner, created_at}`. With `--claim`, it appends one claim for each listed item to your local log and rebuilds `.ergon/board.md` and the item pages, and it does not fetch, commit, push, check the `role@host` form or check the clock. With no ready item it prints `pinax dispatch: ready queue is empty - nothing to dispatch.`

`pinax reconcile` reads `BACKLOG-OFFLINE.md` at the root of the current repository, or the file you name with `--file`. It imports its done and park lines as events in this repository's log, moves each processed line into a dated section of the same file, and rebuilds `.ergon/board.md` and the item pages. It does not commit or push. With `--dry-run` it prints what it would import, reject and skip, and changes nothing. `--actor` names you as the reconciler, and the actor written on each line stays the writer of its event. When the file does not exist, both forms exit 0 with `pinax reconcile: no offline file at /home/alex/planning/BACKLOG-OFFLINE.md - nothing to reconcile.` [commands.md](commands.md) gives the line format.

### What each command reads and writes

| Command | Reads | Writes |
|---|---|---|
| `pinax overview` | the hub, the registry's path entries, the repositories under the scan roots | nothing |
| `pinax overview --markdown` | the same | `PORTFOLIO.md` at the hub root, uncommitted |
| `pinax overview --remote` | the default branch of each registry entry with a URL | nothing in any repository; a temporary clone, deleted afterwards |
| `pinax registry add`, `pinax registry rm` | whether the path is an existing directory | one event in your log in the hub, uncommitted |
| `pinax registry list` | the hub's log | nothing |
| `pinax status --portfolio` | the repositories under the scan roots and, when the scan finds any, the current repository and the path entries of its registry | nothing |
| `pinax dispatch` | the current repository | with `--claim`, claim events in your local log, `.ergon/board.md` and the item pages, uncommitted |
| `pinax reconcile` | `BACKLOG-OFFLINE.md` in the current repository | outside `--dry-run`, events in your local log, `.ergon/board.md`, the item pages, and the processed lines moved in that file, uncommitted |

## Part two: the browser view

### Start it

The full form is:

```
pinax web [-h] [--port PORT]
```

From the repository you want to read, run:

```
pinax web
```

The command prints one line on standard output, such as `http://127.0.0.1:34713/`, and keeps serving until you stop it. Open that address in a browser on the same machine. Without `--port`, the port is 0, which lets the operating system choose a free port, and the printed address names the port it chose. Pass `--port 8765` to fix the port, and the address becomes `http://127.0.0.1:8765/`.

You choose the repository by where you start the server: it serves the Git top level of the current directory, and it keeps that repository until it stops. The root pin is a safety check that every command accepts. You name the repository you expect with the global option `--root PATH`, written before the command name as in `pinax --root ~/work/app web`, or with the variable `PINAX_ROOT`. When the pin differs from the Git top level of the current directory, the command refuses with a message that starts `pinax: ROOT MISMATCH` and exits 1.

A port outside 0 to 65535 exits 2 with `--port must be an integer from 0 to 65535`. A port that cannot be used exits 1 with `pinax: cannot start web view on 127.0.0.1:<port>: <error>`, and a directory with no tracker exits 1 with `pinax: .ergon/log/ not found - run 'pinax init' first.`

### The pages

The server has three routes, and each answers with status 200 and the page title shown here:

| Path | Page title | What it shows |
|---|---|---|
| `/` | `Pinax board` | the board |
| `/items/<id>`, such as `/items/app-4x56` | `Pinax item app-4x56` | one item's page |
| `/history` | `Pinax event history` | every event |

The board has the heading `Board` and the same sections as `.ergon/board.md`, including resolved claim conflicts. Each row links to its item page and shows the item's title, or its id when the title is empty. Under the title it shows the id and the rest of that item's row in `.ergon/board.md`: the status, the owner and the blocking items, or, for a blocked or parked item, its gate or its reason. An empty tracker shows `No items yet.`

An item page shows that item's Markdown page as plain text, including its briefing. An id the tracker does not hold returns status 404 with the text `<id> was not found in this repository.`

The history lists every event in the order the tracker applies them. For each event it shows the type, `#<seq>`, the timestamp, the writer and the event id, and the payload as JSON with sorted keys, indented by two spaces. An empty log shows `No events yet.`

Any other path returns status 404 with a page titled `Pinax page not found`, including `/board`, `/item/app-4x56` and `/history/`. A query string is ignored, so `/?x=1` returns the board. Every request reads the log in the working tree again, so a refresh shows new events, including events that are not yet committed.

### Read-only and local

`pinax web` listens on `127.0.0.1` only, and there is no option to choose another address. It answers `GET` and `HEAD`, and every other method, `POST` included, gets status 405 with `Allow: GET, HEAD` and an empty body. The request handler only reads the log and renders pages, so serving pages changes no file in the repository.

Every page is sent with these headers, and all tracker text is escaped as HTML:

```
Content-Type: text/html; charset=utf-8
Cache-Control: no-store
Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'
X-Content-Type-Options: nosniff
```

Requests are not logged, and each one runs in its own thread. Warnings about the log, such as an event that no longer matches its id, are printed in the terminal where the server runs, and never on a page.

### What it checks about each request

Before it looks at the path, `pinax web` checks the `Host` header. A request must carry exactly one, equal to `127.0.0.1:<port>` or `localhost:<port>`, in any case. When the server runs on port 80, the names `127.0.0.1` and `localhost` without a port are accepted too. Any other request, such as one with `Host: example.invalid`, gets status 400 and a page titled `Invalid address`. A Host of `[::1]` is refused. The server listens on the IPv4 address `127.0.0.1` only, so whether a `localhost` address reaches it depends on how your browser resolves that name; open the printed address.

Because the view has no sign-in, do not forward or proxy its port, for example with an SSH tunnel. A tunnel that arrives as `localhost:<port>` passes this check, and whoever reaches it reads every event, including writer names and the absolute paths in registry events. A colleague who wants the pages runs `pinax web` in their own clone.

### Stop it

Press Ctrl-C in the terminal where it runs, and the server closes its listener and exits 0.

## Limits

### What the combined view cannot show

Because `pinax overview` reads only the hub, the registry's path entries and the repositories under the scan roots within the scan depth, a repository anywhere else stays out until you register it or widen the scan. A root that does not exist gives no error, so check that every repository you expect has its section.

The combined view shows the counts, the next item, and the parked and blocked items of each repository. To read any other item, open that repository's board. No command lists every open item of several repositories in one view. The local view reads working copies on this machine. The remote view shows only pushed work, leaves out the hub, and cannot be written to `PORTFOLIO.md`. `PORTFOLIO.md` holds the view at the moment it was written, and `pinax overview` never commits it.

Because registry commands do not commit, your registry changes stay on your machine until a commit includes your shard. No command dispatches or reconciles across repositories.

### What the browser view is not

The browser view serves one repository, chosen when it starts. It shows no warnings, metrics, status view or portfolio, it shows notes only as events in its history, and that history lists annulled events with no mark that sets them apart from other events. Because it has no sign-in, any user of the same machine can read it. Because it listens only on `127.0.0.1`, no other machine can reach it unless you forward the port, which you should not do.
