"""
python -m pinax — Pinax CLI entry point.

Supported commands:
    pinax init   [--actor <actor>]
    pinax add    --title <title> [--prefix <prefix>] [--actor <actor>] [--json]
                 [--allow-new-prefix]
        (fails loudly if --prefix/default 'pnx' has never appeared
         among this tracker's existing item IDs -- a likely mis-bind, not a
         legitimate new prefix; --allow-new-prefix overrides. An empty
         tracker is exempt: first add always succeeds regardless of prefix)
    pinax claim  <id> [--actor <actor>] [--json]
    pinax status [--json] [--repo <path>|--portfolio] [--since Nd|--all] [--all-branches]
    pinax status <id> <state> [--actor <actor>] [--json]
    pinax done   <id> --briefing <file> [--actor <actor>] [--json]
    pinax block  <id> --gate <gate> [--actor <actor>] [--json]
    pinax park   <id> --reason <reason> [--actor <actor>] [--json]
    pinax priority <id> <rank>|bump|top [--actor <actor>] [--json]
        (priority control: appends item.priority_set;
         compute_next honours it ABOVE critical-path depth. <rank> is an
         explicit int, lower = more urgent; 'top' = ahead of every
         currently-prioritised item; 'bump' = one step ahead of the item's
         own current rank, or 'top' if it has none yet)
    pinax annul  <event-id> --reason <reason> [--actor <actor>] [--json]
        (tombstone a junk/tampered event id — appends event.annulled;
         fold suppresses ITS SPECIFIC tamper-evidence warning and payload
         effects on every future fold; raw bytes stay untouched, append-only)
    pinax dep add <from_id> --to <to_id> --type <t> [--actor <actor>] [--json]
    pinax dep add <from_id> --blocks <to_id> [--actor <actor>] [--json]  (back-compat alias)
    pinax dep rm  <from_id> --to <to_id> --type <t> [--actor <actor>] [--json]
    pinax dep rm  <from_id> --blocks <to_id> [--actor <actor>] [--json]  (back-compat alias)
      where <t> is one of the valid edge types (see 'pinax dep add --help' or VALID_EDGE_TYPES in dep.py)
    pinax ready  [--actor <actor>] [--json] [--all-branches]
    pinax next   [--actor <actor>] [--json]
    pinax note add <item_id> --ref <ref> [--caption <text>] [--actor <actor>] [--json]
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
    pinax overview [--json] [--markdown] [--remote] [--roots <r1,r2,...>] [--max-depth <n>]
        (portfolio rollup: discovers repos by scanning roots for .ergon/ --
         default roots ~/src; registry below is an
         additive override/extra-roots supplement, not required.
         --markdown writes the committed PORTFOLIO.md projection, rung 2 of
         the view ladder, docs/portfolio-views.md.
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
         0 when clean.  --reconcile adds the guided action: one ordinary git
         commit for orphaned shards, and a done/park/skip prompt per stale
         claim appended via the normal event path -- the append-only log is
         never hand-edited)

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
import os
import subprocess
import sys

# Single-source edge-type enum (SSOT: dep.py).  All user-facing text derived here
# so adding a 6th type is a one-line change in dep.py only (ADR-001).
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
            print(
                "pinax: ROOT MISMATCH - the pinned root (--root/PINAX_ROOT) is "
                f"{root_pin!r} but walking up from the current directory "
                f"({cwd!r}) resolves to {resolved!r} instead. Refusing to "
                "proceed rather than silently operating on the wrong "
                "tracker. Run pinax "
                "from inside the pinned repo, fix the pin, or unset "
                "--root/PINAX_ROOT if this drift is intentional.",
                file=sys.stderr,
            )
            sys.exit(1)

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
    )


def _cmd_done(args: argparse.Namespace) -> None:
    from .commands.done import run
    run(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        item_id=args.id,
        briefing_path=args.briefing,
        actor=args.actor,
        as_json=args.json,
    )


def _cmd_block(args: argparse.Namespace) -> None:
    from .commands.block import run
    run(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        item_id=args.id,
        gate=args.gate,
        actor=args.actor,
        as_json=args.json,
    )


def _cmd_park(args: argparse.Namespace) -> None:
    from .commands.park import run
    run(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        item_id=args.id,
        reason=args.reason,
        actor=args.actor,
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
    )


def _cmd_annul(args: argparse.Namespace) -> None:
    from .commands.annul import run
    run(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        target_id=args.event_id,
        reason=args.reason,
        actor=args.actor,
        as_json=args.json,
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
        print(
            f"pinax dep {op}: --to and --blocks are mutually exclusive. "
            "Use '--to <id> --type blocks' or '--blocks <id>' but not both.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Case: neither --to nor --blocks given.
    if not has_to and not has_blocks:
        print(
            f"pinax dep {op}: must supply '--to <id> --type <t>' "
            "or '--blocks <id>' (back-compat alias for --type blocks).",
            file=sys.stderr,
        )
        sys.exit(1)

    # Case: --blocks alias (back-compat) — edge type is forced to "blocks".
    if has_blocks:
        return args.blocks, "blocks"

    # Case: --to without --type — require explicit --type (no silent default).
    if not has_type:
        print(
            f"pinax dep {op}: --to requires --type <t>. "
            f"Specify the edge type explicitly ({_VALID_TYPES_HELP}). "
            "To add a blocks edge use '--blocks <id>' or '--to <id> --type blocks'.",
            file=sys.stderr,
        )
        sys.exit(1)

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
    )


def _cmd_ready(args: argparse.Namespace) -> None:
    from .commands.ready import run
    run(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        actor=args.actor,
        as_json=args.json,
        all_branches=args.all_branches,
    )


def _cmd_next(args: argparse.Namespace) -> None:
    from .commands.next_cmd import run
    run(
        repo_root=_find_repo_root(_resolve_root_pin(args)),
        actor=args.actor,
        as_json=args.json,
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


def main(argv: list[str] | None = None) -> None:
    _configure_console_streams()
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
    p_add.add_argument("--json", action="store_true", help="Output JSON")
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
    p_claim.add_argument("--json", action="store_true", help="Output JSON")
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
    p_status.add_argument("--json", action="store_true", help="Output JSON")
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
    p_status.set_defaults(func=_cmd_status)

    # pinax done
    p_done = sub.add_parser("done", help="Mark item done with briefing work-record")
    p_done.add_argument("id", help="Item ID")
    p_done.add_argument("--briefing", required=True, metavar="FILE", help="Path to briefing file")
    p_done.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_done.add_argument("--json", action="store_true", help="Output JSON")
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
    p_block.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_block.add_argument("--json", action="store_true", help="Output JSON")
    p_block.set_defaults(func=_cmd_block)

    # pinax park
    p_park = sub.add_parser("park", help="Park an item with a reason (appends item.parked)")
    p_park.add_argument("id", help="Item ID")
    p_park.add_argument("--reason", required=True, help="Park reason")
    p_park.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_park.add_argument("--json", action="store_true", help="Output JSON")
    p_park.set_defaults(func=_cmd_park)

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
    p_priority.add_argument("--json", action="store_true", help="Output JSON")
    p_priority.set_defaults(func=_cmd_priority)

    # pinax annul
    p_annul = sub.add_parser(
        "annul",
        help="Tombstone a junk/tampered event id (appends event.annulled)",
    )
    p_annul.add_argument("event_id", help="The target event's content-hash id")
    p_annul.add_argument("--reason", required=True, help="Annulment reason")
    p_annul.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_annul.add_argument("--json", action="store_true", help="Output JSON")
    p_annul.set_defaults(func=_cmd_annul)

    # pinax dep (sub-subcommand: add / rm)
    # _VALID_TYPES and _VALID_TYPES_HELP are derived at module level from VALID_EDGE_TYPES
    # (dep.py) — adding a 6th type is a one-line change in dep.py only (SSOT, ADR-001).
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
    p_dep_add.add_argument("--json", action="store_true", help="Output JSON")
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
    p_dep_rm.add_argument("--json", action="store_true", help="Output JSON")
    p_dep_rm.set_defaults(func=_cmd_dep_rm)

    # pinax ready
    p_ready = sub.add_parser("ready", help="List items ready for dispatch")
    p_ready.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_ready.add_argument("--json", action="store_true", help="Output JSON")
    p_ready.add_argument(
        "--all-branches", action="store_true", dest="all_branches",
        help="Fold in every unmerged local branch's committed .ergon shards.",
    )
    p_ready.set_defaults(func=_cmd_ready)

    # pinax next
    p_next = sub.add_parser("next", help="Show single next item to dispatch")
    p_next.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_next.add_argument("--json", action="store_true", help="Output JSON")
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
    p_note = sub.add_parser("note", help="Add a typed ref note to an item")
    note_sub = p_note.add_subparsers(dest="note_op", required=True)

    p_note_add = note_sub.add_parser(
        "add",
        help=(
            "Append note.added event (ref must match "
            "^(koine://|~/knowledge/|projects/|docs/); caption <= 200 chars)"
        ),
    )
    p_note_add.add_argument("item_id", help="Item ID to attach the note to")
    p_note_add.add_argument(
        "--ref",
        required=True,
        help=(
            "Typed ref: must match ^(koine://|~/knowledge/|projects/|docs/) - "
            "a pointer to a knowledge-plane document, never knowledge content"
        ),
    )
    p_note_add.add_argument(
        "--caption",
        default=None,
        help="Optional caption (<= 200 chars)",
    )
    p_note_add.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_note_add.add_argument("--json", action="store_true", help="Output JSON")
    p_note_add.set_defaults(func=_cmd_note_add)

    # pinax metrics
    p_metrics = sub.add_parser(
        "metrics",
        help="Read-only metrics fold over the event log (never writes to knowledge plane)",
    )
    p_metrics.add_argument("--json", action="store_true", help="Output JSON")
    p_metrics.set_defaults(func=_cmd_metrics)

    # pinax report
    p_report = sub.add_parser(
        "report",
        help="Summary: shipped / parked / failed+blocked / next (read-only fold)",
    )
    p_report.add_argument("--json", action="store_true", help="Output JSON")
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
    p_dispatch.add_argument("--json", action="store_true", help="Output JSON")
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
    p_replay.add_argument("--json", action="store_true", help="Output JSON")
    p_replay.set_defaults(func=_cmd_replay)

    # pinax board
    p_board = sub.add_parser(
        "board",
        help="Print this repo's board (live push-down, read-only fold)",
    )
    p_board.add_argument("--json", action="store_true", help="Output JSON")
    p_board.add_argument(
        "--all-branches", action="store_true", dest="all_branches",
        help="Fold in every unmerged local branch's committed .ergon shards.",
    )
    p_board.set_defaults(func=_cmd_board)

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
    p_overview.add_argument("--json", action="store_true", help="Output JSON")
    p_overview.add_argument(
        "--markdown", action="store_true",
        help="Write the committed PORTFOLIO.md projection at the repo root "
             "(rung 2 of the view ladder, docs/portfolio-views.md) instead "
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
    p_registry_add.add_argument("--json", action="store_true", help="Output JSON")
    p_registry_add.set_defaults(func=_cmd_registry_add)

    p_registry_rm = registry_sub.add_parser(
        "rm",
        help="Unregister a repo from the portfolio rollup (registry.repo_removed)",
    )
    p_registry_rm.add_argument("--id", required=True, help="Repo id (lowercase slug)")
    p_registry_rm.add_argument("--actor", default=None, help="Actor string (role@handle)")
    p_registry_rm.add_argument("--json", action="store_true", help="Output JSON")
    p_registry_rm.set_defaults(func=_cmd_registry_rm)

    p_registry_list = registry_sub.add_parser(
        "list",
        help="List registered repos (read-only fold)",
    )
    p_registry_list.add_argument("--json", action="store_true", help="Output JSON")
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
    p_reconcile.add_argument("--json", action="store_true", help="Output JSON")
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
        help="Claim-staleness threshold in hours (default: 24) -- a claimed, "
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
    p_doctor.add_argument("--json", action="store_true", help="Output JSON")
    p_doctor.set_defaults(func=_cmd_doctor)

    parsed = parser.parse_args(argv)
    parsed.func(parsed)


if __name__ == "__main__":
    main()
