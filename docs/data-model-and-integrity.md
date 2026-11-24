# Data model and integrity

Before you trust a task board that several people and machines write to, or write a program that reads one, you need two answers. You need to know how the files on disk become the board you read, and you need to know which edits to those files a check would expose. When a check does fail, you also need to know how to put the record right.

Pinax stores every change to an item as an event in an append-only log inside your repository, under `.ergon/`. It computes the current state of every item by reading all the events in a fixed order, and it writes the board and the item pages from that state. `pinax verify` tests the log for edits and tests the pages against the log. This page follows one item through that model, states exactly what `verify` can and cannot prove, and ends with how to repair a hand-edited event. For what Pinax is and how to install it, read the [README](../README.md).

## Vocabulary

- An **event** is one line of JSON in the log that records one change.
- The **envelope** is the set of top-level fields every event carries: `id`, `seq`, `ts`, `actor`, `type`, `payload`, `prev` and `v`.
- The **log** is the set of event files in `.ergon/log/`.
- A **shard** is one file of the log, named after the writer who appends to it.
- A **writer** is the `role@host` name stored in an event's `actor` field, such as `alex@laptop`.
- The **predecessor link** is an event's `prev` field: the id of an earlier event by the same writer in the same shard.
- The **fold** is the computation that reads every event in a fixed order and produces the state.
- The **state** is the result of the fold: every item with its status, claim, gate, priority and decision, together with the edges between items and the policies.
- The **projection** is the board and the item pages, which Pinax writes from the state.
- An **annulment** is an `event.annulled` event that names one earlier event and cancels its effect.
- The **local fold** is the fold over the log in your working tree alone.
- The **union fold** is the fold over your local log together with the events committed on the remote's default branch.
- A **phase** is a named stage that a `phase.opened` event opens. `pinax init` opens the only phase, named `init`.
- The **status setter** is `pinax status <id> <state>`, the form of `pinax status` that sets an item's status. Without those two arguments, `pinax status` shows a summary of the work.

## The files under `.ergon/`

Run `pinax init` in a Git repository and Pinax creates four files:

```
.ergon/.gitattributes
.ergon/.gitignore
.ergon/board.md
.ergon/log/operator-<host>.jsonl
```

It also creates an empty `.ergon/items/` directory, which Git does not show. The shard holds the two events that `init` wrote with your default writer, `operator@<host>`: `ergon.created` with sequence number 0, and `phase.opened` with sequence number 1. `board.md` holds only the line `# Board`.

The file `.ergon/.gitattributes` holds exactly two lines, `*.jsonl text eol=lf merge=union` and `.ergon/** text eol=lf`. The first tells Git to merge log files by keeping the lines of both sides. The second line has no effect. Git reads it relative to `.ergon/`, so it names a path that does not exist, and the projection has LF line endings only because Pinax writes it that way.

The file `.ergon/.gitignore` holds `!/log/*.jsonl`. That line brings shard files back into view when a file rule in your repository, such as `*.jsonl`, would hide them. Git does not look inside a directory that a rule ignores, so the line cannot help when a rule ignores `.ergon/log` itself, and you then remove or narrow that rule in your own `.gitignore`. `pinax init` commits none of these files, and the [README](../README.md) shows the commit you make yourself.

