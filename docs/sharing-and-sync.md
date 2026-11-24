# Sharing and synchronisation

A tracked repository rarely lives in one place. You work in a clone on your laptop, a colleague works in a clone on their desktop, and a coding assistant may work in a third clone, and all of you write to the same tracker. Each of you writes on your own machine, and nobody sees anybody else's writes until Git carries them across.

Pinax carries writes across through Git alone. Most write commands check what the shared remote already holds, write their event, commit it and push it, so another clone receives it with its next pull. A few commands write events without committing or pushing them; [commands.md](commands.md) says which, and [a section below](#events-from-commands-that-never-push) explains what that means for other clones. This guide follows one write from your clone to another. It then takes each way a write can stay behind, with what you see and what to do, and ends with what to do when two machines show different owners for one item. The terms event, log, board, item page, projection and writer are explained in the [README](../README.md).

## One write, from your clone to another

Follow one case. You are the writer `alex@laptop`, in a clone that has the remote's default branch `main` checked out, and you add an item:

```
pinax add --title "Write the install guide" --prefix app --actor alex@laptop --json
```

Pinax prints one JSON object that includes `"item_id": "app-4x56"`, `"published": true` and `"sync_status": "pushed"`. The sync status is the field that names how the write ended, and `pushed` means the remote accepted it. The publish sequence is the series of steps a committing write command runs to reach that result, and the stages below are its steps in order.

**The checks before anything is written.** Pinax first checks the writer. It must be written `role@host`, with exactly one `@`, or the command exits 2 with the sync status `invalid_actor` and prints, for a writer given as `alex`:

```
pinax: the actor 'alex' is not a valid handle - every mutating command requires role@host (for example builder@laptop), so the event log can tell every writer apart across machines; no item was created.
```

It then checks that the folder holding `.ergon/` is the top folder of its Git repository. When that folder is a subfolder of another repository, for example, the command exits 4 with `foreign_repository`. Nothing is appended in either case.

**The fetch.** Pinax fetches the remote, which is always the one named `origin`. It takes the remote's default branch from `origin/HEAD` when that is set, and otherwise uses `main`, then `master`, then the first remote branch in alphabetical order. A remote with no branch at all gives Pinax nothing to compare against, and the write ends with the sync status `no_default_branch`, described [below](#exit-0-with-a-note-committed-on-your-machine-not-pushed). A branch that exists but whose log files cannot be read stops the command with exit 4 and `remote_unreadable`, and nothing is appended.

**The union fold.** A fold is the state Pinax works out by reading events in order; [data-model-and-integrity.md](data-model-and-integrity.md) gives the rules. The union fold is the fold of the log in your working tree together with the log files committed on the remote's default branch. Pinax refuses your write when the item it names exists on neither side (exit 1, `unknown_target`). It applies the project's policy from the same union, so a strict process that a colleague pushed binds your write before you have pulled it. A claim also passes a check on your clock, described under [exit 6](#exit-6-your-clock-is-behind-the-remote).

**The append.** Your shard is the file in `.ergon/log/` that holds only your events, named after you: `alex@laptop` writes to `alex-laptop.jsonl`. Pinax appends the event to it as one line; `pinax decide --effect park` is the one write that commits two events, the decision and the park, in the same commit. The event's sequence number is one more than the largest in the union fold, and its time is your machine's clock in UTC, to the second. Two clones can write the same sequence number, and that is expected: Pinax orders events by sequence number, then time, then writer, then id, so every clone puts them in the same order.

**The commit.** Pinax rebuilds the board and item pages from the log and commits only your shard and those pages, with the subject `pinax: item.created app-4x56`. Anything else you had staged stays staged and stays out of that commit. Your Git hooks run as they would for any commit, and Pinax never skips them. During that commit it sets the environment variable `PINAX_ROOT` to the repository root; `PINAX_ROOT`, like the option `--root`, pins the repository a Pinax command must act on, and a command whose repository differs refuses with exit 1.

**The push.** Pinax pushes only when the branch you have checked out is the remote's default branch, with `git push origin main:refs/heads/main`. When the remote has moved on and refuses the push, Pinax runs `git fetch origin main` and `git merge --no-edit FETCH_HEAD`, rebuilds the pages from the merged log, and tries again. Pinax makes up to three attempts in total, a fixed number that no option changes. When the rebuild changes the pages, it commits them, so after a write made while your colleague had pushed first, your history reads:

```
7f18c44 pinax: regenerate the projection from the merged main
450f7b8 pinax: item.created app-4x56
```

Pinax never rebases, forces a push, resets, or deletes a branch.

**The result.** After an accepted push, Pinax fetches once more and folds again. Read `published` to know whether your write left your machine, because several outcomes that keep the event local also exit 0.

**The other clone.** Your colleague, `sam@desktop`, clones the remote or runs `git pull`, and `pinax board` in that clone shows your item at once. Git merges the log files with the union rule, which keeps the lines of both sides. The rule lives in `.ergon/.gitattributes` as `*.jsonl text eol=lf merge=union`, and it reaches other clones only once you have committed that file, which is why the README has you commit `.ergon/` by hand after `pinax init`. Each writer has a separate shard, so two writers seldom change the same file. A line that a merge duplicates counts once. Sam's Pinax sorts the merged lines and arrives at the same state as yours, whatever the order of the lines and on any operating system. After a pull, run `pinax verify`; it prints `pinax verify: OK - projection matches log.` when the committed pages match the merged log.

**Every clone needs the hook.** A fresh clone has no pre-commit hook, because Git does not copy hooks, and a clone without it can write and publish onto a log that fails `pinax verify`. Run `pinax init` once in every clone you make. In a clone of an existing tracker it prints `pinax: .ergon already initialised (idempotent - skipped event emit).`, writes no event, and installs the hook described under [the hooks](#the-hooks).

**When both sides changed the tracker.** When you have a local tracker commit and the remote has another, a plain `git pull --no-rebase` stops with `CONFLICT (content): Merge conflict in .ergon/board.md`, and `git status --short` shows `UU .ergon/board.md`. The log lines merge without conflict; only the generated page conflicts. Rebuild the pages, conclude the merge and push:

```
pinax verify --fix
git add .ergon
git commit -m "Rebuild the Pinax pages after a merge"
git push
```

The hook passes that commit, and the other clone then pulls and verifies cleanly. A Pinax write command in the same situation needs none of this: it fetches, merges and rebuilds the pages itself, and the merge has no conflict.

## When a write does not publish

Each case below is named by what you see. Pinax prints its notes on standard error. When the publish sequence ends with a code other than 0, Pinax also prints its report as one JSON object on standard output, with or without `--json`; the report carries `published`, `sync_status` and the fields named below.

### Exit 0 with a note: committed on your machine, not pushed

Every committing write command except `claim` keeps going when it cannot push. It appends the event, rebuilds the pages, commits where it can, prints one of these notes, and exits 0 with `published: false`.

| The note on standard error | `sync_status` |
|---|---|
| `pinax: origin could not be reached; the event is recorded locally and not published.` | `remote_unreachable` |
| `pinax: offline mode was requested (--offline or PINAX_OFFLINE=1); the event is recorded locally and not published.` | `committed_local` |
| `pinax: origin was reached and publishes no default branch; the event is recorded locally and not published.` | `no_default_branch` |
| `pinax: the push was refused because feature is not the origin default branch main; the event is committed locally and not published.` | `committed_local` |
| `pinax: <root> is not a git repository; the event is recorded locally and not published.` | `not_a_git_repository` |

The first note also appears in a repository with no remote named `origin` at all. The fourth names your branch and the default branch, here `feature` and `main`. In the first three cases Pinax checked your write against your local log alone, so a claim or a policy that exists only on the remote did not stop it. After you publish, pull and run `pinax board` to see the combined state.

**When `origin` could not be reached, or you asked for offline mode**, your event is an ordinary commit on your default branch. Once `origin` can be reached, publish it and check the pages:

```
git pull --no-rebase
git push
pinax verify
```

When the remote has not moved in the meantime, `git pull --no-rebase` reports `Already up to date.`, `git push` pushes the waiting commits, and `pinax verify` prints `pinax verify: OK - projection matches log.` When the remote has moved and the pull stops on `.ergon/board.md`, finish it with the steps under "When both sides changed the tracker" in [One write, from your clone to another](#one-write-from-your-clone-to-another). In the other clone, `git pull --no-rebase` then brings the events, and `pinax verify` passes.

**When the repository has no `origin` yet, and the new remote has no commits**, add the remote and push your default branch to it once:

```
git remote add origin <url>
git push -u origin main
```

Between those two commands a write reports `no_default_branch`. `git push -u origin main` carries every commit made before it, including the events written without a remote, and the writes after it report `pushed`.

**When `origin` publishes no default branch**, push your default branch to it with `git push -u origin main`, as above. The push publishes the waiting events, and later writes and claims publish as normal.

**When another branch is checked out**, the event is committed on that branch and nowhere else. The read commands `pinax board`, `pinax report` and `pinax ready` fold only your working tree, never the remote, so on the default branch they leave the branch's events out and say so:

```
pinax: WARNING - 1 unmerged branch carries tracker events not in this view:
         feature (+3 events)
       This fold is branch-scoped: it reflects only the current branch's committed .ergon log.
```

`pinax status` carries its own warning about the branch in its warnings. Pass `--all-branches` to `pinax board`, `pinax report`, `pinax ready` or `pinax status` to include those events; the first three mark each item that exists only on a branch with `[from: <branch>]`, and `pinax status` lists the branches it read in `source_branches`. When the branch merges into the default branch as a fast-forward, publish its events like this:

```
git checkout main
git merge --no-edit feature
pinax verify
git push
```

The other clone then receives the events with `git pull --no-rebase`, and its `pinax verify` passes.

**When the directory is not a Git repository**, the event is appended, the pages are rebuilt, `pinax verify` passes, and nothing is committed.

### `claim` exits 4: the claim could not be published

A claim only counts once other clones can see it, so `claim` has no offline mode and ignores `PINAX_OFFLINE`. Where any other write would commit on your machine only and exit 0, `claim` exits 4 and prints its report on standard output. A claim whose push the remote rejects ends with [exit 5](#exit-5-the-push-was-rejected).

When `origin` cannot be reached, publishes no default branch, or there is no Git repository, nothing is appended and your log is unchanged. Standard error reads, for each of the first two:

```
pinax: origin could not be reached, so nothing was appended.
pinax: origin was reached and publishes no default branch, so nothing was appended.
```

The report's `git_output` field holds the reason, such as `fatal: 'origin' does not appear to be a git repository` or `no branch is published on origin`. Once `origin` exists and publishes the default branch, run the claim again; it prints `pinax: claimed item app-4x56 by alex@laptop in <root>/.ergon` and exits 0. That holds for an item written before the repository had any remote, too.

When another branch is checked out, the claim is appended and committed on that branch, and standard error carries the note about the default branch. The report has `"committed": true`, `"pushed": false`, `"head_branch": "feature"` and the message `the sync publishes only main and feature is checked out`. The claim holds the item in that branch's history and nowhere else, so clones on the default branch do not see it.

Two other cases end any write command with exit 4 before anything is appended. The first is `remote_unreadable`, when the remote's default branch exists but its log files cannot be read. The second is `foreign_repository`, when the folder holding `.ergon/` is not the top folder of its Git repository.

### Exit 5: the push was rejected

Pinax tried to push, the remote refused, and the retries ran out. Standard error ends with:

```
pinax: the push to <branch> was rejected after <n> attempts.
```

The number of attempts is at most 3. The retries end early when a fetch fails, when Git refuses to start the merge (for example because other work you had staged blocks it), or when the merge stops on a conflict outside the board and item pages. The report has `sync_status` `push_rejected`, the message `the push to <branch> was rejected and the event is not published`, and the fields `attempts`, `remote_head` and `git_output`. Read `git_output` for the reason the remote gave.

A rejected claim cancels itself. An annulment is an event that cancels an earlier event: the cancelled event stays in the log and no longer counts. Before the line above, Pinax prints:

```
pinax: event <id> was annulled in <shard> so an unpublished record is never carried forward.
```

The annulment is written to your shard and left uncommitted. Every other write command keeps its event committed on your machine and cancels nothing.

A merge can be left in progress, and you must end it before anything else. When the merge stopped on a conflict outside the board and item pages, Pinax neither concludes nor aborts it, so `git status` shows a merge in progress. In that state Git refuses `git pull --no-rebase`, `git stash push --staged` cannot run while paths are unmerged, and a write such as `pinax add` appends its event and then ends with [exit 7](#exit-7-the-commit-was-refused). Resolve the conflict and commit the merge, or run `git merge --abort`, before you follow the steps below.

To recover, first move any other work you had staged out of the way: commit it, or run `git stash push --staged`. Do not use a plain `git stash` here: it stashes every uncommitted change, and that includes the annulment the rejected claim left in your shard. Then:

- After a rejected `claim`, run the same `pinax claim` again.
- After a rejected `decide`, run the same `pinax decide` again. It finds your decision already committed, appends nothing, and pushes it.
- After any other write command, run `git pull --no-rebase`. When it stops on the board or the item pages, rebuild them and finish the merge with `pinax verify --fix`, `git add .ergon` and `git commit -m "Rebuild the Pinax pages after a merge"`. Then run `git push`.

### Exit 3: the item is already claimed

An item has one owner at a time. A live claim is the claim that holds an item now: it has not lost to an earlier claim, and it has not been released or expired. When the local log or the fetched remote already holds a live claim by another writer, `pinax claim` exits 3 with `sync_status` `claim_held`. It appends no claim, and commits and pushes nothing, whatever your claim's timestamp. The report's `message` reads, for example, `item app-4x56 is already claimed by alex@laptop since 2026-11-23T09:00:00Z, so nothing was appended`, and `holder_actor`, `holder_event_id` and `claimed_at` name the claim that holds the item.

When your published claim already holds the item, `pinax claim` appends no second claim and exits 0 with `already_claimed`. If your claim is not yet published, for example after `pinax dispatch --claim`, `pinax claim` does not publish it, because its time can be hours old. It annuls that waiting claim and then decides as for a new claim, with a new time. When another writer holds the item, it exits 3, the annulment stays in your shard, uncommitted, and the report's `annulled` names the claim.

Choose another item, for example the one `pinax next` names.

Pinax has no lock between machines, so when two writers claim an item before either has seen the other's claim, both claims are recorded. A superseded claim is one that lost to an earlier claim. The log settles the race afterwards. Claims for one item are compared by their own rule, timestamp first, then writer, then id, and the earlier one wins. When your claim reaches the remote second and loses, the command exits 3 with `claim_superseded`, and `winner_actor` in the report names the owner. The `message` then reads `the claim on app-4x56 is superseded by an earlier claim from alex@laptop, so the item is not owned by this actor`. Standard error adds the winning event's id and says this claim stays in the log, published and superseded. A claim that exited 0 can therefore lose later, when a claim with an earlier timestamp arrives from another clone. Your next command that reads the log then shows the winner as owner, and the board lists your claim under `## Claim conflicts (resolved)`, for example `- app-4x56: sam@desktop superseded by alex@laptop`.

Only the claim that lost prints a line that starts `pinax: claim.superseded:` on standard error. Later write commands do not repeat it; the board lists the conflict.

### Exit 6: your clock is behind the remote

Claims are ordered by timestamp, so `claim` refuses to write one whose time is behind the newest event on the remote by more than the tolerance, which is 5 seconds unless `PINAX_CLOCK_TOLERANCE_S` sets another number of seconds. The command exits 6 with `clock_behind_remote`, or with `unreadable_timestamp` when the claim's own timestamp cannot be read in the form `YYYY-MM-DDTHH:MM:SSZ` (the report then carries only `intended_ts`), and no claim is appended. When the command had found a waiting claim of yours to replace, its annulment can be left in your shard, uncommitted. For `clock_behind_remote` the report gives your time in `intended_ts`, the remote's newest in `remote_newest_ts`, the gap in `behind_s`, the tolerance in `tolerance_s`, and the event that set the newest time in `offending_event_id` and `offending_actor`. Only `claim` runs this check.

### Exit 7: the commit was refused

The event was appended and the pages were rebuilt. Pinax then ran `git add` on your shard and the pages, and either Git could not stage them (`stage_failed`) or the commit was refused (`commit_refused`). Nothing was committed or pushed. Standard error names the event:

```
pinax: the shard and the projection could not be staged, so event <id> in <shard> is appended and not committed.
pinax: the commit was refused, so event <id> in <shard> is appended and not committed.
```

Start by looking at your state:

```
git status --short .ergon
pinax verify
```

After `commit_refused`, `git status --short .ergon` shows your shard and the pages staged, with `M` or `A` in the first column. After `stage_failed`, Git has staged what it could, and when an ignore rule is the cause, `git status --short .ergon` shows the shard and the pages staged in the same way. Then tell the cause apart by what `pinax verify` prints and by `git status`:

| What you see | Cause | What to do |
|---|---|---|
| `pinax verify: LOG SWALLOWED BY GITIGNORE`, and `sync_status` `stage_failed` | An ignore rule hides the log | [An ignore rule](#an-ignore-rule-hides-the-log) |
| `pinax verify: EVENT LOG INTEGRITY FAILURE` | An event in the log was edited by hand | [An edited event and a refused write](#an-edited-event-and-a-refused-write) |
| `git status` shows a merge in progress | An unfinished merge | Resolve and commit the merge, or run `git merge --abort` |
| `pinax verify: OK - projection matches log.`, no merge, and your own hook's output in `git_output` | Your own pre-commit hook refused | Remove what your hook objected to, then run `git add .ergon`, commit and push, the steps that finish [the ignore rule case](#an-ignore-rule-hides-the-log) |

You have recovered when `pinax verify` prints `pinax verify: OK - projection matches log.` and `git status --short .ergon` prints nothing. Do not rerun the write command before then, because it would append a second event. `pinax decide` is the exception: rerunning it commits the decision already appended, adds nothing, and publishes it once.

#### An ignore rule hides the log

Pinax's own `.ergon/.gitignore` contains `!/log/*.jsonl` to counter such a rule, and a rule elsewhere can still hide the log. `pinax verify` then exits 1 and prints the following, naming a path it asks Git about, and no such file is created:

```
pinax verify: LOG SWALLOWED BY GITIGNORE - .ergon/log/pinax-doctor-probe.jsonl would be git-ignored right now.
  A .gitignore rule is shadowing the event log shard directory: new shard files
  will be silently local-only, never committed, invisible across worktrees/
  branches because Git ignores it. Run 'pinax init' again to (re)install the
  .ergon/.gitignore negation, or fix your repo's .gitignore.
```

A write then ends with exit 7 and `stage_failed`, and Git's own message follows Pinax's: `The following paths are ignored by one of your .gitignore files:` and `.ergon/log`. Remove the rule from your `.gitignore`, then check the pages and commit and push the waiting event yourself:

```
pinax verify
git add .ergon
git commit -m "Commit the Pinax event that could not be staged"
git push
```

The hook passes that commit, and the other clone receives the event with `git pull --no-rebase`.

#### An edited event and a refused write

The pre-commit hook refuses every commit while an edited event is in the log, so the write ends with exit 7 and `commit_refused`, and the report's `git_output` holds the integrity failure. `git checkout -- <shard>` does not recover from this, because Pinax has already staged your shard with the edit in it. The recovery returns everything under `.ergon/` to the last commit, staged copies included.

That recovery discards every tracker event appended since the last commit, not only the refused write, and you cannot commit them first while the hook refuses. Before you run it, note what it will remove. `git status --short .ergon` names the files, and `pinax doctor` lists the uncommitted events in its section `[1] uncommitted shard events`. The events at risk come from the refused write, from the commands listed under [events from commands that never push](#events-from-commands-that-never-push), from any earlier exit 7, and from a rejected claim's annulment. Then restore:

```
git restore --source=HEAD --staged --worktree .ergon
pinax verify
```

`pinax verify` then prints `pinax verify: OK - projection matches log.` and `git status --short .ergon` prints nothing. Run the refused write again, and every other command whose event you noted; the write publishes as normal.

When the edited event came to you in a colleague's pushed commit, keep your refused write and wait for the fix. Once they have pushed the revert described under [an event edited by hand](#an-event-edited-by-hand), pull it, rebuild the pages, and commit and push your waiting event:

```
git pull --no-rebase
pinax verify --fix
git add .ergon
git commit -m "Commit the Pinax event that was refused"
git push
```

After the pull, `pinax verify` reports drift on the board and the pages your write touched, and `pinax verify --fix` rebuilds them. The hook passes the commit, `pinax verify` prints OK, and the other clone receives your event with its next pull. The same steps also work with a `git stash` before the pull and a `git stash pop` after it.

### An event edited by hand

When an event has been edited and no write has run since, `pinax verify` exits 1 and prints:

```
pinax verify: EVENT LOG INTEGRITY FAILURE - 1 non-annulled event(s) no longer match their stored content hash or name a predecessor missing from their chain.
  Invalid events:
    id=3egsz7plfcmay4x3m4fyvk4h2a4rbol56hfoghit5w2yepgenteq seq=2 actor=alex@laptop type=item.created

  The append-only event log was modified after these events were written.
  'pinax verify --fix' only regenerates projections and will not rewrite
  event history. Restore the exact original event bytes. A valid ordinary
  event may be formally annulled before replacement; a malformed annulment
  record itself must be restored from known-good history.
```

`git status --short` shows your shard as modified and unstaged. Restoring the shard from the last commit discards every event appended to it since that commit, not only the edit, and the hook refuses any commit that would keep them. Note those events first, with `pinax doctor` section `[1] uncommitted shard events`, then restore:

```
git checkout -- .ergon/log/alex-laptop.jsonl
pinax verify
```

`pinax verify` then passes and the working tree is clean. Run again each command whose event you noted. When a write ran after the edit, follow [an edited event and a refused write](#an-edited-event-and-a-refused-write).

When the edit was committed, for example past the hook with `git commit --no-verify`, and perhaps pushed, undo that one commit with `git revert`. Find the commit with `git log --oneline`; its subject is whatever its author wrote. Then revert it, check, and push:

```
git revert --no-edit <the commit that made the edit>
pinax verify
git push
```

`git revert` adds a new commit that reverses only the edit, so every other event stays and no history is rewritten. `pinax verify` then prints `pinax verify: OK - projection matches log.`, and in the other clone `git pull --no-rebase` and `pinax verify` pass. [data-model-and-integrity.md](data-model-and-integrity.md) explains what this check can and cannot see.

### The pages drift from the log

Drift means the board or an item page on disk differs from what the log produces. `pinax verify` reports it on standard error and exits 1:

```
pinax verify: DRIFT DETECTED - projection differs from log.
  Drifted files:
    .ergon/board.md

  Run 'pinax verify --fix' to regenerate, or run any state-changing
  command (which regenerates atomically).
```

The message ends with one more line, which says the projection must never be edited by hand. Drift comes, for example, from a hand edit, from a `git pull` that merges the pages as text, or from clones on different releases (see [the section on releases](#clones-on-different-releases)). `pinax verify` compares the files in your working tree, and it reads line endings CRLF and LF as the same. With the installed hook, a commit while the pages drift is refused: `git commit` exits 1 and prints the same message.

Rebuild the pages and stage the result:

```
pinax verify --fix
git add .ergon
```

`pinax verify --fix` exits 0, printing `pinax verify: DRIFT DETECTED - regenerating projection (--fix)...` and the drifted files on standard error and `pinax verify --fix: OK - projection regenerated and matches log.` on standard output. When you had staged the hand-edited page, `git status --short` shows `MM .ergon/board.md` until you run `git add .ergon`. When the hand edit never reached a commit, the rebuilt page equals the committed one, so the working tree is then clean and there is nothing to commit.

Any write command repairs drift as well, because it rebuilds every page before it commits. A write made while the board is hand-edited publishes as normal, and afterwards `pinax verify` passes and the working tree is clean.

### Events from commands that never push

Some commands append events without fetching, committing or pushing: `pinax init`, `pinax status <id> <state>` (which sets an item's status), `pinax dispatch --claim` (which claims every item it lists), `pinax registry add` and `pinax registry rm` (which change the list of repositories the portfolio view reads). The same holds for `pinax reconcile` (which imports finished and parked items from a text file) and `pinax doctor --reconcile`. These commands also skip the `role@host` check on the writer. `pinax status app-4x56 ready`, for example, prints `pinax: item app-4x56 status -> ready (by alex@laptop)`, and `git status --short` then shows your shard, the board and the item page as modified and uncommitted. The claims that `pinax dispatch --claim` writes are real claims in your clone. These events reach other clones only once a commit includes your shard.

`pinax doctor` shows what has not left your machine. Its section `[1] uncommitted shard events` lists the files under `.ergon/` that are not committed and the events in them that HEAD lacks. Its section `[5] unsynced shards` lists events committed in HEAD that the remote's default branch lacks, or prints `none ahead of origin/main`. `pinax doctor` never fetches, so section 5 compares against the remote as you last fetched it. A stale claim, in its section `[2]`, is a live claim on an unfinished item that is older than a threshold, by default the project's claim expiry or 24 hours. `pinax doctor` exits 1 when it has findings, such as uncommitted files or stale claims, and unsynced events never count as findings.

`pinax doctor --reconcile` asks `[y/N]` before it commits the uncommitted files under `.ergon/` in one ordinary commit. It refuses while a merge, cherry-pick, revert or rebase is in progress, or while a file is unmerged. It then asks what to do with each stale claim, appends your answers on your machine, and asks you to commit them.

## Two machines show different owners for one item

Each clone shows the owner its own log gives it, so two clones that hold different events can disagree. `pinax board` shows the owner: a claimed item sits under `## Building`, and its row names the owner, for example `- app-4x56 · Write the install guide · building · alex@laptop · ` followed by its blockers. For an item with no live claim the same field shows the writer who created it. `pinax status app-4x56` does not show one item; it exits 2 with `pinax status: use either 'pinax status [--json]' or 'pinax status <id> <state>'.`

To settle it, pull and check in both clones:

```
git pull --no-rebase
pinax verify
pinax board
```

Once both clones hold the same commits, they fold the same log and show the same owner, and that owner is the answer. Among claims made before either saw the other, the one with the earlier timestamp wins. When the two still differ, look for one of these causes:

- A claim was made on a branch other than the default branch. It holds the item only in that branch's history.
- An event was written but not committed or not pushed: the commands under [events from commands that never push](#events-from-commands-that-never-push), `pinax dispatch --claim` among them, or a write that ended with a note under [exit 0](#exit-0-with-a-note-committed-on-your-machine-not-pushed). `pinax doctor` sections `[1]` and `[5]` list them.
- One clone has not pulled.
- A claim expired in one clone only. A claim ends when its expiry time has passed and a later event is recorded, so a clone that holds a later event sees the claim expired, while a clone without it still shows the claim live.

## The hooks

The pre-commit hook is what makes a commit that includes the log pass through `pinax verify`. `pinax init` installs it in the Git hooks directory each time it runs, with no option to skip it, and a clone has it only after `pinax init` has run there. It replaces an existing pre-commit hook only when that hook contains the words `Pinax drift lint`. Otherwise it leaves the existing hook in place and prints:

```
pinax init: .git/hooks/pre-commit already exists and is not a Pinax hook. Skipping hook installation. Run 'pinax verify' manually before commits.
```

The hook does nothing when `.ergon/log` does not exist. Otherwise it runs `pinax verify` from the repository top level, using the first of these that can run Pinax: the `pinax` program on your `PATH`, `python -m pinax` with the hook's own interpreter, or the interpreter that ran `pinax init`. When none of them can, it prints `pinax: WARNING - pre-commit hook could not find pinax; skipping verify.` and lets the commit through. On a clean commit it prints `pinax verify: OK - projection matches log.` on standard error, and on drift or an edited event it refuses the commit with the message `pinax verify` would print. It checks the files in the working tree, so it can refuse or pass a commit on files you have not staged.

The hook runs for Pinax's own commits too, and a refusal there is [exit 7](#exit-7-the-commit-was-refused). Pinax never commits with `--no-verify`.

A post-commit hook is a Git hook that runs after each commit. The source tree ships one for Pinax, and `pinax init` never installs it. Once you install it, after each commit it rebuilds `PORTFOLIO.md` and commits that file alone when Git sees a change. It skips commits whose subject starts with `pinax: regenerate PORTFOLIO.md`, skips while a merge, cherry-pick, revert or rebase is in progress, and never commits a `PORTFOLIO.md` that Git does not track yet. [portfolio-and-web.md](portfolio-and-web.md) explains the portfolio.

## Clones on different releases

Every clone, and every `pinax` that a Git hook finds first on `PATH`, must run 0.5.0 or later before any clone writes. The hook runs whichever `pinax` comes first on `PATH`, so an older copy there checks every commit, including Pinax's own, even after you have upgraded the copy you type.

When you set strict, Pinax repeats that requirement:

```
pinax: every clone, and every older pinax on PATH, must run 0.5.0 or later before this is used; an older client will report drift, and after upgrading, pinax verify --fix repairs it.
```

Every event Pinax writes is in format version 2, whose id covers the link to the writer's previous event, and Pinax still reads and verifies version 1 events, whose id does not cover that link. Version 1 events keep verifying under their own rule, so a log that mixes the two formats verifies, folds and replays.

A clone that runs a release before 0.5.0 can report drift on a page that a newer release wrote, and `pinax verify --fix` repairs it once that clone is upgraded.

0.4.0 and 0.5.0 build different pages from a log that holds an annulled annulment. The same is true of a log that holds a repeat claim by the writer who already held the item. Each release then reports the other's pages as drift, and its `pinax verify` exits 1. The hook in a clone that still runs 0.4.0 refuses ordinary commits while the pages differ. That clone's write commands still commit and push, and 0.5.0 reports the pages they push as drift.

Once every clone runs 0.5.0, follow these steps in each clone, one clone after another, in any order:

1. Run `pinax verify --fix`.
2. Run `git add .ergon`.
3. Run `git commit -m "tracker pages after the upgrade"`. If nothing is staged, go to the next step.
4. Run `git pull --no-rebase`. If it stops on a conflict in `.ergon/board.md`, run `pinax verify --fix` and `git add .ergon` again. Then finish the merge with `git commit --no-edit`.
5. Run `pinax verify --fix` again, because the pull can bring pages that an older release pushed.
6. Run `git add .ergon`.
7. Run `git commit -m "tracker pages after the pull"`. If nothing is staged, go to the next step.
8. Run `git push`.

When every clone has run these steps, `pinax verify` passes in each of them.

Under the strict process, the board and item pages show each blocked item's owner gate.

## Windows and file systems that ignore case

Pinax takes an exclusive lock on a shard before each append, using the Windows file lock on Windows, so two commands on one machine never lose a line. On Windows it compares repository roots and paths ignoring case, so a `--root` or `PINAX_ROOT` that differs only in case matches, and on other systems the comparison keeps case.

Writer names keep their role exactly as typed and lower-case the host, so `alex@Laptop` and `alex@laptop` are the same writer with the same shard. When no file of exactly the shard's name exists, Pinax reuses an existing shard whose name matches it ignoring case, so a file system that ignores case never ends up with two shards whose names differ only in case. Pinax reads only files whose names end in `.jsonl`, and on Windows it matches that ending in any letter case.

A log whose lines end in CRLF folds to the same state and the same event ids as one with LF. Pinax writes the board and item pages with LF line endings, and `pinax verify` treats CRLF and LF as equal when it compares them, so a checkout that converts line endings does not cause drift. `pinax init` also writes the line `.ergon/** text eol=lf` into `.ergon/.gitattributes`. Git reads that pattern relative to `.ergon/`, where it matches no file, so the line is harmless and the pages stay LF because Pinax writes them that way.

## What synchronisation does not protect against

Pinax has no lock between machines. Two writers can claim one item before either has seen the other's claim, and a claim that exited 0 can be shown as superseded after a later fetch.

The clock check applies only to `claim`, and it refuses only a clock that is behind the remote. Every timestamp comes from the clock of the machine that wrote it, and claim order depends on those timestamps.

Claim expiry depends on which events a clone holds, because a claim expires only at an event recorded after its expiry time. Two clones with different events can disagree on whether a claim is still live.

A claim made on a branch other than the default branch holds the item only on that branch, and clones on the default branch do not see it until the branch is merged and pushed.

The claims that `pinax dispatch --claim` writes are never committed by that command. Other clones do not see them until a commit that includes your shard is pushed. Before a write command pushes, it annuls each waiting claim of its own writer on an item that another writer already holds on the remote. A waiting claim that you push by hand with Git can still take an item from a later claim, because no Pinax command runs then. The same holds when you commit a waiting claim and another writer's command in the same clone then pushes that commit, because a command checks only the claims of its own writer.

The union merge rule applies only in clones that have `.ergon/.gitattributes` committed. `pinax init` writes that file and does not commit it, so it is committed only when you commit it or when you accept the commit that `pinax doctor --reconcile` offers.

A write that ran offline, without a reachable remote, or with no published default branch was checked against your local log alone. A strict process, a claim or an item that exists only on the remote did not stop it.

The commands listed under [events from commands that never push](#events-from-commands-that-never-push) publish nothing by themselves, and `pinax doctor` reports unsynced events only against the remote as last fetched.

Clones that run a release before 0.5.0 fall outside the steps on this page.

Synchronisation moves events between clones and does not decide who may write them. Any writer can claim, release, decide under the flow process, and change any policy, and nothing signs an event. Git history is the record of who pushed which lines, and when.
