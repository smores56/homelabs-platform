# Data apps: shares, storage, dumps and cutover

The four data apps live in the `cluster` guest, one namespace per app, each bound
to exactly one virtiofs share of the host's `/var/lib/data` tree. Nothing is
converted on the way in: the library trees and the database files stay where the
host's restic job already looks. This document is the contract between
`modules/nixos/cluster-vm.nix` (nix-config) and `clusters/homelab/data/`.

## One app, one namespace, one share

| Namespace | Host path | Guest path | PVC | App | Image |
| --- | --- | --- | --- | --- | --- |
| `immich` | `/var/lib/data/immich` | `/mnt/data/immich` | `immich-data` | Immich server, machine-learning, Postgres, Valkey | `ghcr.io/immich-app/immich-{server,machine-learning}:v3.0.3`, `ghcr.io/immich-app/postgres:17-vectorchord0.4.3-pgvector0.8.1`, `valkey:9` |
| `navidrome` | `/var/lib/data/music` | `/mnt/data/music` | `navidrome-data` | Navidrome | `deluan/navidrome:0.63.2` |
| `calibre` | `/var/lib/data/books` | `/mnt/data/books` | `calibre-data` | Calibre-Web (CWA image) | `crocodilestick/calibre-web-automated:v4.0.8` |
| `graphiti` | `/var/lib/data/graphiti` | `/mnt/data/graphiti` | `graphiti-data` | Graphiti + FalkorDB | `zepai/knowledge-graph-mcp:1.1.3-graphiti-0.31.2-standalone`, `falkordb/falkordb-server:v4.22.2` |

The guest mounts the share at `/mnt/data/<share>` with a uid translation
(`modules/nixos/cluster-vm.nix`): guest uid 1000 maps to the host's `smores`
(uid 1000, group `users`) and guest root maps to `nobody`. Every data pod
therefore runs as uid/gid 1000, and a file it writes is owned on the host by the
same user the native services used. Anything a pod writes is visible to the host
immediately — restic does not need a hook.

Inside `/data` the components use subdirectories, so a namespace owns one claim:

| Namespace | Subdirectory | Purpose |
| --- | --- | --- |
| `immich` | `library/` | Immich media (`IMMICH_MEDIA_LOCATION`) |
| `immich` | `postgres/` | `PGDATA` |
| `immich` | `valkey/` | Valkey AOF |
| `immich`, `graphiti` | `db-dumps/` | the nightly dumps below |
| `navidrome` | `navidrome/` | Navidrome database and cache index |
| `calibre` | `cwa-config/` | Calibre-Web `app.db` and settings |
| `graphiti` | `falkordb/` | FalkorDB AOF and graph |

## Static volumes, no hostPath

`clusters/homelab/platform/storage/` owns the storage contract:

- `StorageClass data-local`: `provisioner: kubernetes.io/no-provisioner`,
  `reclaimPolicy: Retain`, `volumeBindingMode: WaitForFirstConsumer`. There is no
  dynamic provisioning, so a PVC can only bind to a PV declared here.
- One `local` PV per share (`immich-share`, `navidrome-share`, `calibre-share`,
  `graphiti-share`) with `local.path: /mnt/data/<share>` — a path inside the
  guest, not a host path. The `no-host-access` admission policy forbids
  `hostPath` for the data tier, so this is the only way in.
- Every PV is pre-bound with `spec.claimRef` to one namespace and claim name. A
  mistyped or foreign claim does not bind at all instead of taking a neighbour's
  share.
- Node affinity pins each PV to `kubernetes.io/hostname: cluster`.

The node name is not discovered at runtime. The T3 module sets the guest's
`networking.hostName` to the shared fact `facts.name` (`cluster`) from
`modules/lib/vm-host.nix`, and k3s registers the node under its hostname. On the
host, confirm with the helper the module installs:

```sh
cluster-kubectl get nodes -o wide        # NAME should be exactly: cluster
```

If it ever differs, every data PVC stays `Pending` and nothing mounts: the
failure is a stalled app, not a mount of the wrong directory.

Two admission policies complete the isolation (`docs/tenancy.md`):
`data-pvc-scope` (a data pod may mount only PVC names starting with its own
namespace) and `data-pvc-storage` (a data PVC must be named `<namespace>-*` and
claim `data-local`).

## Dump timing contract (host restic)

Host restic runs at **03:30 Europe/Berlin with `RandomizedDelaySec = 15m`**
(`modules/nixos/restic-backup.nix`), so it starts somewhere in 03:30–03:45 and
walks `/var/lib/data` — which is every share, including the dumps.