A shard's name is the writer's name with `@`, `/`, `\` and space each replaced by `-`, so `alex@laptop` writes to `alex-laptop.jsonl`. Pinax lower-cases the part of a writer's name after `@` and keeps the role exactly as typed. Before it creates a new shard, Pinax looks for an existing one whose name differs only in upper and lower case, and it appends to that one when it finds it. Two writers can therefore share one shard. That happens for names that differ only in the case of the role, and for names such as `a-b@c` and `a@b-c`, which both map to `a-b-c.jsonl`.

Pinax reads only the files directly in `.ergon/log/` whose names end in `.jsonl`, and it reads no subdirectory. On POSIX systems that match is case-sensitive, and on Windows it ignores case. Each append writes one line ending in LF while holding an exclusive operating-system lock on the file, then flushes the file to disk. When Pinax reads the log, it turns CRLF and CR into LF. It skips a line that is not valid JSON with a logged warning, for example a last line that an interrupted write left incomplete or a corrupted line in the middle. It skips a parsed line that has no `id` in the same way.

## One event, field by field

Next, the writer `alex@laptop` runs `pinax add --title "Write the install guide" --prefix app --actor alex@laptop`. Pinax makes the item id `app-4x56` and appends one line to `alex-laptop.jsonl`:

```
{"id":"3egsz7plfcmay4x3m4fyvk4h2a4rbol56hfoghit5w2yepgenteq","seq":2,"ts":"2026-11-23T14:45:26Z","actor":"alex@laptop","type":"item.created","payload":{"item_id":"app-4x56","title":"Write the install guide","prefix":"app","status":"queued"},"prev":"","v":2}
```

Pinax writes the envelope in this order, with no spaces after separators, and escapes every character outside ASCII.

| Field | What it holds |
|---|---|
| `id` | The event's id, a hash of the other fields (see below). |
| `seq` | An integer. A write command that publishes sets it to one more than the largest `seq` in the union fold, which is why this event has 2 after the two events of `init`. The commands that append without publishing use the local fold alone. They are the status setter, `dispatch --claim`, `registry` (which keeps the list of repositories for the portfolio view), `reconcile` (which imports finished and parked work from a text file) and `doctor --reconcile`. `pinax doctor` diagnoses tracker files left uncommitted and claims gone stale, and `--reconcile` offers to fix them. |
| `ts` | The time in UTC to the whole second, in the form `YYYY-MM-DDTHH:MM:SSZ`, read from the clock of the machine that ran the command. |
| `actor` | The writer, with the part after `@` lower-cased. The write commands refuse a value that is not `role@host`; `init`, the status setter, `dispatch --claim`, `registry`, `reconcile` and `doctor --reconcile` do not check that form. |
| `type` | The event type, such as `item.created`. The reference table below lists every type. |
| `payload` | A JSON object whose fields depend on the type. |
| `prev` | The predecessor link. It is empty here, because this is the first event by `alex@laptop` in this shard. |
| `v` | The format version. Every event Pinax writes is version 2. |

Two clones can write events with the same `seq`, so a sequence number alone does not identify an event, and the id does.

### How the event id is made

For a version 2 event, Pinax takes the fields `seq`, `ts`, `actor`, `type`, `payload`, `prev` and `v`. It writes them as canonical JSON, with keys sorted, ASCII only, and `,` and `:` as separators. It hashes that text with 256-bit BLAKE2b and writes the hash as lower-case base32 with no padding. Because `prev` is inside the hash, changing which event the predecessor link names also changes the id.

Pinax also reads version 1 events, which have no `v` field. The id of a version 1 event covers only `seq`, `ts`, `actor`, `type` and `payload`, and Pinax checks it under that rule, so a log that holds both versions passes `verify`. An event fails its id check when it is a version 2 event carrying an id made by the version 1 rule. An event's envelope is damaged when it carries a `v` that is anything other than the integer 2, when `id` is not a string or is empty, when `ts`, `actor`, `type` or `prev` is missing or not a string, when `seq` is missing, not an integer (a boolean does not count) or larger than 9223372036854775807, or when `payload` is missing or not an object. A line with a damaged envelope, a JSON line that is not an object, and a JSON line that Pinax cannot read because it nests values more than 200 levels deep or holds a number with too many digits cannot be used as an event. `verify` reports each such line, and every other command that reads the log refuses with exit 9, except that `overview` and `status --portfolio` report the repository as damaged. Annulling the event does not change this; the recovery is to restore the line. A line that is not JSON is skipped with a warning, and outside `verify` so is an object without an `id`. An event whose payload holds a field of the wrong type is ignored with a warning. So is an `item.created` whose `item_id` cannot be a file name on every supported system: one that is empty, `.` or `..`, holds `/`, `\`, `:`, `*`, `?`, `"`, `<`, `>`, `|` or a control character, or ends with a space or a dot.

### How the item id is made

The item id `app-4x56` is the prefix, a hyphen, and a suffix. The prefix comes from `--prefix`, which defaults to `pnx`. The suffix is the first four or more characters of a base32 BLAKE2b hash over the sequence number, the title, the writer, the host name and the current time in nanoseconds. Pinax lengthens the suffix until no other id in the local fold shares it.

## How an event links to the one before it

When Pinax writes an event, its predecessor link is the id of the last event in the chosen shard whose `actor` field equals the writer, or an empty string when there is none. Pinax reads it from the local shard alone, even when the sequence number comes from the union fold. When `alex@laptop` adds a second item, `app-usda` with sequence number 3, that event's `prev` is `3egsz7plfcmay4x3m4fyvk4h2a4rbol56hfoghit5w2yepgenteq`. Each writer's events in a shard therefore form a chain.

`verify` checks these links for version 2 events, and it checks less than a strict chain would. It groups the events by shard file and writer. Within a group, every event after the first must name the id of some event in the same group, and that event need not be the one just before it. The first event of a group may name the id of any event in the log. An empty `prev` passes at every position in a chain. A link that names no such id makes `verify` fail.

A rewrite that keeps the old id fails its own id check. A deletion, or a rewrite whose id is recomputed, shows up only through the event after it. `verify` catches it when a later version 2 event, not annulled and not changed, still names the old id.

For version 1 events the link is weaker again. A broken link on a version 1 event produces only a warning, and a version 1 `prev` is outside the id, so an edit to it changes nothing that `verify` checks.

## From events to state: the fold

### The order

The fold sorts every parsed line by `seq`, then `ts`, then `actor`, then `id`, comparing the strings as plain strings. When several lines share an id, it keeps the copy whose canonical form has the smallest bytes. It then applies each event in that order. Afterwards it resolves the edges between items, the registry, warnings about cycles of edges, the claims, the owner-gate classes and the roll-ups of parent items, each explained below.

