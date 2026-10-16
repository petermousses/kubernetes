#!/usr/bin/env bash
set -euo pipefail

readonly app_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly runtime="${CONTAINER_RUNTIME:-docker}"
readonly image='ghcr.io/berriai/litellm@sha256:3d88bd757134a00e8c00237b85a021ab1f6d54abb9c1df750f411dbe7256a500'

# All inference runs against loopback fixtures in the pinned deployment image.
# No credentials, cluster, host networking, or external providers are used.
exec "${runtime}" run --rm --network none \
  --env LITELLM_LOCAL_MODEL_COST_MAP=True \
  --env PYTHONDONTWRITEBYTECODE=1 \
  --volume "${app_root}:/contract:ro" --workdir /contract \
  --entrypoint python "${image}" \
  -m unittest discover -s tests -p test_reliability.py -q
