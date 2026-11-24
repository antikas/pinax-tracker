"""
python -m pinax — Pinax CLI entry point.

Supported commands:
    pinax init   [--actor <actor>]
    pinax add    --title <title> [--prefix <prefix>] [--actor <actor>]
                 [--offline] [--json] [--allow-new-prefix]
        (fails loudly if --prefix/default 'pnx' has never appeared
         among this tracker's existing item IDs -- a likely mis-bind, not a
         legitimate new prefix; --allow-new-prefix overrides. An empty
         tracker is exempt: first add always succeeds regardless of prefix)
    pinax claim  <id> [--actor <actor>] [--json]
        (runs the publish sequence; a claim that publishes nothing fails,
         and claim never runs offline -- see "Publishing a state change"
         below)
    pinax status [--json] [--repo <path>|--portfolio] [--since Nd|--all] [--all-branches]
                 [--under ITEM_ID]
    pinax status <id> <state> [--actor <actor>] [--json]
        (--under ITEM_ID restricts the view to the transitive parent-child
         descendants of that item; repo scope only, refused together with
         --portfolio or the setter form above)
    pinax done   <id> --briefing <file> [--actor <actor>] [--offline] [--json]
        (runs the publish sequence; records the event locally when the
         remote cannot be reached, or when --offline is given)
    pinax block  <id> --gate <gate> [--owner-gate CLASS] [--actor <actor>] [--offline] [--json]
        (--owner-gate records the owner-gate class on the item. Under the
         strict process a block with gate decision or destructive must name
         a class from the project's list, and any class given must be on
         it; under flow, the default, nothing is checked)
    pinax park   <id> --reason <reason> [--actor <actor>] [--offline] [--json]
    pinax decide <id> --option <key> --label <text> --effect release|park
                 --reason <text> [--actor <actor>] [--offline] [--json]
        (records a decision: appends item.decided. park also appends
         item.parked with the same reason in the same commit; release leaves
         the status, gate and claim alone. A repeat of the latest committed
         decision appends nothing. Never a note. The value after --option,
         --label, --reason, --effect and --actor is always taken as that
         value, even when it starts with '-')
    pinax release <id> --reason <reason> [--actor <actor>] [--offline] [--json]
        (release an item's live claim: appends item.claim_released. Any
         actor may release, with a reason; the fold then clears the item's
         owner, claim time and claim event id, leaves the status alone, and
         a later claim by another actor wins. Refuses an unknown item, an
         item with no live claim, and an empty reason)
    pinax policy claim-expiry --hours N [--actor <actor>] [--offline] [--json]
        (repository-level: appends policy.claim_expiry_set, naming no item.
         The policy in force for a claim is the last such event at or
         before it in claim order, and 24 hours without one; the fold
         expires a live claim as of the first event after it whose
         timestamp passes that deadline, so a log that records nothing
         after a claim never expires it)
    pinax policy process --mode flow|strict [--actor <actor>] [--offline] [--json]
        (repository-level: appends policy.process_set. The last one wins;
         flow without one)
    pinax policy owner-gates --class TEXT [--class TEXT ...] [--actor <actor>] [--offline] [--json]
        (repository-level: appends policy.owner_gate_classes_set, the list
         strict checks against. The last one wins; the built-in list without one)
    pinax policy decision-actors --decider <role@host> [--decider ...] [--actor <actor>] [--offline] [--json]
        (repository-level: appends policy.decision_actors_set, the actors
         who may decide under strict. The last one wins; under strict
         without one nobody may decide, under flow it is not checked)
    pinax policy note-refs --prefix TEXT [--prefix TEXT ...] [--actor <actor>] [--offline] [--json]
        (repository-level: appends policy.note_ref_prefixes_set, the prefixes
         note add accepts besides files in the repository and web addresses.
         The last one wins; none without one)
    pinax policy check [--json]
        (read-only: lists blocked items for the owner with no valid
         owner-gate class. Exits 0 under flow; under strict, 1 when it
         lists any)
    pinax priority <id> <rank>|bump|top [--actor <actor>] [--offline] [--json]
        (priority control: appends item.priority_set;
         compute_next honours it ABOVE critical-path depth. <rank> is an
         explicit int, lower = more urgent; 'top' = ahead of every
         currently-prioritised item; 'bump' = one step ahead of the item's
         own current rank, or 'top' if it has none yet)
    pinax annul  <event-id> --reason <reason> [--actor <actor>] [--offline] [--json]
        (cancel one earlier event: appends event.annulled, and the cancelled
         event stays in the log and no longer counts. Annulling an annulment
         brings back what it cancelled; the annulment a rejected claim wrote
         cannot be annulled. Refuses an id that names no event)
    pinax dep add <from_id> --to <to_id> --type <t> [--actor <actor>] [--offline] [--json]
    pinax dep add <from_id> --blocks <to_id> [--actor <actor>] [--offline] [--json]  (back-compat alias)
    pinax dep rm  <from_id> --to <to_id> --type <t> [--actor <actor>] [--offline] [--json]
    pinax dep rm  <from_id> --blocks <to_id> [--actor <actor>] [--offline] [--json]  (back-compat alias)
      where <t> is one of the valid edge types (see 'pinax dep add --help' or VALID_EDGE_TYPES in dep.py)
    pinax ready  [--actor <actor>] [--json] [--all-branches] [--under ITEM_ID]
    pinax next   [--actor <actor>] [--json] [--under ITEM_ID]
        (--under ITEM_ID restricts ready/next to the transitive
         parent-child descendants of that item; on both, and on status
         below, an item carrying a live claim -- item.claimed reconciled
         in the fold -- is never eligible, on every machine that
         has folded the claim, regardless of its status field.
         'ready --json' keeps its released bare-array shape when --under
         is not given; with --under it is the envelope
         {"ready": [...], "under": "<id>"}. 'next --json' and
         'status --json' were already objects, so they simply gain an
         "under" key, the id or null, either way)
    pinax note add <item_id> --ref <ref> [--caption <text>] [--actor <actor>] [--offline] [--json]
        (<ref> is a repository-relative path to an existing file, an http or
         https address, or text that starts with a prefix set by 'pinax
         policy note-refs')
    pinax metrics [--json]
    pinax report  [--json] [--all-branches]
    pinax dispatch [--max N] [--claim] [--actor <actor>] [--json]
    pinax verify [--fix]    (check projection drift; --fix regenerates it and re-verifies)
    pinax replay --at <ref> [--json]  (time-travel fold to any git ref)
    pinax board  [--json] [--all-branches]  (this repo's board — live push-down, read-only fold)
        (--all-branches folds the union of the current log plus
         every unmerged local branch's committed .ergon shards -- the
         repo-wide truth view, marking items/events sourced only from a
         branch with that branch's name; board/report/ready all support it)
    pinax web    [--port PORT]  (loopback-only browser view of this repo's board,
                 item briefings, and deterministically ordered event history;
                 default port 0 asks the OS for an available port)
    pinax overview [--json] [--markdown] [--remote] [--roots <r1,r2,...>] [--max-depth <n>]
        (portfolio rollup: discovers repos by scanning roots for .ergon/ --
         default roots ~/src; registry below is an
         additive override/extra-roots supplement, not required.
         --markdown writes the committed PORTFOLIO.md projection, rung 2 of
         the view ladder, docs/portfolio-and-web.md.
         --remote folds remote repositories instead of local clones: the
         manifest is the url-bearing registry entries in this hub's log;
         each remote's PUSHED tip is sparse-fetched to a per-run temp dir
         (GitHub contents-API fallback) and folded through the same fold.
         Only pushed work is visible -- by design. Network reads only.
         Rejects --markdown; ignores --roots/--max-depth)
    pinax registry add --id <id> [--path <path>] [--url <url>] [--actor <actor>] [--json]
        (at least one of --path/--url; --url registers a remote for
         'pinax overview --remote')
    pinax registry rm  --id <id> [--actor <actor>] [--json]
    pinax registry list [--json]
    pinax reconcile [--file PATH] [--actor <actor>] [--dry-run] [--json]
        (import completed or parked work from a text file into .ergon events)
    pinax doctor [--stale-hours N] [--legacy-board PATH] [--now TS] [--json]
                 [--reconcile [--actor <actor>]]
        (read-only diagnosis of incomplete tracker trails --
         uncommitted working-tree .ergon shard events, stale claims older
         than N hours with no item.completed, and legacy-board frontmatter
         contradicting pinax facts on migrated items.  Exits 1 on findings,
         0 when clean.  N defaults to the claim expiry policy the log folds
         to, and to 24 when it sets none.  --reconcile adds the guided
         action: one ordinary git commit for orphaned shards, and a
         done/park/release/skip prompt per stale claim appended via the
         normal event path -- the append-only log is never hand-edited)

Publishing a state change: every command above that appends an event (add,
claim, done, block, park, decide, release, policy, priority, dep, note, annul) runs
the one publish sequence in pinax.sync -- refuse an actor that is not role@host, fetch
origin (unless offline mode applies), fold the union of the local log and
the remote default branch's committed shards, append, regenerate the
projection, commit the shard and the projection with the repository's hooks
running, and push the remote default branch when it is the checked-out
branch. All of them but claim accept --offline (or PINAX_OFFLINE=1) to skip
the fetch and the push and commit locally only; claim never goes offline,
since its whole purpose is making a claim visible on every other machine.
Their exit codes are:

    0  committed, and published when the remote default branch is checked out
    2  the actor is not written as role@host; nothing was appended
    4  the remote was required and the event could not be published, or the
       repository at the resolved root is not the top level of its own git
       repository (nothing was appended either way)
    5  the push was rejected on every attempt
    7  the shard and the projection could not be staged, or the commit was
       refused; either way the appended event stays uncommitted
    8  the project's policy refuses the event: under the strict process, a
       block for the owner without a listed owner-gate class, or a decide
       from an actor not on the decision-actor list or with no list set;
       under any process, a note reference that note add does not accept
    9  the event log holds a line that cannot be used as an event; every
       command that reads the log ends this way, except verify, which lists
       each such line and exits 1
   10  PINAX_TEST_RUN is 1 and the log is not under the temporary directory;
       every command that appends an event ends this way, and the event is
       not written

All commands operate on the repository at the current working directory (CWD),
resolved by walking up from CWD to the nearest ancestor .git directory.

Root guard: a global '--root PATH'
(must precede the subcommand, e.g. 'pinax --root <path> add ...') or the
PINAX_ROOT environment variable pins the EXPECTED root; every command then
errors instead of silently walking up if the CWD-resolved root differs from
the expected root. This prevents commands from operating on an unrelated
ancestor tracker.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import traceback

from . import refusal
from .append import AppendLeakGuardError, UnwritableEventError
from .event import DamagedLogLine

# Single-source edge-type enum (SSOT: dep.py).  All user-facing text derived here
# so adding a 6th type is a one-line change in dep.py only.
from .commands.dep import VALID_EDGE_TYPES as _VALID_EDGE_TYPES_SET
_VALID_TYPES: list[str] = sorted(_VALID_EDGE_TYPES_SET)   # deterministic alphabetical
_VALID_TYPES_HELP: str = "|".join(_VALID_TYPES)


def _find_repo_root(root_pin: str | None = None) -> str:
    """Resolve the Git worktree root, with an optional explicit root pin.

    Git supplies the root for both primary and linked worktrees. If Git is
    unavailable, use the current directory so read-only diagnostics can report
    Git-dependent checks as unavailable. A pin must resolve to the same path.
    """
    cwd = os.getcwd()
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
        )
    except OSError:
        root = None
    resolved = (
        os.path.normpath(root.stdout.strip())
        if root is not None and root.returncode == 0
        else cwd
    )

    if root_pin:
        pin_norm = os.path.normcase(os.path.normpath(os.path.abspath(root_pin)))
        resolved_norm = os.path.normcase(os.path.normpath(os.path.abspath(resolved)))
        if pin_norm != resolved_norm:
            refusal.refuse(
                "pinax: ROOT MISMATCH - the pinned root (--root/PINAX_ROOT) is "
                f"{root_pin!r} but walking up from the current directory "
                f"({cwd!r}) resolves to {resolved!r} instead. Refusing to "
                "proceed rather than silently operating on the wrong "
                "tracker. Run pinax "
                "from inside the pinned repo, fix the pin, or unset "
                "--root/PINAX_ROOT if this drift is intentional.",
                refusal.ROOT_MISMATCH,
            )

    return resolved


def _resolve_root_pin(args: argparse.Namespace) -> str | None:
    """
    The explicit root pin for this invocation: --root
    takes precedence over the PINAX_ROOT environment variable; neither set
    means no pin (unchanged walk-up behaviour).
    """
    explicit = getattr(args, "root", None)
    return explicit if explicit else os.environ.get("PINAX_ROOT") or None


def _configure_console_streams() -> None:
    """
    Make every CLI print path tolerant of data that the active console code
    page cannot encode.  JSON paths already use ensure_ascii=True; this guard
    covers human output containing user-supplied titles.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(errors="replace")
            except (OSError, ValueError):
                pass