| Job | Namespace | Schedule | Writes | Deadline |
| --- | --- | --- | --- | --- |
| `immich-dump` | `immich` | `45 2 * * *`, `timeZone: Europe/Berlin` | `/data/db-dumps/immich.dump` (`pg_dump -Fc`) | 30 min |
| `graphiti-dump` | `graphiti` | `45 2 * * *`, `timeZone: Europe/Berlin` | `/data/db-dumps/graphiti.rdb` (`redis-cli --rdb`) | 30 min |

Contract: a dump starts at 02:45 local, is bounded to 30 minutes
(`activeDeadlineSeconds: 1800`), and is therefore complete by 03:15 — 15 minutes
before the earliest restic start, 30 before the latest. `concurrencyPolicy:
Forbid` keeps a slow run from overlapping the next one, and
`startingDeadlineSeconds: 600` drops a missed run rather than firing it late
into the backup window.

The `timeZone` field is what makes the contract DST-safe: the CronJob's clock is
the host's, not UTC. Do not drop it.

Failure behaviour: a failed dump leaves the previous night's file in place (the
job writes `.tmp` and renames), so restic backs up a stale-but-consistent dump
rather than a truncated one, and the failed Job is visible in
`kubectl -n <app> get jobs`.

Graphiti's store is FalkorDB, not Postgres, so `pg_dump` does not apply;
`redis-cli --rdb` streams a point-in-time snapshot without touching the
server's AOF or forcing a `BGSAVE` on the guest's 15 GiB.

The host's old `restic-db-dump` (Immich → `/var/lib/data/db/immich.dump`) is
replaced by these jobs. `modules/nixos/cluster-data-apps.nix` disables that unit
when the cutover option is on; until then both exist and the platform dump is
the authoritative one once Immich serves from the cluster.

## Cutover order

One app at a time, each verifiable on its own. Stop the native service before
copying so the tree does not change under the copy; the platform manifests can
be applied at any time (a missing share just leaves the pod unstarted).

1. **Ingress first.** Apply `platform-ingress` and confirm
   `kubectl -n ingress get deploy cloudflared` is `Available` with two replicas.
2. **Immich.** `sudo systemctl stop immich`. Create
   `/var/lib/data/immich/{library,postgres,valkey,db-dumps}` (the pods' init
   containers do it too). The host's media tree is **`/var/lib/data/photos`**
   (`modules/nixos/immich.nix` `mediaLocation`), owned `immich:immich` mode
   0700, so the copy runs as root and is then re-owned:
   `sudo rsync -a /var/lib/data/photos/ /var/lib/data/immich/library/` then
   `sudo chown -R smores:users /var/lib/data/immich/library` — the guest maps
   its uid 1000 to `smores`, and anything owned otherwise is unreadable to the
   pods. Dump the host database into the share
   (`sudo -u postgres pg_dump -Fc --no-owner immich > /var/lib/data/immich/db-dumps/immich.dump`).
   Then stop the cluster's server and machine-learning so nothing holds the
   database while it is replaced, restore the dump once, and scale back:
   `cluster-kubectl -n immich scale deploy/immich-server deploy/immich-machine-learning --replicas=0`,
   `cluster-kubectl -n immich exec deploy/immich-postgres -- pg_restore -U immich -d immich --no-owner --clean --if-exists /data/db-dumps/immich.dump`
   (the archive is already on the share, so the pod reads it directly; piping
   it through `exec -i` stalls once the stream's flow-control window fills, and
   `pg_restore` then sits unconnected with no backend to watch),
   then the two Deployments back to `--replicas=1`. The server migrates the
   schema at start. If the restore lands inside the 02:45 dump window, suspend
   `cluster-kubectl -n immich patch cronjob immich-dump -p '{"spec":{"suspend":true}}'`
   first so restic cannot capture the pre-restore database, and unsuspend after.
   Verify `/api/server/ping`, then flip the
   `photos.sammohr.dev` route Cloudflare-side (T7).
3. **Navidrome.** `sudo systemctl stop navidrome`. The music tree itself does
   not move — the `music` share already is the library — and the cluster
   Navidrome has been running since the share came up, so
   `/var/lib/data/music/navidrome` already holds a scan-only DB with no play
   counts, ratings, playlists or stars. Scale the Deployment to 0, then park it
   rather than nesting the source inside it:
   `sudo mv /var/lib/data/music/navidrome /var/lib/data/music/navidrome.cluster-scan`,
   `sudo install -d -o smores -g users /var/lib/data/music/navidrome`, copy the
   host DB in
   (`sudo rsync -a /var/lib/data/navidrome/ /var/lib/data/music/navidrome/`,
   `sudo chown -R smores:users /var/lib/data/music/navidrome`), then scale back
   to 1. Navidrome stores paths relative to the music folder, so the
   `/var/lib/data/music` → `/music` remount is safe; confirm the track count is
   unchanged after the scan. `music.sammohr.dev` exists only on the cluster:
   the host tunnel has always exposed this app as `navidrome.sammohr.dev`
   (behind the email allow policy), so the cutover also adds `music` to
   `cluster_hosts` — until then the name falls through the wildcard to the host
   tunnel's 404 catch-all.