The fold reads no clock, no random source, no locale and no hash seed. The same log therefore gives the same state on any machine and at any time, whatever the order of its lines, its duplicate lines and its line endings. Folding only the first events of the log, up to some point, gives the state at that point.

The fold never drops an event because of an integrity problem. When an event's content no longer matches its id, the fold applies the edited content and prints a warning such as:

```
Integrity violation: event id=<id> does not match recomputed hash (payload or envelope tampered). seq=2 actor=alex@laptop type=item.created
```

A board printed from that log shows the edited title. `verify` is the command that fails on it. An event that names an item the fold does not know, such as a status change, block, completion, park, priority, decision or claim, is ignored with a warning. An event of a type the fold does not know is kept, and its id is listed in the state under `unknown_events`.

### Reading the state from a tool

If you write a tool that reads the state, `pinax board --json` prints `{"repo": <directory name>, "state": <the whole state>}`, and the [command reference](commands.md) gives the other JSON outputs. Take a tracker with items, a released claim, a `blocks` edge and a note (a reference to a document, attached to an item with `pinax note add`). Its state has the keys `claim_released`, `claim_superseded`, `deps`, `edges`, `ergon`, `items`, `notes`, `phases` and `report`. The `report` key holds `warnings`, the list of the fold's warnings.

Once the log holds at least one claim, `claim_superseded` and `report.warnings` are always present, as empty lists when nothing filled them. A log with no claim has neither key. The one exception is `report.warnings`, which also appears when the log holds a cycle of `parent-child` edges. Other keys, such as `annulled`, `claim_expired`, `registry`, `policy`, `process_policy`, `owner_gate_policy`, `decision_actors_policy`, `note_ref_policy` and `unknown_events`, appear when an event sets them.

`items` maps each item id to an object. An item can carry `id`, `title`, `prefix`, `status`, `created_at`, `created_by`, `event_id`, `status_changed_at`, `status_changed_by`, `owner`, `claimed_at`, `claim_event_id`, `gate`, `owner_gate`, `park_reason`, `briefing`, `priority`, `priority_changed_at`, `priority_changed_by`, `decision` and `rollup`, each only when it has a value. `edges` maps each edge type to a list of `[from, to]` pairs, and `deps` lists the `blocks` pairs again. `phases` maps each phase name to `opened_at`, `opened_by`, `opened_seq` and `status`.

### Statuses

The statuses are `queued`, `ready`, `building`, `blind-verify`, `adjudicate`, `done`, `blocked` and `parked`. `pinax add` creates an item as `queued`. `pinax block` sets `blocked`, `pinax park` and a park decision set `parked`, and `pinax done` sets `done`. The status setter sets any of them. An `item.status_changed` event applies whatever status string it carries, and the last one in fold order wins. A second `item.created` for the same id replaces the item.

No transition is guarded. The status setter moves an item from any status to any other, including back from `done`, and `block`, `park` and `done` apply whatever status the item had before.

The status an item shows can differ from the status stored in the state. A claim never changes the stored status, but a `queued` or `ready` item with a live claim is shown as `building` on the board and on its item page. The summary from `pinax status` lists such an item under `building`, and its row there gives the stored status, for example `[queued; alex@laptop; ...]`.

### Gates and owner gates

`pinax block` records a gate, one of `scope`, `decision`, `destructive` and `proposal`, and an owner-gate class when you give one. When the item leaves `blocked`, the state keeps the old `gate` field and drops the owner-gate class.

The people on the project set the policies with `pinax policy`, and each policy is an event in the log. The process in force is the last well-formed `policy.process_set` event in fold order, or `flow` when there is none. The list of owner-gate classes is the last `policy.owner_gate_classes_set` event, or the built-in list when there is none. The prefixes a note reference may start with, besides being a repository-relative path or a web address, are those of the last `policy.note_ref_prefixes_set` event, or none when there is none. A malformed policy event sets nothing and produces a warning. The [README](../README.md) explains what `flow` and `strict` allow.

### Claims and how competing claims resolve

Between two writers, the claim with the earlier time wins. The time is the claim's own `ts`, so the writers' clocks set the order, and the order in which the claims reached the remote plays no part. The clock check limits how far back a claim can reach. `pinax claim` refuses, with exit code 6, a claim stamped more than five seconds, or the number of seconds in `PINAX_CLOCK_TOLERANCE_S`, behind the newest event on the remote that no annulment cancels. `pinax claim` refuses with exit code 3, and appends nothing, when the local log or the fetched remote already holds a live claim by another writer, whatever the new claim's time. A claim stamped up to five seconds earlier than another can therefore win only when neither writer had seen the other's claim. Of two such claims, one that reaches the remote second and loses ends with exit code 3 and names the winning claim. A claim that reached the remote first exits 0 and shows its loss only on the board after a pull.