def _parse_since_days(value: str) -> int:
    text = value.strip().lower()
    if text.endswith("d"):
        text = text[:-1]
    try:
        days = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--since must be an integer day count like 7d") from exc
    if days < 0:
        raise argparse.ArgumentTypeError("--since must be zero or greater")
    return days


def _cmd_init(args: argparse.Namespace) -> None:
    from .commands.init import run
    run(repo_root=_find_repo_root(_resolve_root_pin(args)), actor=args.actor)


def _cmd_add(args: argparse.Namespace) -> None:
    from .commands.add import run
    run(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        title=args.title,
        prefix=args.prefix,
        actor=args.actor,
        as_json=args.json,
        allow_new_prefix=args.allow_new_prefix,
        offline=args.offline,
    )


def _cmd_claim(args: argparse.Namespace) -> None:
    from .commands.claim import run
    run(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        item_id=args.id,
        actor=args.actor,
        as_json=args.json,
    )


def _cmd_status(args: argparse.Namespace) -> None:
    from .commands.status_cmd import run
    scope = "portfolio" if args.portfolio else "auto"
    since_days = None if args.all else args.since
    run(
        repo_root=args.repo or _find_repo_root(_resolve_root_pin(args)),
        item_id=args.id,
        new_status=args.state,
        actor=args.actor,
        as_json=args.json,
        scope=scope,
        since_days=since_days,
        all_branches=args.all_branches,
        under=args.under,
    )


def _cmd_done(args: argparse.Namespace) -> None:
    from .commands.done import run

    # done.run() has no offline= parameter of its own. pinax.sync reads the
    # PINAX_OFFLINE=1 environment variable as an equally valid offline
    # request (see pinax.sync.run_sequence), so --offline here sets that
    # variable for the one call and restores whatever it found there
    # afterwards, whether or not the call raised.
    had_env = "PINAX_OFFLINE" in os.environ
    prior = os.environ.get("PINAX_OFFLINE")
    if args.offline:
        os.environ["PINAX_OFFLINE"] = "1"
    try:
        run(
            repo_root=_find_repo_root(_resolve_root_pin(args)),
            item_id=args.id,
            briefing_path=args.briefing,
            actor=args.actor,
            as_json=args.json,
        )
    finally:
        if args.offline:
            if had_env:
                os.environ["PINAX_OFFLINE"] = prior
            else:
                os.environ.pop("PINAX_OFFLINE", None)