4. **Calibre.** The library does not move. Stop the host's `calibre-server`
   user unit (`systemctl --user stop calibre-server`), set the Calibre-Web admin
   password on first login (`docs/manual-steps.md` step 13), and repoint the
   agent-facing path (calibre-server's content server → Calibre-Web's `/opds`).
   That repoint touches the broker's `calibre` logical host and the agent
   `calibredb` wrapper, both outside this repository: live-gated.
5. **Graphiti.** Copy, do not move, the graph while the host stack still runs:
   `sudo systemctl stop memory-graphiti-mcp memory-falkordb`,
   `sudo rsync -a /var/lib/memory/falkordb/ /var/lib/data/graphiti/falkordb/`,
   start the cluster pods, and confirm `kubectl -n graphiti exec deploy/graphiti -- wget -qO- localhost:8184/health`.
   `memory.sammohr.dev` keeps pointing at the host's `memory-mcp` wrapper (that
   is what maki authenticates against), so the host's copy stays authoritative
   until the wrapper itself moves. The cluster Ingress for that host records the
   eventual target and must not be published before the wrapper is in-cluster.

Rollback for any app: start the native service again. Its data is still at the
host path until the copy step moved it, and a `mv` is reversed by moving it back.

## Tunnel split (host vs cluster)

Two tunnels, two tokens, no shared state:

- **Host tunnel** (`modules/nixos/web-proxy.nix`, tunnel `smortress`): `ssh`,
  `agent`, `memory`, the Kanbei private route and every Access policy stay
  human-owned. `llm` and `chat` are retired (`docs/manual-steps.md` step 17).
- **Cluster tunnel** (`clusters/homelab/ingress/`): `cloudflared` in the
  `ingress` namespace, token mode, two replicas, `--protocol http2` because the
  host's tap firewall only forwards 80/443/DNS/NTP out of the guest. Its token
  comes from the SOPS contract `cloudflare-tunnel-token-sops` in `ingress`
  via ESO; no plaintext exists in this repository.

The Cloudflare side — creating the tunnel, its DNS records, the Access
applications and the ingress rules — is T7 (`smores56/sdlc#581`; #580 depends on
it), and it is now code in `tofu/` (`docs/cloudflare.md`). A hostname joins the
cluster tunnel by appearing in `cluster_hosts` **and** in
`local.cluster_ingress` in `tofu/cluster.tf`; the plan refuses the first without
the second. Before a cutover the wildcard CNAME keeps the name on the host. The
tunnel token is what step 15 hands over; until then the Secret is absent and the
connector pods stay unstarted (fail closed).

Because the tunnel runs in token mode but its configuration is managed by
`cloudflare_zero_trust_tunnel_cloudflared_config.cluster`, the ingress rules are
what `tofu apply` writes, not a dashboard edit. The four `Ingress` objects in the
app directories are the reviewed routing contract that those rules mirror, not
something an in-cluster controller consumes (k3s's traefik is disabled in the
guest). The policy check asserts host and backend service/port against each
app's Service, so the two cannot drift silently.

## What is live-gated

Everything above that needs the cluster, the tunnel or a credential:

- first apply of the data stages (`platform-storage`, `platform-data-apps`,
  `platform-ingress`) and PV binding to node `cluster`;
- ESO creating `immich-db`, `neuralwatt-api-key` and `cloudflare-tunnel-token`
  after `docs/manual-steps.md` steps 8 and 12;
- the four cutover copies, and the tunnel route flips;
- Cloudflare-side tunnel/DNS/Access (T7);
- agent-facing calibre repoint and the memory wrapper cutover;
- CNI enforcement of the NetworkPolicies (a bare API server only stores them).

## Verification on the live cluster

```sh
cluster-kubectl -n immich get pvc,pod
cluster-kubectl -n immich get cronjob immich-dump
cluster-kubectl -n immich create job --from=cronjob/immich-dump dump-now
cluster-kubectl -n immich logs job/dump-now
# on the host, the same file through the share:
sudo ls -l /var/lib/data/immich/db-dumps/immich.dump
```

A pod in `immich` that mounts `navidrome-data` must be rejected, and a PVC in
`immich` with `storageClassName: standard` must be rejected; both cases are in
`tests/policies/fixtures/` and are exercised against the real API server by
`tests/policies/check.sh --runtime`.