The fold applies that rule after it has applied every event. For each item it walks the claims in claim order, which puts the earliest time first. This order differs from the fold order, because time comes first. The first claim is live, and a claim made while another writer's is live is superseded. A claim made while one by the same writer (host compared lower-cased) is live changes nothing: it is not superseded, and the live claim keeps its id, time and expiry. Superseded claims remain in the log as a record, and the state lists them in `claim_superseded` with `item_id`, `superseded_event_id`, `superseded_actor`, `winner_event_id` and `winner_actor`.

A live claim sets the item's `owner`, `claimed_at` and `claim_event_id`. It ends at whichever comes first, in claim order, of two events. One is the first release of that item after the claim. The other is the first applied event of any type after the claim whose timestamp is at or past the claim time plus the expiry hours. When that first event past the deadline is itself a release, the claim counts as released. A decision counts as an ordinary event here, so a decision past the deadline ends the claim as expired.

The expiry hours for a claim come from the last well-formed `policy.claim_expiry_set` event at or before the claim in claim order, or 24 hours when there is none. A policy set later never changes an earlier claim. A log that records nothing after a claim never expires it, so the same shards fold to the same state at any wall-clock time. A claim whose timestamp cannot be read never expires.

A released claim is recorded in `claim_released`, with `item_id`, `claim_event_id`, `claim_actor`, `claimed_at`, `release_event_id`, `release_actor`, `released_at` and `reason`. An expired claim is recorded in `claim_expired`, with the claim fields and `expiry_hours`, `expired_at` and `expiring_event_id`. After a claim ends, the next claim becomes live. A release ends a claim only when its `item_id` and `reason` are both non-empty strings.

There is no lock between machines, so a race is settled by the fold after both claims are in the log. A claim on an unknown item is ignored. A claim recorded after an item is done stays in the state, and the board shows the item as done. What a losing claim sees at the command line is in [sharing and sync](sharing-and-sync.md).

### Dependencies and readiness

An edge between two items is keyed by its type, its source item and its target item. For each key, the last `dep.added` or `dep.removed` in fold order wins, so removing an edge of one type leaves edges of other types between the same items in place. The edge types are `blocks`, `parent-child`, `discovered-from`, `related` and `supersedes`.

`pinax dep add A --blocks B` means that item B cannot be ready until item A is done. An item is ready when four things hold: its status is `queued` or `ready`, it has no live claim, every item that blocks it is done, and it is not on a cycle of `blocks` edges. A blocker the fold does not know counts as not done. The fold warns about cycles and never hangs on one.

Only `blocks` edges affect readiness and the choice of the next item. `parent-child` edges feed the roll-up and the `--under` option of `ready`, `next` and `status`. The other types are recorded and shown on item pages only.

An item that has `parent-child` children carries a `rollup`. It is `done` when every child is done. Otherwise it is `building` when any child is claimed or is in `building`, `blind-verify` or `adjudicate`. Otherwise it is `blocked` when every open child is blocked and at least one child is open. Otherwise it is `queued`. A child that has children of its own contributes its own roll-up, and items on a `parent-child` cycle get none. A roll-up never changes a status or readiness.

### Priorities and the next item

An `item.priority_set` event carries an integer rank, and a lower rank is more urgent. Negative ranks are allowed. The last such event for an item wins, and a rank that is not an integer, including a boolean, is ignored with a warning.

`pinax next` picks one item among the ready ones. It compares these values in turn and takes the item that comes first:

1. whether the item's prefix names a phase, with those items first;
2. whether the item has a priority, with those items first;
3. the priority;
4. the critical-path depth, deepest first;
5. the creation time, the event id and the item id.

The critical-path depth is the longest chain of `blocks` successors that are not done, counted after the items on cycles are removed. Any explicit priority therefore beats any depth.

### Decisions

For each item, the state keeps the latest valid `item.decided` event that is not annulled, as `decision` with `option`, `label`, `effect`, `reason`, `actor`, `ts` and `event_id`. The decision changes nothing else on the item. When you annul the latest decision, the previous one shows again, or none.

A decision with the effect `release` is one event, and the item's status, gate and owner-gate class stay as they were. A decision releases no claim, but like any event it expires a live claim when its time is at or past the claim's deadline. A decision with the effect `park` is two events in the same shard and the same commit: `item.decided`, then `item.parked` with the same reason. The parked event has a `seq` one higher, the same timestamp, and a `prev` equal to the decision's id. The fold then shows the item as `parked` with the reason as its `park_reason`, exactly as `pinax park` would, and keeps the old `gate`. A park decision releases no claim either, and expires one under the same deadline rule.

### Annulled events

An annulment is valid when its own id is valid, its type is `event.annulled`, its `target_id` is a non-empty string different from its own id, and its `reason` is a non-empty string. An annulment is in effect when it is valid and no annulment in effect names it. An annulment in effect cancels the effect of its target, wherever the annulment sits in the order. It also takes its target out of the integrity checks: unless the target is itself an annulment, which must always be valid, `verify` does not check the target's id, and the fold prints no integrity warning about it. The target's line stays in the log.