def _cmd_block(args: argparse.Namespace) -> None:
    from .commands.block import run
    run(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        item_id=args.id,
        gate=args.gate,
        owner_gate=args.owner_gate,
        actor=args.actor,
        as_json=args.json,
        offline=args.offline,
    )


def _cmd_park(args: argparse.Namespace) -> None:
    from .commands.park import run
    run(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        item_id=args.id,
        reason=args.reason,
        actor=args.actor,
        as_json=args.json,
        offline=args.offline,
    )


def _cmd_decide(args: argparse.Namespace) -> None:
    from .commands.decide import run
    run(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        item_id=args.id,
        option=args.option,
        label=args.label,
        effect=args.effect,
        reason=args.reason,
        actor=args.actor,
        as_json=args.json,
        offline=args.offline,
    )


_DECIDE_VALUE_FLAGS = frozenset({"--option", "--label", "--effect", "--reason", "--actor"})


def _bind_decide_values(argv: list[str]) -> list[str]:
    """
    For decide, join each value flag with the token after it as flag=value.

    argparse would read a reason such as '--offline' as the flag itself; a
    caller passing free text as one argument means it as the value. Only
    '--root' itself is skipped with its value before the subcommand; an
    abbreviation such as '--ro' leaves the list to argparse unchanged, as
    it always has.
    """
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--root":
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        break
    if index >= len(argv) or argv[index] != "decide":
        return argv
    bound = list(argv[: index + 1])
    rest = argv[index + 1:]
    position = 0
    while position < len(rest):
        token = rest[position]
        if token in _DECIDE_VALUE_FLAGS and position + 1 < len(rest):
            bound.append(f"{token}={rest[position + 1]}")
            position += 2
            continue
        bound.append(token)
        position += 1
    return bound


def _cmd_release(args: argparse.Namespace) -> None:
    from .commands.release import run
    run(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        item_id=args.id,
        reason=args.reason,
        actor=args.actor,
        as_json=args.json,
        offline=args.offline,
    )


def _cmd_policy_claim_expiry(args: argparse.Namespace) -> None:
    from .commands.policy import run_claim_expiry
    run_claim_expiry(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        hours=args.hours,
        actor=args.actor,
        as_json=args.json,
        offline=args.offline,
    )


def _cmd_policy_process(args: argparse.Namespace) -> None:
    from .commands.policy import run_process
    run_process(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        mode=args.mode,
        actor=args.actor,
        as_json=args.json,
        offline=args.offline,
    )


def _cmd_policy_owner_gates(args: argparse.Namespace) -> None:
    from .commands.policy import run_owner_gates
    run_owner_gates(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        classes=args.owner_gate_class,
        actor=args.actor,
        as_json=args.json,
        offline=args.offline,
    )


def _cmd_policy_decision_actors(args: argparse.Namespace) -> None:
    from .commands.policy import run_decision_actors
    run_decision_actors(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        deciders=args.decider,
        actor=args.actor,
        as_json=args.json,
        offline=args.offline,
    )


def _cmd_policy_note_refs(args: argparse.Namespace) -> None:
    from .commands.policy import run_note_refs
    run_note_refs(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        prefixes=args.prefix,
        actor=args.actor,
        as_json=args.json,
        offline=args.offline,
    )


def _cmd_policy_check(args: argparse.Namespace) -> None:
    from .commands.policy import run_check
    run_check(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        as_json=args.json,
    )


def _cmd_priority(args: argparse.Namespace) -> None:
    from .commands.priority import run
    run(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        item_id=args.id,
        rank_arg=args.rank,
        actor=args.actor,
        as_json=args.json,
        offline=args.offline,
    )


def _cmd_annul(args: argparse.Namespace) -> None:
    from .commands.annul import run
    run(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        target_id=args.event_id,
        reason=args.reason,
        actor=args.actor,
        as_json=args.json,
        offline=args.offline,
    )


def _validate_dep_args(args: argparse.Namespace, op: str) -> tuple[str, str]:
    """
    Validate dep add/rm CLI arguments.  Returns (to_id, edge_type) or exits 1.

    Rules:
    - --blocks <id>  is the back-compat alias for --to <id> --type blocks.
    - --to <id> --type <t>  is the standard form.
    - --to without --type  is rejected (no silent default to blocks).
    - neither --to nor --blocks  is rejected with a clear usage error.
    - both --to and --blocks  is rejected (ambiguous).

    Nothing is appended on rejection (validate-before-append invariant).
    """
    has_to = getattr(args, "to", None) is not None
    has_blocks = getattr(args, "blocks", None) is not None
    has_type = getattr(args, "type", None) is not None

    # Case: both --to and --blocks given — ambiguous.
    if has_to and has_blocks:
        refusal.refuse(
            f"pinax dep {op}: --to and --blocks are mutually exclusive. "
            "Use '--to <id> --type blocks' or '--blocks <id>' but not both.",
            refusal.INVALID_DEPENDENCY,
        )

    # Case: neither --to nor --blocks given.
    if not has_to and not has_blocks:
        refusal.refuse(
            f"pinax dep {op}: must supply '--to <id> --type <t>' "
            "or '--blocks <id>' (back-compat alias for --type blocks).",
            refusal.INVALID_DEPENDENCY,
        )

    # Case: --blocks alias (back-compat) — edge type is forced to "blocks".
    if has_blocks:
        return args.blocks, "blocks"

    # Case: --to without --type — require explicit --type (no silent default).
    if not has_type:
        refusal.refuse(
            f"pinax dep {op}: --to requires --type <t>. "
            f"Specify the edge type explicitly ({_VALID_TYPES_HELP}). "
            "To add a blocks edge use '--blocks <id>' or '--to <id> --type blocks'.",
            refusal.INVALID_DEPENDENCY,
        )

    return args.to, args.type


def _cmd_dep_add(args: argparse.Namespace) -> None:
    from .commands.dep import run_add
    to_id, edge_type = _validate_dep_args(args, "add")
    run_add(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        from_id=args.from_id,
        to_id=to_id,
        edge_type=edge_type,
        actor=args.actor,
        as_json=args.json,
        offline=args.offline,
    )


def _cmd_dep_rm(args: argparse.Namespace) -> None:
    from .commands.dep import run_rm
    to_id, edge_type = _validate_dep_args(args, "rm")
    run_rm(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        from_id=args.from_id,
        to_id=to_id,
        edge_type=edge_type,
        actor=args.actor,
        as_json=args.json,
        offline=args.offline,
    )


def _cmd_ready(args: argparse.Namespace) -> None:
    from .commands.ready import run
    run(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        actor=args.actor,
        as_json=args.json,
        all_branches=args.all_branches,
        under=args.under,
    )


def _cmd_next(args: argparse.Namespace) -> None:
    from .commands.next_cmd import run
    run(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        actor=args.actor,
        as_json=args.json,
        under=args.under,
    )


def _cmd_verify(args: argparse.Namespace) -> None:
    from .commands.verify import run
    run(repo_root=_find_repo_root(_resolve_root_pin(args)), fix=getattr(args, "fix", False))


def _cmd_note_add(args: argparse.Namespace) -> None:
    from .commands.note import run
    run(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        item_id=args.item_id,
        ref=args.ref,
        caption=getattr(args, "caption", None),
        actor=args.actor,
        as_json=args.json,
        offline=args.offline,
    )


def _cmd_metrics(args: argparse.Namespace) -> None:
    from .commands.metrics_cmd import run
    run(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        as_json=args.json,
    )


