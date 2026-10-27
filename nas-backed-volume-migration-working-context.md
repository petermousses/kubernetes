# nas-backed volume migration: working context

## purpose and operating rules

This is the handoff for the active three-stage migration tracked in [`nas-backed-volume-migration-plan.md`](nas-backed-volume-migration-plan.md). The approved direction is VolSync + Restic for PVC backup/restore, with database-native capture for databases. Keep existing production PVs/PVCs during backup onboarding, use array-backed storage on `openmediavault`, and complete the OMV-only stages before planning other nodes or PostgreSQL HA. Backups on the same host/array improve recovery history but do not protect against loss of that host or array.

The working branch is `codex/longhorn-omv-bootstrap`. At the current fetched refs, `origin/develop` (`8e068e7`) is an ancestor of this branch, so the earlier request to rebase on `develop` is satisfied. The LiteLLM config change is already in this branch's history (`bfaaebe` and adjacent LiteLLM commits). Avoid rewriting the shared branch again: OMV's `git pull --ff-only` workflow depends on the topic branch remaining fast-forwardable.

The user asked that every OMV command block start with `git pull --ff-only`, so they can paste the whole block from their existing repository checkout. Do not assume an absolute checkout path on OMV. For command bundles:

```bash
bash <<'BASH'
set -euo pipefail
git pull --ff-only
sha256sum --check <<'SHA256'
<expected-sha256>  scripts/backups/<script>.py
SHA256
revision=$(git rev-parse HEAD)
sudo python3 -B scripts/backups/<installer>.py --revision "$revision"
BASH
```

Use the exact expected hashes from the pushed commit; never invent them. Run Python with `sudo python3 -B` to avoid writing bytecode into the checkout. Keep shell blocks self-contained and fail-fast. Do not use `cd` to a guessed OMV path: the user runs the block in the repository checkout already open on OMV. Scheduled systemd workers use their installed root-owned runtime and must not run `git pull` themselves.

Prefer a small read-only inventory before long or stateful commands. Keep secrets, tokens, document contents, raw logs, and decrypted recovery data out of terminal output. Use sanitized diagnostics. Initial backup/cutover work is installed as a systemd unit so it can continue through SSH disconnects; after reconnect, inspect that unit and its private journal before retrying. Never blindly rerun a partially completed backup, lock, or migration. Preserve failed Jobs, recovery journals, captures, and private diagnostics unless a reviewed cleanup step proves they are disposable.

When an OMV run changes a script since the last run, pull first and verify its checksum. Distinguish old systemd journal lines from the current invocation: installers may include prior failure output before the new attempt begins. Keep commits scoped to intended files and never stage `.DS_Store` or private/local recovery files.

## approved order and progress

Namespace order is sequential and gated by the migration plan. Monitoring, n8n, and paperless-ngx have been approved and their backup/restore/recovery work is complete. Scheduled-run verification remains deferred for monitoring and n8n. Paperless-ngx later had a verified automatic run on 2026-10-26, despite an earlier deferral. LibreChat is approved and is the current namespace. Its automatic schedule, initial restore validation, and off-NAS recovery export remain pending. Do not start the next namespace (LiteLLM) until LibreChat is complete and approved.

| namespace | completed | still pending |
| --- | --- | --- |
| monitoring | initial backups, isolated native restores, recovery archive and off-NAS verification | scheduled-run verification (deferred) |
| n8n | live-data split from nested backup path, browser check, initial/daily backups, isolated restore, recovery archive and off-NAS verification | scheduled-run verification (deferred) |
| paperless-ngx | GPT state capture and storage cutover, repaired GPT API token, browser validation, seven-component initial/daily backup, isolated restore, retention proof, verified automatic run, recovery archive and off-NAS verification | none |
| LibreChat | user-approved baseline browser checks; Meilisearch endpoint verified; five repositories/endpoints installed and authenticated; initial backup resources installed | complete the consistent initial capture/upload/verification, restore validation, daily schedule and retention proof, recovery archive/off-NAS verification, browser checks after any restart, and schedule verification unless explicitly deferred |

Detailed historical receipts and implementation notes remain in `nas-backed-volume-migration-plan.md`; this file is the concise operational handoff.

## LibreChat current state