An annulment that another annulment in effect names is not in effect and does nothing: its target takes part in the fold again, and the integrity checks apply to it again. So when you annul the only `item.created` event of an item, the item leaves the board and its page is deleted. Running `pinax annul` on that annulment's own id brings the item, its board row and its page back, with the status and claims its other events give it. The rule holds at any depth: a third annulment, naming the second, puts the first in effect again. When several annulments name one event, it stays cancelled while any of them is in effect. Annulments that name each other in a cycle, which valid ids rule out, would not be in effect.

A restored event takes part by the ordinary rules, as if the annulment had never existed, and replaces nothing. If you added the item again under another id in the meantime, both items are on the board. Because the fold reads the whole log, a log written by an earlier version that already holds an annulment of an annulment now folds with the target restored.

`pinax annul` refuses one kind of target: the annulment a rejected `claim` writes for its own event, or that Pinax writes for a waiting claim, because restoring it would bring back a claim that was never published. Deleting an annulment line by hand is an edit to the log, and `verify` catches it only when a later event by the same writer names the annulment.

`pinax annul` on an event that is already annulled appends a second annulment, and two annulments have the same effect as one. `pinax annul` accepts an empty `--reason`, and the annulment it then writes is invalid. An invalid annulment cancels nothing and makes `verify` fail.

### Notes and other events

A `note.added` event is stored in the state under `notes` with `event_id`, `item_id`, `ref` (the document reference), `ts`, `actor` and an optional `caption`. Notes change no status. You can read them in `pinax board --json` and on the history page of `pinax web`. The board, the item pages, the `pinax status` summary and `pinax report` (a summary of shipped, parked and blocked items) do not show them. Registry events are described in [portfolio and web](portfolio-and-web.md).

## From state to the board and item pages

After every command that appends an event, Pinax rewrites `.ergon/board.md` and the pages in `.ergon/items/` from the state. `pinax registry add` and `pinax registry rm` are the exceptions. The files have LF line endings and no timestamps, so a second rewrite from the same log gives the same bytes. A character that UTF-8 cannot hold is written as U+FFFD. The rewrite deletes the page of any item that is no longer in the state.

### The board

`board.md` starts with `# Board` and a blank line. Each lane that has rows follows as `## <heading>`, a blank line, its rows and a blank line, and the file ends with a single LF. The lanes come in this order: `## Queued`, `## Ready`, `## Building`, `## Blind verify`, `## Adjudicate`, `## Done`, then one section for each other status string, sorted. Then come `## Parked / needs human` and `## Claim conflicts (resolved)`.

An item is placed by the status it shows, so a claimed `queued` item appears under `## Building`. A row in a status section reads:

```
- <id> · <title> · <status>[ [next]][ · rollup:<bucket>] · <owner> · <blockers>
```

The fields are separated by a middle dot (U+00B7), and an empty field holds an em dash character (U+2014). `[next]` marks the one item that `pinax next` would pick. The owner is the live claim's owner, otherwise the item's creator; an unclaimed item that `alex@laptop` created shows `alex@laptop`. The blockers field lists every item with a `blocks` edge to this one, done or not, sorted and joined by `, `. Rows put the items whose prefix names a phase first, then follow the status sections, the next item first, then creation time, event id and item id.

Under `## Parked / needs human`, blocked items come first, each as `- <id> · <title> · gate=<gate>`. Under `strict`, a recorded owner-gate class adds ` · owner gate: <class>`. Parked items follow as `- <id> · <title> · <reason>`, for example `- app-m3xd · Review the pricing page · Waiting for the designer`. Each row under `## Claim conflicts (resolved)` stands for one superseded claim, for example `- app-4x56: sam@desktop superseded by alex@laptop`. The board shows no priority, decision or note.

### The item pages