def _cmd_dispatch(args: argparse.Namespace) -> None:
    from .commands.dispatch import run
    run(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        max_items=getattr(args, "max", None),
        claim=getattr(args, "claim", False),
        actor=args.actor,
        as_json=args.json,
    )


def _cmd_report(args: argparse.Namespace) -> None:
    from .commands.report_cmd import run
    run(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        as_json=args.json,
        all_branches=args.all_branches,
    )


def _cmd_replay(args: argparse.Namespace) -> None:
    from .commands.replay_cmd import run
    run(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        ref=args.at,
        as_json=args.json,
    )


def _cmd_board(args: argparse.Namespace) -> None:
    from .commands.board_cmd import run
    run(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        as_json=args.json,
        all_branches=args.all_branches,
    )


def _cmd_web(args: argparse.Namespace) -> None:
    from .commands.web_cmd import run
    run(repo_root=_find_repo_root(_resolve_root_pin(args)), port=args.port)


def _parse_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("--port must be an integer from 0 to 65535") from error
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("--port must be an integer from 0 to 65535")
    return port


def _cmd_overview(args: argparse.Namespace) -> None:
    from .commands.overview import run
    roots = [r.strip() for r in args.roots.split(",") if r.strip()] if args.roots else None
    run(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        as_json=args.json,
        as_markdown=args.markdown,
        roots=roots,
        max_depth=args.max_depth,
        remote=args.remote,
    )


def _cmd_registry_add(args: argparse.Namespace) -> None:
    from .commands.registry_cmd import run_add
    run_add(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        repo_id=args.id,
        path=args.path,
        url=args.url,
        actor=args.actor,
        as_json=args.json,
    )


def _cmd_registry_rm(args: argparse.Namespace) -> None:
    from .commands.registry_cmd import run_rm
    run_rm(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        repo_id=args.id,
        actor=args.actor,
        as_json=args.json,
    )


def _cmd_registry_list(args: argparse.Namespace) -> None:
    from .commands.registry_cmd import run_list
    run_list(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        as_json=args.json,
    )


def _cmd_reconcile(args: argparse.Namespace) -> None:
    from .commands.reconcile import run
    run(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        file_path=args.file,
        actor=args.actor,
        dry_run=args.dry_run,
        as_json=args.json,
    )


def _cmd_doctor(args: argparse.Namespace) -> None:
    from .commands.doctor_cmd import run
    run(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        stale_hours=args.stale_hours,
        legacy_board=args.legacy_board,
        now_iso=args.now,
        reconcile=args.reconcile,
        actor=args.actor,
        as_json=args.json,
    )


