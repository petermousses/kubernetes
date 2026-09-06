# Image inventory: immich

The Docker images below were inspected before cutover and the Kubernetes
workloads use the recorded immutable registry digests.

| Service | Configured reference | Local image ID | Registry digest | Created | Application version / verification |
|---|---|---|---|---|---|
| server | `ghcr.io/immich-app/immich-server:v2` | `sha256:f71a3a1f325972f620101773d4e23ad6d6eecb81cc53c464431080d5a1cf9a28` | `ghcr.io/immich-app/immich-server@sha256:0cc1f82953d9598eb9e9dd11cbde1f50fe54f9c46c4506b089e8ad7bfc9d1f0c` | `2026-03-26T16:26:35.122200066Z` | `v2.6.3`; Docker `/api/server/version` and Kubernetes startup log |
| machine learning | `ghcr.io/immich-app/immich-machine-learning:v2` | `sha256:f8d6860c165a2de78e0cef32c08a3396e26b2d6431940326b828b979c63ec742` | `ghcr.io/immich-app/immich-machine-learning@sha256:33b17015c3d14f2565e9b8cd36b48a70027b14b5cd20da7fbfff21a370b0309c` | `2026-03-26T16:25:07.406341222Z` | `v2.6.3` image metadata; ML `/ping` and healthcheck |
| database | `ghcr.io/immich-app/postgres:14-vectorchord0.4.3-pgvectors0.2.0` | `sha256:178719aeb38df6f0ce8edc1d8320008379fcf04eb3444e76419cc98d9861e331` | `ghcr.io/immich-app/postgres@sha256:bcf63357191b76a916ae5eb93464d65c07511da41e3bf7a8416db519b40b1c23` | `2025-10-07T12:09:13.605869304Z` | PostgreSQL `14.19`; `pg_isready`, raw-data startup, query, and extensions |
| Valkey | `docker.io/valkey/valkey:9` plus the Compose digest | `sha256:ceec8dafe719eec3cd1d9b11111259a57f14dc3cd15ffd70da6ae5caee4f4dd` | `docker.io/valkey/valkey@sha256:3eeb09785cd61ec8e3be35f8804c8892080f3ca21934d628abc24ee4ed1698f6` | `2026-03-09T20:09:08.61409005Z` | `redis-cli ping` |

Kubernetes references are in `/filesystem/k3s/k8s/apps/immich` and runtime
`imageID` values were checked against these digests after rollout.