- The user checked the available baseline flows before the Meilisearch restart: sign-in, existing chats, existing upload/image if present, and search. The user later retracted an accidental “yes, search works” answer after that restart, so post-restart search is **not yet verified**. The other post-restart browser flows should be confirmed after the initial backup’s app restart.
- Five per-component HTTPS Restic repositories passed DNS, TLS, authentication, and isolation checks. Meilisearch capacity was adjusted to 25m CPU / 224Mi memory while preserving its existing PVC, image, and limits. The measured namespace reservation at setup was 120m CPU / 133.5Mi memory.
- The first initial-backup attempt stopped before capture because the budget check counted Restic server Pods from every namespace and saw more than one. `scripts/backups/prepare-librechat.py` was fixed to scope that count to LibreChat and committed/pushed as `64fc37f fix LibreChat Restic pod scope` (following `57d2ac1 add initial LibreChat backup runner`).
- The next attempt passed that preflight. It found no unsafe links, hard links, xattrs, or concurrent file changes in the three file volumes: `data` had 3 entries/2 files/1,125 bytes; `uploads` 5/1/70,471 bytes; `images` 7/5/3,914,087 bytes.
- It then stopped before app quiescence, capture, or source creation because the standalone MongoDB has TTL indexes. A `mongodump` without coordinating those TTL writes can be inconsistent. No production TTL setting was changed. Do not disable `ttlMonitorEnabled`: [MongoDB warns against doing that in production except under Support guidance](https://www.mongodb.com/docs/v8.0/reference/parameters/#mongodb-parameter-param.ttlMonitorEnabled).
- Current code work is adding a durable MongoDB `fsync` write-lock protocol around the native dump. The app writer is stopped during the coordinated file/DB/search capture; MongoDB's lock should prevent TTL deletes while `mongodump` reads the standalone database. The next run must verify lock ownership/state, capture, release the lock, verify the database is unlocked, then resume the app and verify application health. If release is uncertain or another lock is present, fail closed and keep LibreChat stopped for recovery. MongoDB's docs say standalone dumps can be inconsistent if writes occur and document using `fsync` lock for consistency: [mongodump consistency guidance](https://www.mongodb.com/docs/database-tools/mongodump/mongodump-examples/) and [fsync command](https://www.mongodb.com/docs/manual/reference/command/fsync/).
- The failed preflight printed `Failed to allocate directory watch: Too many open files`; a read-only diagnostic showed 10,944 allocated file handles against an effectively unlimited max, and inotify limits of 128 instances / 245,806 watches. The precise source of the watch warning is unknown. It was not the fatal preflight reason on the retry. The repository endpoint scope failure was fixed separately; do not conflate these warnings with the Mongo TTL guard.
- A previous install output showed the old Restic-Pod-count failure before the new run started; this was stale systemd journal text. On retries, identify the current invocation and current service result, not just any prior error line.

## current implementation review focus

The uncommitted change in `scripts/backups/backup-librechat.py` adds MongoDB fsync-lock acquisition, durable quiescence-journal state, cleanup on exceptions/signals, lock evidence in the database artifact, and removes the TTL-index rejection. Review the full state machine before running the service. Required invariants:

1. Check MongoDB is unlocked during preflight, before LibreChat stops. Never unlock a pre-existing fsync lock. MongoDB exposes a shared lock count, not per-caller lock ownership. Establish an unlocked baseline, persist `acquiring` before sending the command, require the returned count to be exactly one, and stop if another actor changes that count.
2. Only treat a lock as ours when the MongoDB Pod UID, container ID, and restart count still match the journal. A different process plus a remaining lock is an ambiguity: preserve the lock and keep LibreChat stopped.
3. Persist `releasing` before `fsyncUnlock`; require the command result and lock count to prove release, then independently verify `currentOp.fsyncLock` is false before resuming LibreChat.
4. A journal saying `released` must not be trusted blindly. Verify the database is actually unlocked before app resume; contradictory evidence must fail closed.
5. On timeout, signal, or dump failure, terminate/reap the local subprocess. A nonzero/ambiguous remote exit must retain the fsync lock and journal; do not unlock or resume the app until remote dump exit and lock release are proven.
6. Compare database inventory under the lock before and after the dump; verify the archive and captured file artifacts before upload. Record that TTL indexes existed, that the lock was acquired/released, and the DB process identity without exposing application data.
7. MongoDB documents fsync locks as persistent. After any Pod/container identity change, inspect the actual lock status; if it remains locked under a different identity, fail closed instead of guessing who owns it. If an interrupted dump has no recorded successful remote exit, keep the lock and LibreChat stopped until its exit is established.

Static parsing and `git diff --check` are appropriate here; the user has not asked to run tests. Never invoke the production systemd service as a local code check. The OMV initial backup itself is authorized once the reviewed runner is committed and the user runs the checksummed command.

## operational pitfalls learned

- `kubectl create --dry-run=client -o json` may emit a JSON stream with multiple objects, not one JSON object; parse all JSON documents and validate the exact expected set before mutation.
- Kubernetes adds defaulted fields to live Pod templates. Compare submitted fields while accounting for API defaults and fields injected by the runner; do not report defaulting as drift.
- Use API object UIDs/resource versions in guarded patches and recheck identities before state changes.
- Logs and inventory can be very large. Prefer purpose-built scripts that print summaries and keep diagnostics private rather than dumping raw logs or values into an SSH session.
- `systemd-run`/systemd directory watches may report “Too many open files” even when `/proc/sys/fs/file-nr` looks far below its maximum; investigate the relevant resource and command, but do not infer that this alone caused the operation failure.
- Do not use forced Restic unlocks. Monitoring previously showed stale-looking locks and an ISO timestamp parse bug; preserve lock evidence and repair parsing/ownership checks rather than unlocking blindly.
- Avoid commands that assume a repo path on OMV; the user has already hit a `cd` failure from a guessed path. Start with `git pull --ff-only` in the current checkout.
- Capture and recovery scripts must not print credential values, encryption keys, document titles/content, or raw application logs. Off-NAS verification must not leave decrypted files behind.

## next actions

1. Finish adversarial review of the pending MongoDB fsync-lock implementation, especially stale/released markers, process identity changes, lock-count races, interrupts, and subprocess cleanup.
2. Update `nas-backed-volume-migration-plan.md` with the observed LibreChat preflight stop and the approved consistency approach; do not duplicate all of this handoff into that plan.
3. Run static syntax and whitespace/diff checks only. Do not run tests unless the user asks.
4. Commit and push only the reviewed LibreChat script, migration plan, and this handoff document. Leave `.DS_Store` unstaged.
5. Provide the user a checksummed OMV command, beginning with `git pull --ff-only`; ask them to paste the systemd output. The runner should leave the app stopped if MongoDB lock release cannot be proven.
6. After the initial backup passes, continue with restore validation, recovery export/off-NAS verification, browser confirmation of LibreChat flows (including search after the Meilisearch restart), and daily scheduling/retention. Ask whether to defer scheduled-run verification only at the final stage gate.

## procedural-memory checkpoint

Current classification: **do nothing**. The useful procedural conventions are recorded here because the user explicitly requested this handoff. No skill patch is justified by this task alone.