def build_parser() -> argparse.ArgumentParser:
    """The whole command line: every subcommand and its handler."""
    parser = argparse.ArgumentParser(
        prog="pinax",
        description="Pinax - Git-native build tracker",
    )
    # walk-up resolution.  Global (precedes the subcommand token, e.g.
    # 'pinax --root <path> add ...'); PINAX_ROOT env var is the fallback
    # pin when --root is not given (see _resolve_root_pin/_find_repo_root).
    parser.add_argument(
        "--root",
        default=None,
        metavar="PATH",
        help=(
            "Pin the expected tracker root; every command errors instead of "
            "silently walking up if the resolved .git root differs from this "
            "(also settable via the PINAX_ROOT environment variable)"
        ),
    )
    from . import __version__

    parser.add_argument(
        "--version",
        action="version",
        version=f"pinax {__version__}",
        help="Print the Pinax version and exit",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # pinax init
    p_init = sub.add_parser("init", help="Initialise .ergon/ in the current repo")
    p_init.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_init.set_defaults(func=_cmd_init)

    # pinax add
    p_add = sub.add_parser("add", help="Add a new item to the board")
    p_add.add_argument("--title", required=True, help="Item title")
    p_add.add_argument("--prefix", default="pnx", help="ID prefix (default: pnx)")
    p_add.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_add.add_argument(
        "--offline", action="store_true",
        help="Skip fetch and push; append and commit locally only "
             "(also settable via the PINAX_OFFLINE=1 environment variable)",
    )
    p_add.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_add.add_argument(
        "--allow-new-prefix",
        action="store_true",
        dest="allow_new_prefix",
        help=(
            "Override the prefix-collision guard: required when this "
            "IS a legitimate first use of a new prefix on a non-empty tracker. "
            "An empty tracker (no items yet) never needs this -- first add "
            "always succeeds regardless of prefix."
        ),
    )
    p_add.set_defaults(func=_cmd_add)

    # pinax claim
    p_claim = sub.add_parser("claim", help="Claim an item (appends item.claimed)")
    p_claim.add_argument("id", help="Item ID to claim")
    p_claim.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_claim.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_claim.set_defaults(func=_cmd_claim)

    # pinax status
    p_status = sub.add_parser(
        "status",
        help="Show work status, or set item status with <id> <state>",
    )
    p_status.add_argument("id", nargs="?", help="Item ID for setter form")
    p_status.add_argument(
        "state",
        nargs="?",
        help=(
            "New status: queued|ready|building|blind-verify|"
            "adjudicate|done|blocked|parked"
        ),
    )
    p_status.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_status.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_status.add_argument(
        "--repo",
        default=None,
        metavar="PATH",
        help="Show status for the Pinax repo at PATH (view form only)",
    )
    p_status.add_argument(
        "--portfolio",
        action="store_true",
        help="Show portfolio status even when the current directory is a repo",
    )
    p_status.add_argument(
        "--since",
        type=_parse_since_days,
        default=7,
        metavar="Nd",
        help="Show shipped items completed in the last N days (default: 7d)",
    )
    p_status.add_argument(
        "--all",
        action="store_true",
        help="Show all shipped items instead of the recent window",
    )
    p_status.add_argument(
        "--all-branches", action="store_true", dest="all_branches",
        help="Fold in every unmerged local branch's committed .ergon shards "
             "(repo view only)",
    )
    p_status.add_argument(
        "--under", default=None, metavar="ITEM_ID",
        help="Restrict the view to the transitive parent-child descendants "
             "of ITEM_ID (repo view only; refused with --portfolio)",
    )
    p_status.set_defaults(func=_cmd_status)

    # pinax done
    p_done = sub.add_parser("done", help="Mark item done with briefing work-record")
    p_done.add_argument("id", help="Item ID")
    p_done.add_argument("--briefing", required=True, metavar="FILE", help="Path to briefing file")
    p_done.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_done.add_argument(
        "--offline", action="store_true",
        help="Skip fetch and push; append and commit locally only "
             "(also settable via the PINAX_OFFLINE=1 environment variable)",
    )
    p_done.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_done.set_defaults(func=_cmd_done)

    # pinax block
    p_block = sub.add_parser("block", help="Block an item with a gate (appends item.blocked)")
    p_block.add_argument("id", help="Item ID")
    p_block.add_argument(
        "--gate",
        required=True,
        choices=["scope", "decision", "destructive", "proposal"],
        help="Gate type",
    )
    p_block.add_argument(
        "--owner-gate", default=None, metavar="CLASS",
        help="Owner-gate class, recorded on the item; required for gate "
             "decision or destructive under the strict process, and then "
             "checked against the project's list (see 'pinax policy check')",
    )
    p_block.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_block.add_argument(
        "--offline", action="store_true",
        help="Skip fetch and push; append and commit locally only "
             "(also settable via the PINAX_OFFLINE=1 environment variable)",
    )
    p_block.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_block.set_defaults(func=_cmd_block)

    # pinax park
    p_park = sub.add_parser("park", help="Park an item with a reason (appends item.parked)")
    p_park.add_argument("id", help="Item ID")
    p_park.add_argument("--reason", required=True, help="Park reason")
    p_park.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_park.add_argument(
        "--offline", action="store_true",
        help="Skip fetch and push; append and commit locally only "
             "(also settable via the PINAX_OFFLINE=1 environment variable)",
    )
    p_park.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_park.set_defaults(func=_cmd_park)

    # pinax decide
    p_decide = sub.add_parser(
        "decide",
        help="Record a decision on an item (appends item.decided; park also parks it)",
    )
    p_decide.add_argument("id", help="Item ID")
    p_decide.add_argument("--option", required=True, help="The chosen option's key")
    p_decide.add_argument("--label", required=True, help="The chosen option's label")
    p_decide.add_argument(
        "--effect", required=True, choices=["release", "park"],
        help="release leaves the item's status alone; park also parks it",
    )
    p_decide.add_argument("--reason", required=True, help="Why this was decided")
    p_decide.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_decide.add_argument(
        "--offline", action="store_true",
        help="Skip fetch and push; append and commit locally only "
             "(also settable via the PINAX_OFFLINE=1 environment variable)",
    )
    p_decide.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_decide.set_defaults(func=_cmd_decide)

    # pinax release
    p_release = sub.add_parser(
        "release",
        help="Release an item's live claim (appends item.claim_released)",
    )
    p_release.add_argument("id", help="Item ID")
    p_release.add_argument("--reason", required=True, help="Release reason")
    p_release.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_release.add_argument(
        "--offline", action="store_true",
        help="Skip fetch and push; append and commit locally only "
             "(also settable via the PINAX_OFFLINE=1 environment variable)",
    )
    p_release.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_release.set_defaults(func=_cmd_release)

    # pinax policy (sub-subcommand: claim-expiry)
    p_policy = sub.add_parser(
        "policy",
        help="Set a repository-level tracker policy",
    )
    policy_sub = p_policy.add_subparsers(dest="policy_op", required=True)

    p_policy_expiry = policy_sub.add_parser(
        "claim-expiry",
        help=(
            "Set the claim expiry in hours (appends policy.claim_expiry_set; "
            "applies to every claim from this event onward, 24 without one)"
        ),
    )
    p_policy_expiry.add_argument(
        "--hours",
        required=True,
        type=float,
        help="Hours a claim holds an item before the fold expires it "
             "(must be positive)",
    )
    p_policy_expiry.add_argument(
        "--actor", default=None, help="Actor string (role@handle)",
    )
    p_policy_expiry.add_argument(
        "--offline", action="store_true",
        help="Skip fetch and push; append and commit locally only "
             "(also settable via the PINAX_OFFLINE=1 environment variable)",
    )
    p_policy_expiry.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_policy_expiry.set_defaults(func=_cmd_policy_claim_expiry)

    p_policy_process = policy_sub.add_parser(
        "process",
        help=(
            "Set the project's process (appends policy.process_set; flow "
            "without one, strict requires an owner-gate class on owner blocks)"
        ),
    )
    p_policy_process.add_argument(
        "--mode", required=True, choices=["flow", "strict"], help="Process",
    )
    p_policy_gates = policy_sub.add_parser(
        "owner-gates",
        help=(
            "Set the owner-gate classes strict checks against (appends "
            "policy.owner_gate_classes_set; the built-in list without one)"
        ),
    )
    p_policy_gates.add_argument(
        "--class", dest="owner_gate_class", action="append", required=True,
        metavar="TEXT", help="One owner-gate class; repeat for each",
    )
    p_policy_deciders = policy_sub.add_parser(
        "decision-actors",
        help=(
            "Set who may decide under strict (appends "
            "policy.decision_actors_set; under strict nobody without one)"
        ),
    )
    p_policy_deciders.add_argument(
        "--decider", action="append", required=True, metavar="ROLE@HOST",
        help="One actor who may decide; repeat for each",
    )
    p_policy_refs = policy_sub.add_parser(
        "note-refs",
        help=(
            "Set the prefixes a note reference may start with, besides files "
            "in the repository and web addresses (appends "
            "policy.note_ref_prefixes_set; none without one)"
        ),
    )
    p_policy_refs.add_argument(
        "--prefix", action="append", default=None, metavar="TEXT",
        help="One prefix, without whitespace; repeat for each, at least one",
    )
    for parser_ in (p_policy_process, p_policy_gates, p_policy_deciders, p_policy_refs):
        parser_.add_argument("--actor", default=None, help="Actor string (role@handle)")
        parser_.add_argument(
            "--offline", action="store_true",
            help="Skip fetch and push; append and commit locally only "
                 "(also settable via the PINAX_OFFLINE=1 environment variable)",
        )
        parser_.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_policy_process.set_defaults(func=_cmd_policy_process)
    p_policy_gates.set_defaults(func=_cmd_policy_owner_gates)
    p_policy_deciders.set_defaults(func=_cmd_policy_decision_actors)
    p_policy_refs.set_defaults(func=_cmd_policy_note_refs)

    p_policy_check = policy_sub.add_parser(
        "check",
        help=(
            "List blocked items for the owner with no valid owner-gate class "
            "(read-only; exits 1 on findings under strict only)"
        ),
    )
    p_policy_check.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_policy_check.set_defaults(func=_cmd_policy_check)

    # pinax priority
    p_priority = sub.add_parser(
        "priority",
        help="Set an item priority (appends item.priority_set)",
    )
    p_priority.add_argument("id", help="Item ID")
    p_priority.add_argument(
        "rank",
        help="Explicit integer rank (lower = more urgent), or 'bump'/'top'",
    )
    p_priority.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_priority.add_argument(
        "--offline", action="store_true",
        help="Skip fetch and push; append and commit locally only "
             "(also settable via the PINAX_OFFLINE=1 environment variable)",
    )
    p_priority.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_priority.set_defaults(func=_cmd_priority)

    # pinax annul
    p_annul = sub.add_parser(
        "annul",
        help=(
            "Cancel one earlier event; annulling an annulment brings back what it "
            "cancelled; the annulment a rejected claim wrote cannot be annulled"
        ),
    )
    p_annul.add_argument("event_id", help="The target event's content-hash id")
    p_annul.add_argument("--reason", required=True, help="Annulment reason")
    p_annul.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_annul.add_argument(
        "--offline", action="store_true",
        help="Skip fetch and push; append and commit locally only "
             "(also settable via the PINAX_OFFLINE=1 environment variable)",
    )
    p_annul.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_annul.set_defaults(func=_cmd_annul)

    # pinax dep (sub-subcommand: add / rm)
    # _VALID_TYPES and _VALID_TYPES_HELP are derived at module level from VALID_EDGE_TYPES
    # (dep.py) — adding a 6th type is a one-line change in dep.py only (SSOT).
    p_dep = sub.add_parser(
        "dep",
        help=f"Manage dependency edges ({_VALID_TYPES_HELP})",
    )
    dep_sub = p_dep.add_subparsers(dest="dep_op", required=True)

    p_dep_add = dep_sub.add_parser(
        "add",
        help="Add a typed dependency edge (dep.added)",
    )
    p_dep_add.add_argument("from_id", help="Source item of the edge")
    p_dep_add.add_argument(
        "--to",
        default=None,
        metavar="TO_ID",
        help="Destination item of the edge (use with --type)",
    )
    p_dep_add.add_argument(
        "--type",
        default=None,
        choices=_VALID_TYPES,
        metavar="TYPE",
        help=f"Edge type: {_VALID_TYPES_HELP}",
    )
    p_dep_add.add_argument(
        "--blocks",
        default=None,
        metavar="TO_ID",
        help="Back-compat alias: equivalent to --to <TO_ID> --type blocks",
    )
    p_dep_add.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_dep_add.add_argument(
        "--offline", action="store_true",
        help="Skip fetch and push; append and commit locally only "
             "(also settable via the PINAX_OFFLINE=1 environment variable)",
    )
    p_dep_add.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_dep_add.set_defaults(func=_cmd_dep_add)

    p_dep_rm = dep_sub.add_parser(
        "rm",
        help="Remove a typed dependency edge (dep.removed)",
    )
    p_dep_rm.add_argument("from_id", help="Source item of the edge")
    p_dep_rm.add_argument(
        "--to",
        default=None,
        metavar="TO_ID",
        help="Destination item of the edge (use with --type)",
    )
    p_dep_rm.add_argument(
        "--type",
        default=None,
        choices=_VALID_TYPES,
        metavar="TYPE",
        help=f"Edge type: {_VALID_TYPES_HELP}",
    )
    p_dep_rm.add_argument(
        "--blocks",
        default=None,
        metavar="TO_ID",
        help="Back-compat alias: equivalent to --to <TO_ID> --type blocks",
    )
    p_dep_rm.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_dep_rm.add_argument(
        "--offline", action="store_true",
        help="Skip fetch and push; append and commit locally only "
             "(also settable via the PINAX_OFFLINE=1 environment variable)",
    )
    p_dep_rm.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_dep_rm.set_defaults(func=_cmd_dep_rm)

    # pinax ready
    p_ready = sub.add_parser("ready", help="List items ready for dispatch")
    p_ready.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_ready.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_ready.add_argument(
        "--all-branches", action="store_true", dest="all_branches",
        help="Fold in every unmerged local branch's committed .ergon shards.",
    )
    p_ready.add_argument(
        "--under", default=None, metavar="ITEM_ID",
        help="Restrict the ready set to the transitive parent-child "
             "descendants of ITEM_ID",
    )
    p_ready.set_defaults(func=_cmd_ready)

    # pinax next
    p_next = sub.add_parser("next", help="Show single next item to dispatch")
    p_next.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_next.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_next.add_argument(
        "--under", default=None, metavar="ITEM_ID",
        help="Restrict the candidate set to the transitive parent-child "
             "descendants of ITEM_ID",
    )
    p_next.set_defaults(func=_cmd_next)

    # pinax verify
    p_verify = sub.add_parser("verify", help="Drift lint: check projection matches log")
    p_verify.add_argument(
        "--fix",
        action="store_true",
        help="Regenerate the projection from the log (via the same path state-changing "
        "commands use), then re-verify",
    )
    p_verify.set_defaults(func=_cmd_verify)

    # pinax note (sub-subcommand: add)
    p_note = sub.add_parser("note", help="Attach a reference to an item")
    note_sub = p_note.add_subparsers(dest="note_op", required=True)

    p_note_add = note_sub.add_parser(
        "add",
        help=(
            "Append note.added event (ref is a repository-relative path, an http "
            "or https address, or starts with a prefix set by 'pinax policy "
            "note-refs'; caption <= 200 chars)"
        ),
    )
    p_note_add.add_argument("item_id", help="Item ID to attach the note to")
    p_note_add.add_argument(
        "--ref",
        required=True,
        help=(
            "The reference: a repository-relative path, an "
            "http or https address, or text that starts with a prefix set by "
            "'pinax policy note-refs'"
        ),
    )
    p_note_add.add_argument(
        "--caption",
        default=None,
        help="Optional caption (<= 200 chars)",
    )
    p_note_add.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_note_add.add_argument(
        "--offline", action="store_true",
        help="Skip fetch and push; append and commit locally only "
             "(also settable via the PINAX_OFFLINE=1 environment variable)",
    )
    p_note_add.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_note_add.set_defaults(func=_cmd_note_add)

    # pinax metrics
    p_metrics = sub.add_parser(
        "metrics",
        help="Read-only metrics fold over the event log (never writes to knowledge plane)",
    )
    p_metrics.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_metrics.set_defaults(func=_cmd_metrics)

    # pinax report
    p_report = sub.add_parser(
        "report",
        help="Summary: shipped / parked / failed+blocked / next (read-only fold)",
    )
    p_report.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_report.add_argument(
        "--all-branches", action="store_true", dest="all_branches",
        help="Fold in every unmerged local branch's committed .ergon shards.",
    )
    p_report.set_defaults(func=_cmd_report)

    # pinax dispatch
    p_dispatch = sub.add_parser(
        "dispatch",
        help="Emit the ready-item execution manifest with an optional --max cap",
    )
    p_dispatch.add_argument(
        "--max",
        type=int,
        default=None,
        metavar="N",
        help="Cap the manifest at N items (concurrency cap)",
    )
    p_dispatch.add_argument(
        "--claim",
        action="store_true",
        help="Automatically claim each manifest item",
    )
    p_dispatch.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_dispatch.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_dispatch.set_defaults(func=_cmd_dispatch)

    # pinax replay
    p_replay = sub.add_parser(
        "replay",
        help="Time-travel fold: reconstruct state exactly as it existed at a git ref",
    )
    p_replay.add_argument(
        "--at",
        required=True,
        metavar="REF",
        help="Git ref (branch, tag, or commit sha) to fold the log up to",
    )
    p_replay.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_replay.set_defaults(func=_cmd_replay)

    # pinax board
    p_board = sub.add_parser(
        "board",
        help="Print this repo's board (live push-down, read-only fold)",
    )
    p_board.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_board.add_argument(
        "--all-branches", action="store_true", dest="all_branches",
        help="Fold in every unmerged local branch's committed .ergon shards.",
    )
    p_board.set_defaults(func=_cmd_board)

    p_web = sub.add_parser(
        "web",
        help="Serve this repo's board, item briefings, and event history on loopback",
    )
    p_web.add_argument(
        "--port",
        type=_parse_port,
        default=0,
        metavar="PORT",
        help="Loopback port (0 selects an available port; default: 0)",
    )
    p_web.set_defaults(func=_cmd_web)

    # pinax overview
    p_overview = sub.add_parser(
        "overview",
        help="Portfolio rollup: discover repos by scanning roots for .ergon/, fold each into one view",
    )
    p_overview.add_argument(
        "--roots", default=None,
        help="Comma-separated root dirs to scan for .ergon/ "
             "(default: ~/src; also settable via PINAX_ROOTS)",
    )
    p_overview.add_argument(
        "--max-depth", type=int, default=None, dest="max_depth",
        help="Max directory depth to scan under each root "
             "(default: 3; also settable via PINAX_ROOTS_MAX_DEPTH)",
    )
    p_overview.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_overview.add_argument(
        "--markdown", action="store_true",
        help="Write the committed PORTFOLIO.md projection at the repo root "
             "(rung 2 of the view ladder, docs/portfolio-and-web.md) instead "
             "of printing",
    )
    p_overview.add_argument(
        "--remote", action="store_true",
        help="Fold repository remotes: the "
             "manifest is this hub's url-bearing registry entries "
             "('pinax registry add --id <id> --url <url>'); each remote's "
             "PUSHED tip is fetched to a per-run temp dir and folded. Shows "
             "only what is pushed to each remote -- unpushed local work is "
             "invisible by design (git's publish contract). Network reads "
             "only. Not combinable with --markdown; --roots/--max-depth are "
             "ignored",
    )
    p_overview.set_defaults(func=_cmd_overview)

    # pinax registry (sub-subcommand: add / rm / list)
    p_registry = sub.add_parser(
        "registry",
        help="Manage the set of repos 'pinax overview' folds (add/rm/list)",
    )
    registry_sub = p_registry.add_subparsers(dest="registry_op", required=True)

    p_registry_add = registry_sub.add_parser(
        "add",
        help="Register a repo for the portfolio rollup (registry.repo_added)",
    )
    p_registry_add.add_argument("--id", required=True, help="Repo id (lowercase slug)")
    p_registry_add.add_argument(
        "--path", default=None,
        help="Repo root path (local override for 'pinax overview' discovery); "
             "at least one of --path/--url is required",
    )
    p_registry_add.add_argument(
        "--url", default=None,
        help="Git remote URL. Registers the repository in the remote manifest "
             "that 'pinax overview --remote' folds; at least one of "
             "--path/--url is required",
    )
    p_registry_add.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_registry_add.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_registry_add.set_defaults(func=_cmd_registry_add)

    p_registry_rm = registry_sub.add_parser(
        "rm",
        help="Unregister a repo from the portfolio rollup (registry.repo_removed)",
    )
    p_registry_rm.add_argument("--id", required=True, help="Repo id (lowercase slug)")
    p_registry_rm.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_registry_rm.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_registry_rm.set_defaults(func=_cmd_registry_rm)

    p_registry_list = registry_sub.add_parser(
        "list",
        help="List registered repos (read-only fold)",
    )
    p_registry_list.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_registry_list.set_defaults(func=_cmd_registry_list)

    # pinax reconcile
    p_reconcile = sub.add_parser(
        "reconcile",
        help="Import completed or parked work from a text file into .ergon events",
    )
    p_reconcile.add_argument(
        "--file", default=None, metavar="PATH",
        help="Path to the action file (default: repository action file)",
    )
    p_reconcile.add_argument(
        "--actor", default=None,
        help="Reconciler actor (role@handle) -- provenance only, never overrides line actors",
    )
    p_reconcile.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be imported/rejected/skipped; touch nothing",
    )
    p_reconcile.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_reconcile.set_defaults(func=_cmd_reconcile)

    # pinax doctor
    p_doctor = sub.add_parser(
        "doctor",
        help="Diagnose incomplete tracker trails (read-only); "
             "--reconcile for the guided action",
    )
    p_doctor.add_argument(
        "--stale-hours", type=float, default=None, dest="stale_hours",
        metavar="N",
        help="Claim-staleness threshold in hours (default: the claim expiry "
             "policy the log folds to, or 24 when it sets none) -- a claimed, "
             "unfinished item older than this is flagged as claim-without-done",
    )
    p_doctor.add_argument(
        "--legacy-board", default=None, dest="legacy_board", metavar="PATH",
        help="Legacy board file or directory of *.md files to cross-check "
             "frontmatter (id/status) against pinax facts on migrated items "
             "(default: <repo>/board if it exists; report-only, legacy files "
             "are frozen archives)",
    )
    p_doctor.add_argument(
        "--now", default=None, metavar="TS",
        help="Reference UTC time (YYYY-MM-DDTHH:MM:SSZ) for claim ages -- "
             "pins the diagnosis to a pure function of (repo state, TS); "
             "default: current UTC time",
    )
    p_doctor.add_argument(
        "--reconcile", action="store_true",
        help="Guided fix: offer one ordinary git commit of orphaned .ergon "
             "shards, and prompt done/park/skip per stale claim (resolutions "
             "append normal events; the append-only log is never hand-edited). "
             "Not combinable with --json",
    )
    p_doctor.add_argument(
        "--actor", default=None,
        help="Actor for events appended under --reconcile (role@handle)",
    )
    p_doctor.add_argument("--json", action=_JsonFlag, help="Output JSON")
    p_doctor.set_defaults(func=_cmd_doctor)
    _tag_commands(parser)
    return parser


