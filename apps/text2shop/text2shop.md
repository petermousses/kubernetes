# Text2Shop k3s migration runbook

## Stack record

- Stack/namespace: `text2shop`
- Compose file/project: `/filesystem/docker/Text2Shop/compose.yaml` / `text2shop`
- Workload unit and hostname: `text2shop` / `text2shop.omv.mousses.xyz`
- Internal DNS server: `10.9.20.1`
- Temporary coexistence URL: `https://text2shop.omv.mousses.xyz:8443`
- k3s URL: `https://text2shop.omv.mousses.xyz`
- Docker source: `/filesystem/docker/Text2Shop`
- Kubernetes data destination: not applicable; the application stores its list in
  each browser's local storage and has no container data mount
- Rollback command:
  `kubectl -n text2shop scale deployment/text2shop --replicas=0 && docker compose -f /filesystem/docker/Text2Shop/compose.yaml up -d`

## Freeze and image baseline

- Watchtower is stopped as part of the migration freeze.
- The running Docker container uses locally built image ID
  `sha256:7dca9289dd42decbf56f3d8de335f00e4a3d3f448f7be03c4d65bc221996b337`.
- The published GHCR `develop` image was pulled separately and uses registry
  digest `sha256:e0ab9d02bf36392afb47ae81902d1cfc79e5c5a875784deea7b685f0ea8f6a27`.
- The full comparison is recorded in
  `/filesystem/k3s/inventory/images/text2shop.md`.
- Kubernetes is pinned to the GHCR digest; it does not use the local Docker
  image or a mutable tag.

## Data and environment

- No persistent, named-volume, external, NAS/media, or host-integration mounts
  exist. Browser local storage is outside the container and is not copied.
- The effective Compose environment contains `DOMAIN_NAME`,
  `LOCAL_DOMAIN_NAME`, and `PROJECT_SUB_DOMAIN`. `LOCAL_DOMAIN_NAME` must be
  resolved to `omv.mousses.xyz`; do not copy the literal `${DOMAIN_NAME}`
  expression from `.env`.
- The live Kubernetes Secret `text2shop-env` is generated from Docker Compose's
  resolved environment output and is intentionally not stored in Git:

  ```bash
  docker compose --env-file /filesystem/docker/Text2Shop/.env \
    -f /filesystem/docker/Text2Shop/compose.yaml config --environment \
    | awk -F= '/^(DOMAIN_NAME|LOCAL_DOMAIN_NAME|PROJECT_SUB_DOMAIN)=/ {print}' \
    | kubectl -n text2shop create secret generic text2shop-env \
        --from-env-file=/dev/stdin --dry-run=client -o yaml \
    | kubectl apply -f -
  ```

  This command sends the resolved values directly through the pipeline and does
  not write an unencrypted Secret manifest to the migration tree.

## Manifest gates

- Namespace, Service, Ingress, production/staging Certificates, probes,
  resource requests/limits, and default-deny NetworkPolicies are in the app
  Kustomization.
- The staging Certificate is applied and must become Ready before production
  Certificate acceptance.
- The workload is a stateless singleton with `replicas: 1` and
  `strategy.type: Recreate`.
- The GHCR image is public, so no registry pull Secret is required.

## Exact cutover order

1. Validate the rendered manifests and perform a server-side dry-run.
2. Apply the Namespace, environment Secret, NetworkPolicies, Service, Ingress,
   and staging Certificate while the workload is not running.
3. Wait for the staging Certificate to become Ready, then apply the production
   Certificate and wait for it to become Ready.
4. Confirm the Docker image baseline one final time.
5. Stop the complete Docker Compose service and verify no `text2shop` container
   remains running. There is no application data-copy step.
6. Apply the digest-pinned Deployment and wait for rollout. k3s/containerd
   pulls the GHCR image independently of the local Docker image.
7. Verify readiness, logs, events, the running image digest, EndpointSlice,
   exact-host TLS, and the route through packaged k3s Traefik.
8. Delete the pod once and verify the replacement becomes Ready, then repeat the
   route and functional checks.

## Functional and policy checks

- HTTPS route returns HTTP 200.
- HTML and a representative static asset return HTTP 200.
- A shopping list survives a browser refresh and tab close through local storage.
- A transient pod in another namespace cannot reach the Service under the
  default-deny policy, while packaged Traefik can reach the pod.
- Logs show successful NGINX startup without unexplained errors.

## Immediate rollback

If readiness, TLS, policy, or functional validation fails before acceptance:

1. Scale the Kubernetes Deployment to zero and wait for pod deletion.
2. Preserve failed pod logs and events. Do not copy data back; the app has no
   container-owned persistent dataset.
3. Start the unchanged Compose project:
   `docker compose -f /filesystem/docker/Text2Shop/compose.yaml up -d`.
4. Verify the Docker coexistence route at
   `https://text2shop.omv.mousses.xyz:8443`.

## Acceptance record

- Cutover timestamp: pending
- Docker stopped/verified: pending
- Data-copy result: not applicable; no persistent mounts
- Rollout/readiness result: pending
- Digest/TLS/internal URL results: pending
- Restart result: pending
- NetworkPolicy result: pending
- Rollback exercised: pending; documented stateless rollback remains available
- Accepted by and follow-up date: pending; retain the stopped Compose definition
  and local image until the migration retention window expires
