# Pinax - project context

## Context authority

This file owns runtime-neutral project context. Provider-specific files import it and contain mechanics only.

Read `README.md` for use and `DESIGN.md` for the event model before changing behaviour. `DESIGN.md` owns event semantics.

## Project boundary

Pinax is a deterministic, Git-native work tracker. Its append-only JSONL event log is operational truth, and pure folds produce readable projections and status views.

Do not copy live tracker state into documentation. Query the tracker for current state and keep documentation focused on durable behaviour.

## Change rules

- Preserve append-only event history and deterministic replay.
- Treat projections as derived views, never competing sources of truth.
- Extend the canonical event and fold model before adding parallel status logic.
- Keep public content free of private work items, local paths, and unpublished source context.
- Use focused package checks for code changes. Instruction-only changes need import, scope, and public-safety checks.

## Publication relationship

The private `pinax` repository is the source and release build home. This repository is its reviewed public projection.
