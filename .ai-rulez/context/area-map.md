---
type: Concept
title: Area Map
x-ai-rulez:
  kind: context
  id: area-map
  metadata:
    priority: high
---

## Area Map

The xberg-io mise toolchain baseline that every repository follows.

- `baseline.toml` — canonical tool versions in mise `[tools]` syntax
- `scripts/toolchains.py` — `check` and `sync` for consumer repositories; tests in `tests/`
- `task-sets/` — shared mise task sets that `sync` writes into consumers
- `default.json` — Renovate preset for consumers
