# Pinax

When you work on a repository alone, a list in a text file is enough to remember what is left to do. Once several people, or several coding assistants, work on the same repository from different machines, two of them can start on the same task without knowing it, and a task finished on one laptop can still look open on another. Later, nobody can say for certain who changed which task, or when.

Pinax is a task list that lives inside the repository and reaches everyone through Git. Before you start a task you put your name on it with one command, and everyone else sees your name on it the next time they pull. Pinax calls that a claim. If someone else then tries to take the same task, Pinax refuses and tells them it is yours.

By default a claim lasts 24 hours, and after that someone else can take the task. When two people claim the same task within a few seconds of each other, the earlier claim wins, and the other person finds out at once or from the task list after their next pull. Pinax shares claims only by pushing straight to the repository's main branch, which Git calls the default branch, so it does not fit a repository where every change must go through a pull request. It cannot stop someone who starts work without claiming the task first.

## One task from start to finish

You can install Pinax first, as described under [Requirements and install](#requirements-and-install). The steps below follow one task. Pinax calls each task an item. You see the task list as a Markdown file, `.ergon/board.md`, which Pinax calls the board. The board groups the items by their status, and each group is a lane.

1. Run `pinax init` once for the repository, in a clone whose remote holds at least one commit. A folder named `.ergon/` appears. It holds the log of every change in `.ergon/log/`, the board in `.ergon/board.md`, and a folder `.ergon/items/` for one page per item. It also holds `.ergon/.gitattributes` and `.ergon/.gitignore`, which tell Git how to treat the log.
2. Commit that folder once, by hand, and push it.
3. Run `pinax add --title "Write the install guide" --prefix app --actor alex@laptop`. The prefix starts every item id. `--actor` gives the name the change is recorded under, which Pinax calls the writer; a writer can be a person or an assistant. Pinax prints the new item's id, such as `app-4x56`.
4. Look at what changed. A line appears in `.ergon/log/alex-laptop.jsonl`, and a row appears in the `## Queued` lane. A page appears at `.ergon/items/app-4x56.md`, and a commit named `pinax: item.created app-4x56` is pushed.
5. Run `pinax claim app-4x56 --actor alex@laptop`. The item moves to the `## Building` lane with `alex@laptop` as its owner. Run `pinax board` to print the board in your terminal; every clone shows the same after a pull.
6. Write a short note of what you did into `note.md`, and run `pinax done app-4x56 --briefing note.md --actor alex@laptop`. The item moves to the `## Done` lane, and its page shows your note under `## Briefing`.
7. Run `pinax verify`. It prints `pinax verify: OK - projection matches log.` and exits 0. The projection is the board and the item pages together. Pinax rebuilds them from the log after every change, so they always show what the log says.

## How a change travels

This section follows the item from step 3 through the system.

**The line in the log.** Each change is one line of JSON in the log, which Pinax calls an event. It records the change, its time and its writer. A writer name has the form `role@host`. The role says who writes, such as `alex` for a person or `assistant1` for a coding assistant, and the host names the machine. Pinax writes the event to a file named after its writer, here `alex-laptop.jsonl`. To build that file name, it replaces `@`, `/`, `\` and spaces with `-`. Names that then differ only in letter case share one file. The event's id is a hash of its own contents and of the previous event that writer wrote. Each event also carries a number, one more than the highest number in the log when it was written, which Pinax calls the sequence number.

**The check against the remote.** Before it writes, Pinax fetches `origin`. It reads the events already on the remote's default branch together with your local ones. Pinax calls an event published once it is on the remote's default branch. When a command names an item that neither side holds, for example `pinax claim app-9zzz`, Pinax refuses it and appends nothing.

**The pages.** Pinax reads every event and works out the current state of every item. It then rewrites `.ergon/board.md` and each item page from that state. The same log always gives the same pages, whatever the order of the lines and on any operating system.

**The commit.** Each write command commits its event in a commit of its own. When it retries a push after a merge, it may add a merge commit and a commit that rebuilds the projection. Pinax stages the file in `.ergon/log/` named after you, which it calls your shard, and the projection. It commits only those paths, here with the subject `pinax: item.created app-4x56`. Anything else you had staged stays staged and stays out of that commit. Git hooks run as usual, and Pinax never bypasses them.

**The push.** Pinax pushes when the branch you have checked out is the remote's default branch. If the remote has moved on, Pinax pulls, merges, rebuilds the projection and tries again, up to three attempts in total. It never rebases, force-pushes, resets or deletes branches. The push can still be rejected after three attempts, or the merge between attempts can fail to finish. Then the command exits 5, and the event stays committed only in your clone. A claim is the exception, because it cancels itself. [docs/sharing-and-sync.md](docs/sharing-and-sync.md) gives the recovery.

On any other branch, Pinax commits the event locally and prints `pinax: the push was refused because <branch> is not the origin default branch <default branch>; the event is committed locally and not published.` A claim made there is not shared with anyone, and `pinax claim` exits 4.

**Another clone.** A colleague pulls. Git merges the log files line by line, because `.ergon/.gitattributes` tells it to keep the lines of both sides. Their Pinax reads the merged log and arrives at the same state you see. When they now run `pinax claim` on your item, the command exits 3 and appends no claim. Its message names you and the time of your claim, and the board keeps you as the owner. If they had a claim of their own waiting unpublished, the command cancels it in their log. When you run `pinax claim` again on an item you already hold, it exits 0 and appends nothing.

**Two claims close together.** Each event's time comes from the clock of the machine that wrote it. When two people claim an item before either has seen the other's claim, both claims are recorded and the one with the earlier time wins. A machine whose clock runs a few seconds behind yours can therefore take an item you have just claimed, if it fetched before your claim was pushed. To limit that, `pinax claim` refuses with exit 6 when its time is more than five seconds behind the newest event on the remote that no annulment cancels. `PINAX_CLOCK_TOLERANCE_S` changes the five seconds. If your claim reaches the remote second and loses, `pinax claim` exits 3, and your claim stays in the log as superseded. If your claim reaches the remote first and has the later time, your command exits 0. The board then shows the other writer as the owner after your next pull, and lists that conflict under `## Claim conflicts (resolved)`.

## Rules you set

You and the other people on the project set the rules that decide how work may move. Each rule is an event in the log like any other. You can read the rules on the history page of `pinax web`, a read-only view of one repository in your browser, or in `pinax board --json`. You change a rule with a `pinax policy` command.

**Blocking.** When an item cannot go on, block it with `pinax block <id> --gate <gate>`. The gate is the reason, and you choose it from `scope`, `decision`, `destructive` and `proposal`. A blocked item can also wait for someone's approval. The kind of approval it waits for is called its owner gate. Here "owner" means the person who must approve, who is often a different person from the writer holding the claim. The default classes are:

- production deployment
- publication
- real personal data
- irreversible or destructive act
- material change of scope, cost or architecture
- login-bound act or owner-held input
- start signal for a material plan

`pinax policy owner-gates --class TEXT` replaces that list with your own.

**Parking.** To set an item aside with a reason, park it with `pinax park <id> --reason TEXT`.

**Decisions.** To record the answer to a blocked item, run `pinax decide`. It stores the option chosen, a label, an effect and a reason, and the item page shows the latest one. With `--effect release` the item's status does not change. With `--effect park` the item is also parked. The writers allowed to decide form the decision-actor policy, which you set with `pinax policy decision-actors --decider ROLE@HOST`.

**The process.** The process is either `flow` or `strict`, and you set it with `pinax policy process --mode`. The default is `flow`, where any writer may decide and any class is accepted. Under `strict`, only listed writers may decide. A block with the gate `decision` or `destructive` must then also name a class on the list. Pinax refuses a block or a decision that breaks these rules with exit code 8 and appends nothing. A strict policy pushed from another clone binds your next write. Because any writer can switch strict back to flow, strict protects you from mistakes and gives no access control.

**How long a claim holds.** A claim is live from the moment it is recorded until it is released, expires, or loses to a claim with an earlier time. Any writer may release any claim by running `pinax release <id> --reason TEXT`. A claim expires once its expiry time has passed and some later event, of any kind, has been recorded. In a log where nothing happens, it never expires. The default expiry is 24 hours. `pinax policy claim-expiry --hours N` changes it for claims made after that event.

**What a note may point at.** A note may point at a path inside the repository, written from its root without `..`, or at a web page, and `pinax policy note-refs --prefix TEXT` adds other prefixes that its reference may start with.

## Checks on the record

`pinax verify` reads the whole log and checks it. If any check fails, it exits 1.

Each event must still match its own id, unless an annulment in effect names it. An annulment is an event, written by `pinax annul`, that cancels an earlier event while its line stays in the log. To undo an annulment, annul it in turn, and the event it cancelled counts again.

Each event Pinax writes carries format version 2. When a version 2 event names a previous event, that event must still be in the log. So a deleted event is caught when a later version 2 event names it, as long as that later event is unchanged and not annulled.

The board and every item page must match a fresh rebuild from the log. A board or item page that no longer matches is called drift. Finally, the log must not be hidden from Git by a `.gitignore` rule.

Git runs a script before each commit, called the pre-commit hook. `pinax init` installs one that runs `pinax verify`. When the hook cannot find Pinax, it lets the commit through with a warning.

`pinax verify --fix` rebuilds the board and pages when the log passes its checks. It never changes an event. While the log fails its checks, it refuses to touch the projection.

Because nothing signs the events, `verify` cannot see several kinds of edit. It does not catch the deletion of the newest event of any writer, or of the newest event on either branch of a merge. A writer with access to the repository can add a forged event with a correct id. Such a writer can also rewrite an event and every event after it. A line that is not valid JSON is skipped with a warning. The skip itself does not fail `verify`, although a later check can fail because of what the line held. Sequence numbers that go backwards are not caught either. For everything `verify` cannot see, Git history is the only record.

## Limits

Pinax has no lock between machines, because a claim is checked against what the remote holds when it runs. When another writer's live claim is already there, the command exits 3 and writes nothing. When two writers claim before either has seen the other's claim, the earlier time wins. A losing writer whose claim reaches the remote second is told by exit 3. When the losing claim reached the remote first, its command has already exited 0. That writer learns of the loss only from the board after the next pull.

Pinax cannot stop someone who starts work without claiming it. It cannot stop someone who works on a branch other than the remote's default branch either, because claims made there are not shared.

Pinax has no access control. Any writer may claim any item, release a claim, decide under `flow`, and change any policy.

Pinax has no rules about the order of statuses. The command `pinax status <id> <state>` sets any status from any status, including back from done. These are the statuses:

| Status | Meaning |
|---|---|
| `queued` | Set by `pinax add`. Pinax offers the item as ready to take. |
| `ready` | Set by you. Pinax offers the item as ready to take. |
| `building` | A working status. When you claim an item, the board shows it in this lane. |
| `blind-verify`, `adjudicate` | Two more working statuses, which `pinax status` lists with `building`. |
| `done`, `blocked`, `parked` | Finished, blocked and parked items. |

Pinax runs only when you call it. The one exception is `pinax web`, which serves one repository read-only on `127.0.0.1` with no sign-in.

Some commands write events without committing or pushing them. [docs/commands.md](docs/commands.md) lists them under "Commands that never push" and describes each one.

| Command | What it does |
|---|---|
| `pinax init` | Sets up the tracker. |
| `pinax status <id> <state>` | Sets an item's status. |
| `pinax dispatch --claim` | Claims the ready items it lists, all at once. |
| `pinax registry add`, `pinax registry rm` | Record other repositories for the portfolio view. |
| `pinax reconcile` | Imports done and park lines from a text file. |
| `pinax doctor --reconcile` | Walks you through repairing incomplete records. |

Their events reach other clones only once a commit includes the shard they went into. That commit can be your own `git add .ergon` and commit, or the next write command by that shard's writer.

Without a reachable remote, Pinax sees only your local log. A policy or a claim set on the remote stays invisible until you pull.

## For engineers

### Requirements and install

You need Python 3.10 or later and Git on your `PATH`. Pinax runs on POSIX systems and on Windows. It has no other runtime dependencies, and it is released under the MIT licence.

```
python -m pip install pinax-tracker
pinax --version
```

The second command prints the installed version. [docs/commands.md](docs/commands.md) lists every command with its exit codes.

### Track your first item without a remote

`pinax claim` needs a remote. In a repository with no remote, you add items and finish them without claiming. Start in a Git repository with at least one commit and no `origin`, and run these commands from its root:

```
pinax init
git add .ergon
git commit -m "Add the Pinax tracker"
pinax add --title "Write the install guide" --prefix app --actor alex@laptop
```

`pinax init` prints `pinax: initialised .ergon/ in <root>` and `pinax: emitted ergon.created + phase.opened (actor=operator@<host>)`. It commits nothing, which is why you commit `.ergon/` yourself. The hook then prints `pinax verify: OK - projection matches log.`

`pinax add` prints `pinax: created item <id> - "Write the install guide" in <root>/.ergon`. A second line gives the event id, the sequence number and the writer. It also prints `pinax: origin could not be reached; the event is recorded locally and not published.` It exits 0, with the item committed locally.

Write a few lines about the finished work into `note.md`. Then run these commands, with your item's id in place of `<id>`:

```
pinax done <id> --briefing note.md --actor alex@laptop
pinax verify
```

`pinax done` prints `pinax: item <id> marked done by alex@laptop in <root>/.ergon`. A second line gives the briefing length, the event id and the sequence number, and the same warning about `origin` follows. It exits 0. `pinax done` needs no claim. Pinax stores the full text of `note.md` in the event and does not commit the file itself. `pinax verify` prints `pinax verify: OK - projection matches log.` and exits 0.

If you run `pinax claim <id> --actor alex@laptop` here, it prints `pinax: origin could not be reached, so nothing was appended.` and exits 4. To skip the fetch and the push on purpose, pass `--offline` or set `PINAX_OFFLINE=1`. This works for every write command except `pinax claim`.

### Track your first item with a remote

Start in a clone whose remote is named `origin`, with its default branch checked out and at least one commit already pushed. Run these commands from its root, once for the repository:

```
pinax init
git add .ergon
git commit -m "Add the Pinax tracker"
git push
pinax add --title "Write the install guide" --prefix app --actor alex@laptop --json
```

`pinax add --json` prints one line: `{"actor": "alex@laptop", "event_id": "<event id>", "item_id": "<id>", "prefix": "app", "published": true, "root": "<root>/.ergon", "seq": 2, "status": "queued", "sync_status": "pushed", "title": "Write the install guide", "ts": "<time>"}`. A write can exit 0 without reaching the remote, so check `published`. It is true only when this run's push was accepted or the event was already published. `pinax claim`, `pinax done` and the other write commands accept `--json` too.

Now claim the item:

```
pinax claim <id> --actor alex@laptop
```

`pinax claim` prints `pinax: claimed item <id> by alex@laptop in <root>/.ergon`. A second line gives the event id and the sequence number, and it exits 0. When it exits 3 instead, another writer's claim holds the item.

To pick another item, run `pinax ready`. It lists the items that are queued or ready, have no live claim and are not waiting on another item. `pinax next` prints the one item to take first. It chooses by priority, then by the length of the chain of items waiting on it, then by age. An item waits on another when someone has run `pinax dep add <other> --blocks <item>`. It stays out of `pinax ready` until the other item is done.

Before you start work, confirm the claim:

```
git pull --no-rebase
pinax board
```

A claim with an earlier time from another machine can still reach the remote after yours, so exit 0 alone does not prove the item is yours. Check that the item's row under `## Building` names `alex@laptop`.

Then write a few lines about the finished work into `note.md`. Finish the item and check the log:

```
pinax done <id> --briefing note.md --actor alex@laptop
pinax verify
```

`pinax done` prints `pinax: item <id> marked done by alex@laptop in <root>/.ergon`. A second line gives the briefing length, the event id and the sequence number, and it exits 0 with the event pushed. `pinax verify` prints `pinax verify: OK - projection matches log.` and exits 0.

### Set up every further clone

Each colleague or assistant with its own clone installs Pinax, clones or pulls the repository, and runs `pinax init` in it. The `.ergon/` folder is the tracker. When it already exists, `pinax init` prints `pinax: .ergon already initialised (idempotent - skipped event emit).` It appends nothing and installs the pre-commit hook. The hook lives inside each clone's own Git directory, so it does not travel with the repository. Each writer then passes its own `--actor` name on every command.

### Add a remote later

Add the remote and push the branch once by hand, for example with `git remote add origin <url>` and `git push -u origin main`. Until that first push, write commands print `pinax: origin was reached and publishes no default branch; the event is recorded locally and not published.` They exit 0, and `pinax claim` exits 4. After the first push, the next write command pushes, and the items you added while there was no remote are published with it. From then on you can claim them like any other item.

### Writers and prefixes

When you leave out `--actor`, Pinax records `operator@<your host name>`, so your machine's host name is pushed with the log. Every writer on that machine then shares that one name, and no setting changes the default. Give each person and each coding assistant its own writer name, and pass it with `--actor` on every command.

The write commands that fetch, commit and push, such as `add`, `claim` and `done`, check the name. They refuse a name that is not `role@host` with exactly one `@`, and exit 2. `pinax init` and the commands in the table under "Limits" skip that check. Any name of that form is accepted, in any directory.

`--prefix app` gives ids such as `app-4x56`, and the default prefix is `pnx`. Once a tracker has items, `pinax add` refuses a prefix it has not seen before unless you also pass `--allow-new-prefix`. Repeat `--prefix app` on every later `add`.

### Repair the board and pages

When `pinax verify` reports drift, the hook refuses every commit in that clone, yours included. Run `pinax verify --fix`, and stage the result with `git add .ergon`. If the drift came from your own hand edit that you had not committed, the pages now match the last commit, and there is nothing left to commit. The next write command also rebuilds the pages.

The log files merge line by line, and the board and item pages do not. When `git pull --no-rebase` stops on a conflict in `.ergon/board.md` or an item page, run `pinax verify --fix`, `git add .ergon`, `git commit -m "Rebuild the Pinax pages after a merge"` and `git push`.

### Upgrading

Before any clone writes, make sure every clone, and every hook's `PATH`, runs 0.5.0 or later. The hook runs the first `pinax` it finds on `PATH`. An older `pinax` there is the one that checks every commit in that clone, whatever you have installed elsewhere. While clones run different releases, one of them may report an item page as drift. After you upgrade that clone, `pinax verify --fix` rebuilds the page, and you commit it. An item that stayed cancelled under 0.4.0 after you annulled its annulment is in effect again under 0.5.0, and annulling its `item.created` event again cancels it.

### The other documents

| Document | The question it answers |
|---|---|
| [docs/commands.md](docs/commands.md) | What does each command take, print, return as JSON, and exit with? |
| [docs/sharing-and-sync.md](docs/sharing-and-sync.md) | What happens on another machine when I write, and how do I recover when a write does not publish? |
| [docs/data-model-and-integrity.md](docs/data-model-and-integrity.md) | What is stored, how is state derived, and what can `verify` prove? |
| [docs/portfolio-and-web.md](docs/portfolio-and-web.md) | How do I see several repositories, or browse one in a browser? |
