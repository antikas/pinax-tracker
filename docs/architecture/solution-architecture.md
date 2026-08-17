# Solution architecture

Pinax is a standard-library Python command-line application with Git as its
only external dependency.

```text
CLI command
    |
append-only JSONL log
    |
deterministic fold
    |
generated board and item pages
```

The CLI appends events under a file lock and regenerates projections after
state-changing commands. Read commands fold the current log. Git carries log
shards between clones, and the union merge driver preserves concurrent appends.

The diagrams in this directory show the event flow and item lifecycle.