# The write commands: every subcommand whose handler runs the publish
# sequence. Under --json each of them prints exactly one JSON object on
# every exit (see _run_with_json_result). tests/test_json_every_exit.py
# derives the same set from the handlers, so a new write command cannot
# be left out silently.
WRITE_COMMANDS = (
    "add", "claim", "done", "block", "park", "decide", "release",
    "policy claim-expiry", "policy process", "policy owner-gates",
    "policy decision-actors", "policy note-refs", "priority", "annul",
    "dep add", "dep rm", "note add",
)

# Subcommands whose second word names the operation.
_GROUPED = frozenset({"policy", "dep", "note"})


def _subparser(parser: argparse.ArgumentParser, path: list[str]):
    """The parser for the subcommand words in path, or None."""
    current = parser
    for word in path:
        found = None
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                found = action.choices.get(word)
        if found is None:
            return None
        current = found
    return current


def _refused_json_command(parser: argparse.ArgumentParser, argv: list[str]) -> str | None:
    """
    The write command an argument list argparse refused names, when the
    list asks for --json; else None.

    Used only when argparse ended with an error before its --json action
    ran (see _JsonFlag), so a --json written where argparse refuses it,
    such as '--json=1', before the subcommand, between 'dep' and 'add', or
    after an option still waiting for its value, still asks for JSON and
    the argument error is reported as JSON. argv is the list after
    _bind_decide_values, so a decide value such as '--reason --json' is
    already one token, '--reason=--json'. Options are resolved as argparse
    resolves them, abbreviations included, against the parser for the
    words read so far: '--ro PATH' is the root pin, '--js' is --json. A
    '--' before the subcommand words is passed over, as argparse passes it
    over; after them, what follows is positional.
    """
    words: list[str] = []
    current = parser
    asked = False
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            complete = len(words) == 2 or (len(words) == 1 and words[0] not in _GROUPED)
            if complete:
                break
            index += 1
            continue
        if token.startswith("--"):
            name = token.split("=", 1)[0]
            option = _option_name(current, token)
            if option == "--json" or (option is None and len(name) > 2 and "--json".startswith(name)):
                asked = True
            elif current is parser and option == "--root" and "=" not in token:
                # The pin's value, unless argparse would read it as an option.
                if index + 1 < len(argv) and not argv[index + 1].startswith("-"):
                    index += 1
        elif not token.startswith("-"):
            if not words or (len(words) == 1 and words[0] in _GROUPED):
                words.append(token)
                current = _subparser(parser, words) or current
        index += 1
    command = " ".join(words)
    return command if asked and command in WRITE_COMMANDS else None


