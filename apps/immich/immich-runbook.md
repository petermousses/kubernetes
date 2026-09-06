# Immich migration runbook

This is the stateful migration record for the four-service Immich Compose
unit. Read the reusable [stateful migration gates](/filesystem/k3s/runbooks/stateful-migration-gates.md)
before changing the storage or rollout design.

## Stack record

- Stack/namespace: `immich`
- Compose file/project: `/filesystem/docker/immich/compose.yaml` / `immich`
- Workload unit and hostname: `immich-server` / `photos.omv.mousses.xyz`
- Temporary k3s URL: `https://photos.omv.mousses.xyz:8443`
- Node: `openmediavault` (`10.9.20.14`); all local PVs are node-pinned
- Docker source library: `/filesystem/docker/immich/library`
- Kubernetes writable library: `/filesystem/k3s/data/immich/library`
- Docker source PostgreSQL data: `/filesystem/docker/immich/postgres`
- Kubernetes PostgreSQL data: `/filesystem/k3s/data/immich/postgres`
- Docker model-cache volume: `immich_model-cache`
- Kubernetes model cache: `/filesystem/k3s/data/immich/model-cache`
- External authoritative photos: `/filesystem/Media/Media/Photos` -> `/external/photos`, read-only, no copy
- Missing Docker Google Photos source: `/filesystem/Media/takeout-feb-2025/Takeout/Google\\ Photos`; the current host path is absent, so k3s preserves `/external/google_photos_peter` as an empty read-only mount
- Backup directory: `/filesystem/k3s/backups/immich`
- Rollback before Kubernetes writes: `kubectl -n immich scale deployment/immich-server deployment/immich-database --replicas=0 && docker compose -f /filesystem/docker/immich/compose.yaml up -d`

The 420G external photos tree is authoritative media and remains in place.
It is exposed through a static local PV with node affinity and mounted
read-only. Do not copy it, relocate it, or allow a second writer without a
separate approved external-storage migration.

## Configuration and secrets

Non-sensitive connection settings are in `k8s/apps/immich/configmap.yaml`.
The Compose `DB_PASSWORD` was bootstrapped into the live `immich-secret`; no
raw `.env` or Secret value is stored in the k3s tree. Rotate the database
password only as a coordinated PostgreSQL/Immich operation.

## Freeze and image baseline

- Docker Compose was stopped with `docker compose ... down`; no Immich
  containers remain.
- Images were inspected before stop and pinned by digest; see
  `immich-image-inventory.md`.
- The observed application version is Immich `v2.6.3`.

## Pre-cutover data gates

1. A current logical backup was created while PostgreSQL was running:
   `/filesystem/k3s/backups/immich/immich-db-20260810T043432Z.sql.gz`.
   `gzip -t` passed and `pg_dump` completed using PostgreSQL 14.19.
2. Docker was stopped before copy; the source library inventory was 85,498
   files and 74,308,539,431 logical bytes.
3. The quiescent library copy has the same 85,498 files and logical bytes.
   Its complete metadata inventory matched the source.
4. The quiescent PostgreSQL copy has 1,664 files and 397,072,542 logical
   bytes. Its complete metadata inventory matched the source after inherited
   setgid bits were removed from destination directories; `PG_VERSION` and
   `global/pg_control` hashes matched.
5. First helper copies that normalized ownership were preserved for diagnosis
   under `/filesystem/k3s/data/immich/*-copy-with-normalized-ownership-20260810T044005Z`;
   they are not mounted by Kubernetes.

## Deployment and cutover

Validate and apply:

```bash
kubectl apply --dry-run=server -k /filesystem/k3s/k8s/apps/immich
kubectl apply -k /filesystem/k3s/k8s/apps/immich
kubectl -n immich rollout status deployment/immich-database --timeout=10m
kubectl -n immich rollout status deployment/immich-machine-learning --timeout=25m
kubectl -n immich rollout status deployment/immich-server --timeout=10m
```

The database and Valkey entrypoints require their normal ownership/setup
capabilities; the manifests drop all other capabilities and retain
`allowPrivilegeEscalation: false` and `RuntimeDefault` seccomp. ML has a
25-minute startup budget because a cold model cache took about 15--17 minutes
on this node. Once initialized, its `/ping` and image healthcheck pass.

## Acceptance record

- Cutover date: `2026-08-09` MST / `2026-08-10T04:34Z` backup timestamp
- Docker stopped/verified: passed; Compose removed all four containers and
  `docker compose ps --all` is empty
- Data-copy result: passed; library and PostgreSQL source/destination
  inventories and metadata matched before Kubernetes startup
- PostgreSQL integrity: passed; copied raw database started cleanly, reported
  PostgreSQL 14.19, accepted queries, and retained `vchord`, `vector`, and
  expected PostgreSQL extensions; pre-cutover logical dump passed `gzip -t`
- PVC/PV result: passed; four claims Bound, including `ROX` external photos,
  with local node affinity and Retain policy
- Application result: passed; server logs report Immich `v2.6.3`, server
  `/api/server/ping` returns 200, ML Service `/ping` returns 200, and the
  external photos mount exposes the expected year directories
- Ingress/TLS result: passed from a temporary in-cluster client through k3s
  Traefik 8443; `/api/server/ping` returned HTTP 200 and `{"res":"pong"}`.
  Production and staging Certificates are Ready. A curl originating on the
  node itself cannot validate this ServiceLB port because klipper-lb uses the
  node PREROUTING path; test from another LAN client or in-cluster client.
- Runtime images: passed; all four pod `imageID` values match the inventory
  digests. The ML rollout has zero restarts after the cold-start probe-budget
  correction; server retains six startup restarts from the initial database
  dependency race and has been stable since.
- External-volume permission gate: passed; `/external/photos` rejected a
  controlled write with `Read-only file system`, `/external/photos/2001` was
  readable, and `/data` accepted and removed a controlled temporary write
- Replacement gate: passed; server, database, ML, and Valkey pods were each
  deliberately deleted and replaced. Each returned Ready and each Service
  had exactly one EndpointSlice address. The database replacement performed
  normal PostgreSQL crash recovery after the forced deletion and recorded one
  container restart before becoming Ready; its current logs are clean and it
  remains stable. Server, ML, and Valkey replacements have zero restarts.
- Post-start logical backup: passed; `/filesystem/k3s/backups/immich/immich-db-post-start-20260810T051718Z.sql.gz`,
  SHA-256 `9d83583fb46676f4c1665020ac37ca3d073b23aa7c8fcdcee4b8c548c671576b`,
  passed `gzip -t`
- NetworkPolicy controls: passed; server reached its documented ML Service
  `/ping` and a private Kubernetes API egress attempt was denied
- Final ingress API checks: passed from an in-cluster client; `/api/server/ping`
  200 `{"res":"pong"}`, `/api/server/version` 200 `2.6.3`, homepage 200,
  and unauthenticated `/api/server/about` 401
- Source preservation: passed; the Docker library source remains at 85,498
  regular files and the exact regular-file logical byte sum remains
  74,308,539,431; the source write sentinel is absent
- Authenticated UI, upload, mobile-sync, external-library scan, and ML
  inference/upload checks: not exercised in this agent run; require a user
  session and deliberate sample asset. The malformed/missing Google Photos
  source remains an existing Compose configuration issue, not a new k3s path.
- Rollback exercised: not exercised; source data remains retained, but after
  Kubernetes accepts writes the old source is no longer a lossless rollback
  target. Use the logical backup or application export for rollback after that
  point.
