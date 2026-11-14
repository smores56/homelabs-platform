# data_plane

Portable, stdlib-only libraries for the Nix-free personal data system in #660.
They are service-independent: nothing here imports Kubernetes, Nix, a provider
SDK, or `restic`. The same code runs in the local and external planes.

## Modules

| Module | Responsibility |
| --- | --- |
| `model` | Source contracts, per-field authority, capabilities, recovery groups |
| `identity` | Signed workload claims; external profiles cannot reach personal sources |
| `confirmation` | Normalized operation, hash binding, single-use/expiry/epoch gate, plain-text render |
| `event_log` | SQLite single-writer log, epochs, durable cursors, idempotency, long-poll |
| `intents` | SQLite intent/effect journal with a closed transition table; no blind replay |
| `broker` | Scoped reads and confirmed, revision-checked writes; outcome reconciliation |
| `broker_journal` | Adapter that persists the broker's full operation in the one journal |
| `backup.generations` | Immutable generation manifests, atomic writes, complete-generation selection |
| `backup.controller` | Fence/checkpoint/snapshot once, fan out independently, publish only if all copies land |
| `backup.destinations` | Destination protocol and fixture destinations; no `restic copy` |
| `collectors.relay` | Authenticated, schema-validated, rate-limited, idempotent ingest |
| `collectors.staging` | Staging generation, atomic publish, cursors, tombstones, fidelity limits |
| `memory.file_memory` | Portable per-profile file memory with provenance and tombstones |
| `recovery.planner` | Select a complete compatible generation and emit an ordered, dry-run restore plan |
| `github.backup` | Safe argv for mirror/bundle capture and a verifiable manifest |
| `cli` | Offline validation and inspection commands |

## Invariants

- **Authority is per field.** No database is called disposable because its media survives.
- **Personal is local-only.** `IdentityIssuer` rejects a personal scope on the external profile.
- **Destructive, bulk, irreversible, and local-to-general writes need a human.**
  The gate binds a token to the operation hash, intent id, and source epoch; it is
  single-use and expires.
- **No blind replay.** An `outcome_unknown` intent is reconciled by reading the
  source; it never returns to `applying`.
- **A generation is complete or it is not published.** Every required destination
  must independently capture the same snapshot before the manifest is written.
- **A partial enumeration never advances a cursor or deletes a replica object.**
- **Restore selects a complete generation**, never a mutable `latest` pointer.

## Commands

```sh
# host Python is fine in CI; locally use nix-shell for python3
PYTHONPATH=libs python3 -m unittest discover -s tests/data-plane -t tests/data-plane
tests/data-plane/check.sh

PYTHONPATH=libs python3 -m data_plane validate-catalog catalog.json
PYTHONPATH=libs python3 -m data_plane render-confirmation operation.json
PYTHONPATH=libs python3 -m data_plane manifest-hash generation.json
PYTHONPATH=libs python3 -m data_plane select-generation backups/*.json \
  --destinations nvme hdd r2 --datasets content --schema 1
```

Everything is fixture-only. The commands never contact a cluster, a provider, or
a backup repository.

## Integration

- The platform's SQLite journal is the single source for intent state; the
  broker reaches it through `broker_journal.BrokerJournal`.
- The services that wrap these libraries still need OCI images and manifests;
  see `deploy/data-plane/` and `ansible/` in this repository.
- The `libs/` tree is not yet covered by `scripts/kustomize-build-all.sh`; the
  new tree is validated by `deploy/data-plane/tests/check.sh`.
