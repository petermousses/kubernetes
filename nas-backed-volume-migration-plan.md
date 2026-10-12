# three-stage nas-backed volume migration

## approved architecture

use **VolSync + Restic for portable PVC backups**, with **database-native backups for databases**. keep existing live PVs/PVCs during backup onboarding. complete all three stages on `openmediavault` before making other nodes available; planned moves and PostgreSQL HA follow separately.

this revision replaces the required Longhorn installation and NFS backup target. Longhorn replication is not required for this plan. the preparatory scripts under `scripts/longhorn/` are superseded and must not be run for this migration.

VolSync provides scheduled backups and restores into destination PVCs. each protected PVC gets its own Restic repository and credentials Secret. a restore is an explicit recovery or migration operation; it does not provide automatic node failover. [VolSync Restic documentation](https://volsync.readthedocs.io/en/stable/usage/restic/index.html)

## backup layout and shared setup

- store repositories on the NAS array at **`/filesystem/k3s/data/<namespace>/backups/<pvc>/`**. for example, the current board-games database claim maps to `/filesystem/k3s/data/board-games/backups/data-postgres-0/`. for database-native backups, `<pvc>` identifies the protected database claim; its backup artifacts are uploaded from a separate staging PVC.
- serve these repositories through authenticated HTTPS **Restic rest-server endpoints on OMV**, one per namespace mounting only that namespace's backup directory. configure the persistent storage path explicitly and use `--private-repos` with a separate username per PVC. standard repository access lets VolSync enforce retention/pruning within that repository. keep credentials and encryption keys outside Git and recoverable independently of the cluster. [Restic rest-server](https://github.com/restic/rest-server)
- **keep repositories outside the live PVC contents and backup staging data.** `n8n` and `vaultwarden` currently bind their namespace roots. resolve that overlap during their approved namespace work before creating nested repositories, using separate live-data subdirectories and updated bindings with rollback preserved. never back up a repository into itself or recursively change ownership on a namespace root.
- verify the actual mount and available space before creating any backup, staging, cache, or restore volume. specify storage paths/classes explicitly so new volumes do not silently land on the NAS OS filesystem. neither 512 GB drive is assumed to be spare.
- install a compatible VolSync release and its required Kubernetes dependencies. provision its cache storage explicitly; existing static local storage classes do not dynamically provision cache PVCs.
- schedule **daily backups with seven-day retention**, staggered across namespaces. verify expiry and space reclamation. if append-only repository access is enabled, provide separate trusted NAS maintenance for retention/pruning rather than granting deletion to backup clients. [Restic rest-server append-only mode](https://github.com/restic/rest-server#append-only-mode)
- keep the array-check throttle already applied. schedule backup pruning and other heavy maintenance to avoid overlapping integrity checks where possible.
- preserve Syncthing and all read-only mounts with their existing OMV bindings and host pins. reuse `hosts/openmediavault/` without adding files there yet.

during the OMV-only stages, live data and NAS backups share a host and may share the same array. this protects recovery history, but does not protect against losing that host or array. a later node move still depends on NAS backup availability; a backup in a separate failure domain is a future decision.

## consistency and namespace procedure

current local PVs do not supply CSI snapshots. use VolSync `Direct` only for stable backup artifacts or data made consistent by application-specific coordination; it reads the PVC without an atomic point-in-time copy. native Btrfs snapshots require separate host integration and are not automatically Kubernetes snapshots. [VolSync copy methods](https://volsync.readthedocs.io/en/stable/usage/restic/index.html#backup-options)

for PostgreSQL, begin with database-native logical backups, including required roles and restore credentials. do not treat a file copy of running `PGDATA` as a valid backup. later physical backups and WAL archiving support point-in-time recovery. apply the appropriate native backup or quiescence procedure to MongoDB, SQLite, Redis/Valkey, and other stateful services. coordinate database and application files when they require a common recovery point. [PostgreSQL logical backups](https://www.postgresql.org/docs/current/backup-dump.html), [filesystem backup requirements](https://www.postgresql.org/docs/current/backup-file.html)

for **each namespace**, in table order:

1. inventory the live claims, bindings, underlying paths, read-only flags, dependencies, and data sizes. confirm the running workload uses the expected claim.
2. select the consistency procedure, repository paths, explicit storage placement, and rollback steps. resolve any live-data/repository overlap before creating repositories.
3. configure native backup production where needed and VolSync backup resources. quiesce writers when the consistency method requires it; restart them after the consistent backup is secured.
4. complete an initial backup, restore into fresh isolated PVCs on OMV, and verify the database/application data. keep restore validation isolated from production jobs, integrations, and writers.
5. verify application health, scheduled backup completion, retention, and the ability to recover using saved credentials. retain original bindings and data through verification. any required binding change needs a final consistent backup and a controlled cutover with writers stopped.
6. report the namespace result and stop. provide exact commands for the user to run; do not infer successful execution from generated manifests.

**approval is per namespace.** wait for explicit user approval before starting the next namespace. at each stage boundary, obtain separate explicit approval before starting the next stage. do not join or enable other nodes during stages 1–3. every listed namespace remains pinned to OMV throughout these stages.

## stage 1 — least impact

| namespace | writable data to protect (read-only: no) | retained OMV mounts (read-only: yes) | namespace steps and verification |
| --- | --- | --- | --- |
| `board-games` | PostgreSQL data | — | pilot native database backup → isolated restore → database and application verification, before changing any live binding. confirm the active `data-postgres-0` claim rather than assuming the older `board-games-postgres` claim is in use. |
| `searxng` | Valkey and search cache | — | configure both claim backups; restore and verify Valkey state and search service health. |
| `monitoring` | Prometheus, Alertmanager, Grafana, Loki | — | use service-appropriate consistency procedures; restore and verify dashboards, alerts, and retained metrics/logs. |

stop after each namespace. after `monitoring`, obtain separate approval for stage 2.

## stage 2 — medium impact

| namespace | writable data to protect (read-only: no) | retained OMV mounts (read-only: yes) | namespace steps and verification |
| --- | --- | --- | --- |
| `n8n` | data and images | — | separate live data from the nested backup repository; coordinate database and image recovery. restore and verify workflows, credentials, and images. |
| `paperless-ngx` | documents, AI, GPT, PostgreSQL, Redis | — | coordinate document and database backups; protect each claim. restore and verify documents, metadata, and application behavior. |
| `librechat` | data, uploads, images, MongoDB, Meilisearch | — | use native database/search backup procedures and coordinate uploaded files. restore and verify chats, uploads, and search. |
| `litellm` | PostgreSQL data | — | back up and restore the database; verify the gateway and its persisted configuration. |
| `jellyfin` | config and cache | movies and TV | protect writable claims; retain media bindings and OMV placement. restore and verify configuration and playback. |

stop after each namespace. after `jellyfin`, obtain separate approval for stage 3.

## stage 3 — high impact

| namespace | writable data to protect (read-only: no) | retained OMV mounts (read-only: yes) | namespace steps and verification |
| --- | --- | --- | --- |
| `immich` | library, model cache, PostgreSQL data | external photos | coordinate database and library recovery; retain the external photos binding read-only on OMV. restore and verify assets and jobs. |
| `authentik` | `/data` and PostgreSQL data | — | coordinate database and application data backups. restore and verify sign-in and dependent apps. |
| `crafty` | data | — | coordinate server saves and stopped writers as required. restore and verify configuration, worlds, and servers. |
| `vaultwarden` | data | — | separate live data from the nested backup repository; use a consistent database backup coordinated with attachments. restore and verify login and client sync. |

stop after each namespace. after `vaultwarden`, report completion of all three stages and obtain explicit approval before making other nodes available.

## after stage 3 — node moves and database HA

1. **after approval**, join the additional non-NAS nodes and verify their intended local storage. keep existing OMV scheduling pins until each destination is ready.
2. move workloads one namespace at a time, with explicit approval before each namespace. provision fresh destination PVCs, quiesce production writers for the final consistent backup, restore onto the destination, update bindings/placement, restart, and verify application data and health. keep the old volumes for rollback. these are restore-based moves; Kubernetes does not move existing local PV contents between nodes. Syncthing and read-only mounts retain their OMV bindings and pins.
3. once three non-NAS nodes and Kubernetes control-plane quorum independent of OMV are available, migrate PostgreSQL workloads to **CloudNativePG**: `board-games`, `paperless-ngx`, `litellm`, `authentik`, and `immich`. require separate explicit approval for each database cutover.
4. give each PostgreSQL instance its own RWO PVC. run three instances across separate nodes, synchronous replication to one standby, and quorum-aware promotion. configure PostgreSQL base backups and WAL archives to OMV through a supported CloudNativePG backup backend; the Restic REST endpoint is not itself that backend. verify failover, promotion safety, and client reconnection before the next database cutover. [CloudNativePG failover](https://cloudnative-pg.io/docs/current/failover/), [backup and recovery](https://cloudnative-pg.io/docs/current/backup/), [PostgreSQL replication](https://www.postgresql.org/docs/current/warm-standby.html)

backup-only recovery can lose writes made since the latest usable backup and requires time to restore. database replication and Kubernetes control-plane HA are separate later work.

## current implementation checkpoint

shared setup and the `board-games` pilot are approved. the relayed inventory confirms K3s v1.33.5+k3s1, PostgreSQL 17.6, and the active `data-postgres-0` claim on the OS filesystem. its reclaim policy has been changed to `Retain` and confirmed `Bound`. the older array-backed `board-games-postgres` claim is not mounted by `postgres-0`. the approved backup directory resolves to the Btrfs RAID array with approximately 32 TiB free.

snapshot-controller v8.4.0 and VolSync 0.16.0 are installed, with their CRDs established and both pods confirmed running on OMV. Restic is running, its repository and staging PVCs are bound, and both certificates are ready. the relayed `restic-repository-check-ppbv5` output confirms the mover hostname resolves to `10.43.70.64` with default `ndots: 5`, verified TLS/authentication/repository isolation, and repository integrity with **zero snapshots**. the endpoint is verified; the database is not backed up yet. VolSync's `volsync-src-postgres-backup-cache` claim is now bound to the reserved array-backed cache PV.

the original check resolved the hostname to `10.9.20.2` through the LAN search suffix. the standalone check now uses `ndots: 1`, matching the existing repository convention. VolSync 0.16.0 cannot override mover Pod DNS settings, so the mover uses the already-certified shorter hostname `restic.board-games.svc` with the same repository path and passwords in a separate credential Secret. the first-backup runner verifies that hostname under the mover's default DNS policy before creating the dump. [Kubernetes Pod DNS configuration](https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/#pods-dns-config), [VolSync 0.16.0 mover configuration](https://github.com/backube/volsync/blob/v0.16.0/api/v1alpha1/common_types.go)

the first native dump attempt failed immediately with a refused connection to the correct PostgreSQL Pod IP (`10.42.0.190:5432`), while `postgres-0` remained ready. no dump was uploaded and `postgres-backup` remains paused. delayed NetworkPolicy allow rules are a plausible cause, supported by the transient refusal observed in the successful Restic check, but are not confirmed. the corrected dump waits for an authenticated TCP query before producing artifacts. the runner can replace a terminal failed dump Job only after checking ownership, backup generation, paused source, empty staging, and terminated dump Pods; it saves the old Job and logs privately first and uses deletion preconditions. the corrected retry still needs execution on OMV. [Kubernetes NetworkPolicy Pod lifecycle](https://kubernetes.io/docs/concepts/services-networking/network-policies/#pod-lifecycle)

the native backup runner creates a PostgreSQL 17 custom archive plus globals, metadata, checksums, and row counts captured using the dump's exported snapshot. it publishes completed artifacts to the separate array-backed staging PVC, then unpauses and triggers VolSync. the source starts paused because VolSync begins its first sync immediately; a nonempty manual tag prevents continuous resynchronization. the runner checks successful source completion, reads the backed-up artifacts from Restic, and compares all checksums. retention is `within: 7d` with pruning at most once per day; no daily backup timer is enabled yet. [PostgreSQL snapshot option](https://www.postgresql.org/docs/17/app-pgdump.html#APP-PGDUMP-OPTIONS), [VolSync trigger implementation](https://github.com/backube/volsync/blob/v0.16.0/internal/controller/statemachine/machine.go)

recovery credentials and TLS secrets are saved on OMV under `/root/k3s-backup-recovery/board-games/data-postgres-0/`; the first-backup runner also saves the application database Secret. an independently recoverable copy is required before namespace completion.

the NAS command downloads the selected files from an immutable pushed commit into a temporary directory and then runs the setup script from that bundle. no NAS checkout or `git pull` is involved; pushing a commit alone does not apply Kubernetes resources.

after the initial backup succeeds, restore into fresh isolated array-backed PVCs and compare the restored database with the captured row counts. then enable and verify the daily dump/upload sequence, retention, and independent credential recovery. no namespace cutover is recorded as completed. later namespaces, stages, node enablement, and database cutovers retain their approval gates.

the initial CPU scheduling block is resolved. both board-games API and web resource adjustments rolled out successfully with two ready replicas and pinned running image digests. requests of `25m`/`64Mi` and `10m`/`16Mi` per replica release `180m` CPU and `160Mi` memory; existing limits are retained. original deployments are saved under `/root/k3s-backup-recovery/board-games/resource-adjustment/`. before this adjustment, OMV reserved `11985m` of `12000m` CPU and 99% of allocatable memory, versus `705m` measured CPU use. backup and restore jobs must fit the remaining CPU/memory budget and be sequenced to prevent overlap.