def _option_name(parser: argparse.ArgumentParser, token: str) -> str | None:
    """
    The long option a '--' token names in parser, as argparse resolves it:
    the exact name, or the one option the name abbreviates; else None.
    """
    name = token.split("=", 1)[0]
    options = parser._option_string_actions
    if name in options:
        return name
    if len(name) <= 2:
        return None
    matches = [option for option in options if option.startswith(name)]
    return matches[0] if len(matches) == 1 else None


# The write commands whose --json flag argparse has read in this process,
# appended by _JsonFlag in the order argparse read them.
_JSON_SEEN: list[str] = []


class _JsonFlag(argparse._StoreTrueAction):
    """
    --json, which also records which subcommand argparse read it for.

    argparse may still end with an error after reading the flag, such as a
    missing required option; the record says the caller asked for JSON
    exactly as argparse parsed the list, for the Python running it.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        super().__call__(parser, namespace, values, option_string)
        _JSON_SEEN.append(getattr(parser, "pinax_command", ""))


def _tag_commands(parser: argparse.ArgumentParser, path: tuple = ()) -> None:
    """Name each subcommand parser with its words, on the parser and its result."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for word, child in action.choices.items():
                _tag_commands(child, path + (word,))
            return
    parser.pinax_command = " ".join(path)
    parser.set_defaults(pinax_command=" ".join(path))


def _damaged_result(error: DamagedLogLine, command: str) -> dict:
    from .sync import EXIT_LOG_DAMAGED

    return {
        "published": False,
        "sync_status": refusal.LOG_DAMAGED,
        "error": str(error),
        "exit_code": EXIT_LOG_DAMAGED,
        "file": error.file,
        "line": error.line,
        "command": command,
    }