Each page `items/<id>.md` opens with frontmatter holding `id`, `title`, `phase` (despite its name, this field holds the item's prefix), `owner` and `status` (the shown status). The frontmatter also holds `rollup` when there is one, `gate` when there is one, `owner_gate` under `strict` only, and `deps` (the list of blockers). The page's `owner` is the item's claim owner, and it stays empty for an item nobody has claimed, while the board shows the creator in that case. Here is the page of an unclaimed item that `app-vqkc` blocks and is related to:

```
---
id: app-swlg
title: Update the screenshots
phase: app
owner: 
status: queued
deps: [app-vqkc]
---

## Briefing

_(no briefing yet)_

## Edges

### blocks
- from: app-vqkc

### related
- from: app-vqkc

## Audit trail

_(no audit results yet)_
```

The sections always come in this order. `## Briefing` holds the text stored by `pinax done`. `## Decision` appears only when the item has one. `## Edges` has one `###` heading per edge type, or `_(no typed edges yet)_`. `## Audit trail` closes the page; no command in Pinax records audit results, so it always reads `_(no audit results yet)_`. The decision section holds one line:

```
- option <option> (<label>), effect <effect>, by <actor> at <ts>: <reason>
```

## What `verify` checks

`pinax verify` runs its checks in this order:

1. It reads every parsed line, before duplicates are merged. It reports every line that cannot be used as an event, with its file and line number, even when an annulment names its id. Each other event that is not an annulment, and that no annulment in effect names, must pass its id check. Each annulment must be valid. A line without an id fails.
2. When step 1 passes, it checks the predecessor links of version 2 events, as described above.
3. It renders the board and every item page from the log in memory, writing nothing, and compares them with the files in your working tree, treating CRLF as LF. It reports each missing, changed and stale file.
4. It asks `git check-ignore` whether a new shard in `.ergon/log/` would be ignored, and fails when it would.

When everything passes, `verify` prints `pinax verify: OK - projection matches log.` and exits 0. It also exits 0 when `.ergon/log/` does not exist, after printing `pinax verify: .ergon/log/ not found - nothing to verify.` on standard error. Any failure exits 1.

When you edit an event by hand, `verify` prints this on standard error. It names each failing event by id, sequence number, writer, type, file and line number:

```
pinax verify: EVENT LOG INTEGRITY FAILURE - 1 non-annulled event(s) no longer match their stored content hash or name a predecessor missing from their chain.
  Invalid events:
    id=3egsz7plfcmay4x3m4fyvk4h2a4rbol56hfoghit5w2yepgenteq seq=2 actor=alex@laptop type=item.created at .ergon/log/alex-laptop.jsonl line 1

  The append-only event log was modified after these events were written.
  'pinax verify --fix' only regenerates projections and will not rewrite
  event history. Restore the exact original event bytes. A valid ordinary
  event may be formally annulled before replacement; a malformed annulment
  record itself must be restored from known-good history.
```

When you edit the board by hand, it starts like this on standard error, and goes on to suggest `pinax verify --fix`:

```
pinax verify: DRIFT DETECTED - projection differs from log.
  Drifted files:
    .ergon/board.md
```

When an ignore rule hides the log from Git, it prints this on standard error. The name `pinax-doctor-probe.jsonl` is a test path that `verify` asks Git about, and no such file is created:

```
pinax verify: LOG SWALLOWED BY GITIGNORE - .ergon/log/pinax-doctor-probe.jsonl would be git-ignored right now.
  A .gitignore rule is shadowing the event log shard directory: new shard files
  will be silently local-only, never committed, invisible across worktrees/
  branches because Git ignores it. Run 'pinax init' again to (re)install the
  .ergon/.gitignore negation, or fix your repo's .gitignore.
```

### The pre-commit hook

`pinax init` installs a pre-commit hook that runs `verify` before every commit, so a `git commit` of a hand-edited board prints the drift message and is refused. The hook runs `verify` on the files in your working tree, which can differ from what you have staged.

The hook lives in each clone's own Git directory, so a fresh clone has none until you run `pinax init` in it. In a clone of an existing tracker, `pinax init` installs the hook, prints `pinax: .ergon already initialised (idempotent - skipped event emit).` and writes no event. The hook refuses commits while an edited event or a broken link is in the log. A clone without it can still write and publish new events onto such a log. A damaged line refuses every write without the hook. Run `pinax init` once in every clone.

## What `verify` catches

The id check and the predecessor links are the only defences, because nothing signs an event. This table lists every kind of change to the record and whether `verify` catches it.

| Change to the record | Caught | Reason |
|---|---|---|
| An edit to any field inside the id of an event that no annulment in effect names, leaving the old id in place | Yes | The id no longer matches the event. |
| An edit to `prev` of such a version 2 event, leaving the old id in place | Yes | `prev` is inside a version 2 id. |
| A second line with the id of an existing event and different content | Yes | Every line is checked before duplicates are merged. |
| A `v` other than the integer 2, a version 1 id on a version 2 event, or an envelope field that is removed or of the wrong type | Yes | `verify` exits 1 and names the file and line. A version 1 id on a version 2 event fails the id check; the others leave a line that cannot be used as an event. |
| A line that parses as JSON and is not an object | Yes | The line cannot be used as an event, and `verify` names its file, line number and reason. |
| A line that parses as JSON and has no `id` | Yes | A line without an id fails, and `verify` names its file and line number. |
| Deleting a version 2 event that a later version 2 event, unchanged and not annulled, names | Yes | The later event's link names an id that is gone. This holds for the oldest event too, and when the later events have been moved into another shard file. |
| Editing a version 2 event, or pointing its `prev` elsewhere, and recomputing its id, when a later version 2 event, unchanged and not annulled, names it | Yes | The later event's link names the old id. |
| An invalid annulment, including one that names itself or has an empty reason | Yes | Every annulment must be valid. |
| A hand edit to `board.md` or an item page, a missing page, or a stale page | Yes | The projection is compared with a fresh rendering. |
| A `.gitignore` rule that would hide new shards from Git | Yes | `verify` asks Git directly. `--fix` cannot repair this. |
| Any edit to an annulled event, or replacing it with any line that keeps its id | No | `verify` skips the id check of an event that an annulment in effect names. |
| Deleting or recomputing the newest event of a chain | No | No later event names it. |
| Deleting or recomputing the last event on each side of a merge | No | No later event names either one. |
| Deleting an event whose successor is annulled | No | The annulled successor is out of the integrity checks, so nothing reports its link. |
| Rewriting an event and recomputing every event after it | No | The rewritten chain is consistent with itself. |
| Deleting or recomputing an event that a version 1 event follows | No | A broken link on a version 1 event only warns. |
| An edit to `prev` of a version 1 event | No | A version 1 `prev` is outside the id, and its links only warn. |
| Blanking `prev` of a version 2 event later in its chain and recomputing its id | No | An empty `prev` passes at every position. |
| Pointing `prev` of a version 2 event at another event of its group and recomputing its id, when no later event names it | No | A link need only name some id in its group. |
| Adding a field that the id does not cover | No | The id stays the same, and the state does not change. |
| Adding a forged event with a correct id | No | Nothing signs an event, so anyone who can write the file can compute a correct id. |
| Sequence numbers that go backwards | No | Nothing checks that `seq` grows. |
| Two events that name the same predecessor (a fork) | No | Each link names a real event. |
| A line that is not valid JSON, such as a corrupted middle line | Only through its effects | The reader skips the line with a warning, and the skip itself does not fail. `verify` fails when a later event's link named the lost event, or when losing it changes the state so the projection on disk drifts. |

For every change in the "No" rows, Git history is the only other record. Each commit records an author and a committer, with times, as the person who made the commit declared them, and `verify` reads none of it.

## What `verify --fix` does

`pinax verify --fix` rewrites the board and the item pages from the log, but only when the log passes steps 1 and 2. On drift it names the drifted files on standard error with `pinax verify: DRIFT DETECTED - regenerating projection (--fix)...`, rewrites them, checks again, and prints `pinax verify --fix: OK - projection regenerated and matches log.` on standard output. It deletes stale item pages as part of the rewrite. It does not stage or commit the repaired files. A hand edit you had staged stays staged, so run `git add` on the repaired files before you commit them.

`verify --fix` never changes an event. When the log fails step 1 or 2, it prints the same integrity failure as `verify`, exits 1 and leaves the projection alone. It cannot repair a log that Git would ignore either.

Drift also follows a `git pull --no-rebase` when your clone and the remote both changed the tracker. Git merges the log files line by line, but it stops with a conflict in `.ergon/board.md`. Finish the merge from the log:

```
pinax verify --fix
git add .ergon
git commit -m "Rebuild the Pinax pages after a merge"
git push
```

## Repairing a hand-edited event

When `pinax verify` reports `EVENT LOG INTEGRITY FAILURE`, the repair is to put back the event's original bytes from Git. `verify --fix` cannot do it, because it never changes an event.

### Find out which state the edit is in

The failure message names the event, and its `actor` tells you the shard: `actor=alex@laptop` means `.ergon/log/alex-laptop.jsonl`. Then run:

```
git status --short .ergon
```

- A line ` M .ergon/log/alex-laptop.jsonl`, with the `M` in the second column, means the edit is in your working tree and not staged.
- A line `M  .ergon/log/alex-laptop.jsonl`, with the `M` in the first column, means the edit is staged. A write command that the pre-commit hook refused with exit code 7 leaves the edited shard staged in this way, together with its own new event.
- No line for the shard, while `verify` still fails, means the edit is already committed. That happens when the clone had no hook, or the hook could not find Pinax and let the commit through.

### The edit is not staged

Note the events in the shard that no commit holds yet, with `pinax doctor` section `[1] uncommitted shard events`, because restoring the shard discards them, then restore the shard from the staged copy and check again:

```
git checkout -- .ergon/log/alex-laptop.jsonl
pinax verify
```

### The edit is staged

`git checkout -- <shard>` restores the staged copy here, so `verify` still fails. Restore the whole tracker folder from the last commit:

```
git restore --source=HEAD --staged --worktree .ergon
pinax verify
```

This command discards every uncommitted change under `.ergon/`, including the event of the refused write. Run that refused Pinax command again, such as the `pinax add` that ended with exit code 7, once `verify` passes.

### The edit is committed

Find the commit that made the edit, then undo exactly that commit with `git revert`, which adds a new commit and removes nothing else:

```
git log --oneline -- .ergon/log/alex-laptop.jsonl
git revert --no-edit <the commit that made the edit>
pinax verify
git push
```

Every event committed after the edit, by you or by anyone else, stays in the log. When the push is refused because a colleague pushed in the meantime, run `git pull --no-rebase`, and when `verify` then reports drift, run `pinax verify --fix`, `git add .ergon`, a commit and the push again.

### What your colleagues do

Once the fix is pushed, every colleague pulls and checks:

```
git pull --no-rebase
pinax verify
```

A colleague whose own write was refused while the edit was on the remote still holds that event, uncommitted. After the pull, their `verify` reports drift. They finish with `pinax verify --fix`, `git add .ergon`, a commit and `git push`, and their event is kept.

### Annulling the edited event

The failure message also says that a valid event "may be formally annulled before replacement". `pinax annul` accepts the id of the edited event, and `verify` skips the id check of an annulled event, so annulling it and making the change again with an ordinary command also makes `verify` pass. The replacement is a new event, though, and for an `item.created` it is a new item with a new id, while the annulled event comes back only when you annul its annulment, and then `verify` checks its id again and fails. Restoring the original bytes is the repair, and annulment is for an event that is valid but wrong.

## Looking back with replay

`pinax replay --at REF` folds the shards as they were committed at REF, which can be a branch, a tag or a commit:

```
pinax replay --at HEAD~3

  items (4):
    app-m3xd                  done          Review the pricing page
    app-swlg                  queued        Update the screenshots
    app-4x56                  done          Write the install guide
    app-vqkc                  queued        Fix the footer links

  next (as of this ref):
    app-vqkc  Fix the footer links
```

With `--json` it prints `{"at": REF, "state": <the state>}`. A REF that does not resolve prints `pinax replay: not a valid git ref in this repo: '<REF>'` and exits 1, and a REF from before `init` folds to an empty state. Replay writes nothing.

## Event types

Every event type Pinax writes, with its payload fields and the commands that write it. A field followed by `?` is optional.

| Type | Payload fields | Written by |
|---|---|---|
| `ergon.created` | `repo` (the directory name) | `init` |
| `phase.opened` | `phase` (always `init`) | `init` |
| `item.created` | `item_id`, `title`, `prefix`, `status` | `add` |
| `item.status_changed` | `item_id`, `status` | the status setter |
| `item.claimed` | `item_id`; `dispatch --claim` adds `actor` | `claim`, `dispatch --claim` |
| `item.claim_released` | `item_id`, `reason` | `release`, `doctor --reconcile` |
| `item.blocked` | `item_id`, `gate`, `owner_gate?` | `block` |
| `item.completed` | `item_id`, `briefing?`; an import carries `caption?` and the import fields below | `done`, `reconcile`, `doctor --reconcile` |
| `item.parked` | `item_id`, `reason`; an import adds the import fields below | `park`, `decide --effect park`, `reconcile`, `doctor --reconcile` |
| `item.priority_set` | `item_id`, `priority` | `priority` |
| `item.decided` | `item_id`, `option`, `label`, `effect`, `reason` | `decide` |
| `dep.added` | `from_id`, `to_id`, `type` | `dep add` |
| `dep.removed` | `from_id`, `to_id`, `type` | `dep rm` |
| `note.added` | `item_id`, `ref`, `caption?` | `note add` |
| `registry.repo_added` | `repo_id`, `path?`, `url?` | `registry add` |
| `registry.repo_removed` | `repo_id` | `registry rm` |
| `policy.claim_expiry_set` | `hours` | `policy claim-expiry` |
| `policy.process_set` | `process` | `policy process` |
| `policy.owner_gate_classes_set` | `classes` | `policy owner-gates` |
| `policy.decision_actors_set` | `actors` | `policy decision-actors` |
| `policy.note_ref_prefixes_set` | `prefixes` | `policy note-refs` |
| `event.annulled` | `target_id`, `reason` | `annul`, and `claim` after its push is rejected |

`reconcile` imports from an action file, `BACKLOG-OFFLINE.md` at the repository root by default, in which each line records a finished or parked item. An event that `reconcile` imports keeps the writer and timestamp from its line and adds `imported_by`, `source`, `source_line_hash` and `imported_at`. The board and the item pages do not show the `caption` of an imported completion, and the history page of `pinax web` prints it with the rest of the payload. The `policy.*` events and the registry events name no item. The [command reference](commands.md) gives each command's options and output.

## Limits

Nothing signs an event, and nothing anchors the log outside the repository. A writer with access to the repository can add correctly hashed events or delete the newest ones, and `verify` will pass. Git history is the only other record of those changes, and it holds what each committer declared.

`verify` reads the files in your working tree and ignores the staged index, so it checks the files as they are on disk at the moment it runs. It skips a line that is not valid JSON with a warning, and it fails over that line only when the loss shows up in a link or in the projection.

The fold only warns. Every command that reads state, from `board` to `next`, uses an event with a wrong id or a broken link as if it were sound, and only `verify` turns those problems into a failure.

Sequence numbers are not unique across clones, and nothing checks that they grow. Item ids are unique only among the ids in the local fold when they are made. Two writers can share a shard, so a shard name does not identify one writer; the `actor` field does.

Version 1 events carry weaker protection, because their `prev` is outside the id and their broken links only warn. A log that holds both versions passes `verify`, so any version 1 events in it keep that weaker protection.

Some inputs are rendered or counted, but nothing in Pinax writes them. `pinax init` opens the only phase, so the phase rule in `next` and on the board matters only for items whose prefix is `init`. Every item page ends with an empty `## Audit trail`, and `pinax metrics`, which prints counts over the log, counts `item.audit_result` events that no command writes.

An annulment leaves its target's line in the log, so anyone who reads the files can still see what was annulled.