def _test_run_result(error: AppendLeakGuardError, command: str) -> dict:
    from .sync import EXIT_TEST_RUN_REFUSED

    return {
        "published": False,
        "sync_status": refusal.TEST_RUN_REFUSED,
        "error": str(error),
        "exit_code": EXIT_TEST_RUN_REFUSED,
        "command": command,
    }


def _unwritable_result(error: UnwritableEventError, command: str) -> dict:
    return {
        "published": False,
        "sync_status": refusal.INVALID_ARGUMENT,
        "error": str(error),
        "exit_code": 1,
        "command": command,
    }


def _run_refusing(parsed: argparse.Namespace) -> None:
    """
    Run a command outside the write commands' --json mode, ending it with
    the one refusal when it meets a damaged log line, when a test run may
    not write to its log, or when its event may not be written to a log.
    """
    command = getattr(parsed, "pinax_command", "")
    try:
        _run_parsed(parsed)
        return
    except DamagedLogLine as exc:
        result = _damaged_result(exc, command)
    except AppendLeakGuardError as exc:
        result = _test_run_result(exc, command)
    except UnwritableEventError as exc:
        result = _unwritable_result(exc, command)
    print(result["error"], file=sys.stderr)
    if getattr(parsed, "json", False):
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    raise refusal.Refusal(result["exit_code"], result["sync_status"])


def _run_parsed(parsed: argparse.Namespace) -> None:
    if getattr(parsed, "actor", None):
        from .append import normalise_actor
        parsed.actor = normalise_actor(parsed.actor)
    parsed.func(parsed)


class _WriteThrough(io.TextIOBase):
    """Standard error that still reaches the terminal and is also kept."""

    def __init__(self, stream) -> None:
        self._stream = stream
        self._kept = io.StringIO()

    def write(self, text: str) -> int:
        self._kept.write(text)
        if self._stream is not None:
            self._stream.write(text)
        return len(text)

    def flush(self) -> None:
        if self._stream is not None:
            self._stream.flush()

    def kept(self) -> str:
        return self._kept.getvalue()


def _exit_code(exc: SystemExit) -> int:
    if exc.code is None:
        return 0
    if isinstance(exc.code, int):
        return exc.code
    print(exc.code, file=sys.stderr)
    return 1


def _publication_result(line: str) -> dict | None:
    try:
        value = json.loads(line)
    except ValueError:
        return None
    if isinstance(value, dict) and "published" in value and "sync_status" in value:
        return value
    return None


def _write(stream, text: str) -> None:
    """Write text to stream when there is a usable one; never fail for it."""
    if stream is None or not text:
        return
    try:
        stream.write(text)
        stream.flush()
    except (OSError, ValueError):
        pass


# The exit code a JSON result reports for an interrupt: the shell's status
# for SIGINT. The process itself still ends by the interrupt.
_INTERRUPT_EXIT_CODE = 130


def _run_with_json_result(
    parsed: argparse.Namespace | None,
    parse_code: int,
    command: str,
    captured: io.StringIO,
    kept: _WriteThrough,
) -> None:
    """
    Run a write command under --json and print exactly one JSON object.

    parsed is None when argparse refused the arguments with parse_code.
    Standard output is captured; standard error is kept and still written
    through. Whatever ends the command, the one line printed is its own
    result on exit 0, the publish sequence's report when the command
    printed one, and otherwise a refusal object naming why it stopped.
    Every other line the command printed goes to standard error. The exit
    code is the command's own. An interrupt prints the result, then ends
    the process by the interrupt exactly as without --json.
    """
    real_stdout, real_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = captured, kept
    name = None
    error = None
    interrupt = None
    refused = None
    try:
        if parsed is None:
            raise refusal.Refusal(parse_code, refusal.INVALID_ARGUMENT)
        _run_parsed(parsed)
        code = 0
    except refusal.Refusal as exc:
        code, name = _exit_code(exc), exc.name
    except SystemExit as exc:
        # A plain exit the command did not name is a value refusal.
        code, name = _exit_code(exc), refusal.INVALID_ARGUMENT
    except DamagedLogLine as exc:
        refused = _damaged_result(exc, command)
        code, name = refused["exit_code"], refused["sync_status"]
        print(exc, file=sys.stderr)
    except AppendLeakGuardError as exc:
        refused = _test_run_result(exc, command)
        code, name = refused["exit_code"], refused["sync_status"]
        print(exc, file=sys.stderr)
    except UnwritableEventError as exc:
        refused = _unwritable_result(exc, command)
        code, name = refused["exit_code"], refused["sync_status"]
        print(exc, file=sys.stderr)
    except KeyboardInterrupt as exc:
        interrupt = exc
        code, name = _INTERRUPT_EXIT_CODE, refusal.INTERRUPTED
        error = "pinax: interrupted (KeyboardInterrupt)"
    except Exception as exc:
        traceback.print_exc()
        code, name = 1, refusal.INTERNAL_ERROR
        error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    finally:
        sys.stdout, sys.stderr = real_stdout, real_stderr

    chosen = None
    others = []
    for line in captured.getvalue().splitlines():
        result = _publication_result(line) if chosen is None else None
        if result is not None:
            chosen = result
        elif line.strip():
            others.append(line)
    _write(real_stderr, "".join(line + "\n" for line in others))

    if chosen is None and refused is not None:
        chosen = refused
    if chosen is None:
        if code == 0:
            name = refusal.INTERNAL_ERROR
            error = "pinax: the command printed no JSON result"
        if error is None:
            error = kept.kept().strip() or f"pinax {command}: exited with code {code}"
        chosen = {
            "published": False,
            "sync_status": name,
            "error": error,
            "exit_code": code,
            "command": command,
        }
    _write(real_stdout, json.dumps(chosen, sort_keys=True, ensure_ascii=True) + "\n")
    if interrupt is not None:
        raise interrupt
    if code != 0:
        sys.exit(code)


def main(argv: list[str] | None = None) -> None:
    """
    Parse the arguments, then run the command.

    Parsing always runs with standard output captured and standard error
    kept and written through, so the mode follows what argparse actually
    parsed: a write command whose parse read --json runs under
    _run_with_json_result, and every other command runs as it always has.
    A help or version text reaches standard output unchanged, exit 0.
    """
    _configure_console_streams()
    parser = build_parser()
    bound = _bind_decide_values(list(sys.argv[1:] if argv is None else argv))
    real_stdout, real_stderr = sys.stdout, sys.stderr
    captured = io.StringIO()
    kept = _WriteThrough(real_stderr)
    del _JSON_SEEN[:]
    parsed = None
    parse_code = 0
    sys.stdout, sys.stderr = captured, kept
    try:
        parsed = parser.parse_args(bound)
    except SystemExit as exc:
        parse_code = _exit_code(exc)
    finally:
        sys.stdout, sys.stderr = real_stdout, real_stderr

    if parsed is None and parse_code == 0:
        _write(real_stdout, captured.getvalue())
        sys.exit(0)
    if parsed is not None:
        named = getattr(parsed, "pinax_command", "")
        asked = bool(getattr(parsed, "json", False)) and named in WRITE_COMMANDS
        command = named if asked else None
    else:
        seen = [name for name in _JSON_SEEN if name in WRITE_COMMANDS]
        command = seen[0] if seen else _refused_json_command(parser, bound)
    if command is None:
        _write(real_stdout, captured.getvalue())
        if parsed is None:
            sys.exit(parse_code)
        _run_refusing(parsed)
        return
    _run_with_json_result(parsed, parse_code, command, captured, kept)


if __name__ == "__main__":
    main()
