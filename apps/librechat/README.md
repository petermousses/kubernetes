# librechat onboarding

this directory deploys a single-replica LibreChat instance in the `librechat`
namespace.

the staging stack is deliberately limited to:

- LibreChat API/UI, pinned to `v0.8.7`
- MongoDB for users, sessions, conversations, and application state
- Meilisearch for conversation search
- Authentik OIDC and a restricted LiteLLM key for Qwen chat and image tools

RAG/pgvector and the separate LibreChat admin-panel service are not deployed.
file search is disabled in [`configmap.yaml`](configmap.yaml) until the RAG
stack and an embeddings provider are added.

## prerequisites

before applying the kustomization, confirm all of the following:

1. `kubectl` can access the target cluster.
2. the `openmediavault` node exists and has these local paths:

   | path | workload | requested capacity |
   | --- | --- | ---: |
   | `/filesystem/k3s/data/librechat/mongodb` | MongoDB | 10 GiB |
   | `/filesystem/k3s/data/librechat/meilisearch` | Meilisearch | 10 GiB |
   | `/filesystem/k3s/data/librechat/data` | LibreChat `/app/data` | 2 GiB |
   | `/filesystem/k3s/data/librechat/uploads` | LibreChat `/app/uploads` | 10 GiB |
   | `/filesystem/k3s/data/librechat/images` | LibreChat images | 5 GiB |

   The PV sizes are Kubernetes requests; local PVs do not enforce filesystem
   quotas. Create the directories on `openmediavault` and make them writable
   by the container users before deployment:

   ```sh
   sudo install -d -o 999 -g 999 /filesystem/k3s/data/librechat/mongodb
   sudo install -d -o 1000 -g 1000 /filesystem/k3s/data/librechat/meilisearch
   sudo install -d -o 1000 -g 1000 /filesystem/k3s/data/librechat/data
   sudo install -d -o 1000 -g 1000 /filesystem/k3s/data/librechat/uploads
   sudo install -d -o 1000 -g 1000 /filesystem/k3s/data/librechat/images
   ```

3. `librechat.omv.mousses.xyz` resolves to the Traefik entrypoint. The
   [`Certificate`](certificate.yaml) uses the existing
   `letsencrypt-production-cloudflare` ClusterIssuer.
4. the Authentik OIDC discovery URL returns HTTP 200, and `librechat-oidc`
   exists in the `librechat` namespace. LibreChat pins the Authentik hostname
   to the NAS's `10.9.20.14` HTTPS entrypoint inside its pod; verify the NAS
   still serves `auth.omv.mousses.xyz` on TCP 443. Browser DNS is unaffected.
5. add your Authentik user to `librechat_users` and `librechat_admin`. Prepare
   a non-admin user in `librechat_users` and one outside that group for tests.
   The provider's ID token must contain the `groups` claim.
6. create a LiteLLM virtual key limited to models `qwen3.8-27b` and
   `qwen-image-2.1`, with `allowed_routes` limited to
   `/v1/chat/completions`, `/v1/images/generations`, and `/v1/images/edits`.
   Never use the master key. Use the
   [LiteLLM admin tunnel](../litellm/README.md) if needed. On the NAS, run
   this script and paste the virtual key at its hidden prompt:

   ```sh
   ./apps/librechat/bootstrap-secrets.sh
   ```

   `CREDS_KEY` and `CREDS_IV` are persistent encryption material. Do not
   regenerate them after the instance has stored credentials. Back up this
   Secret securely. `MEILI_MASTER_KEY` must remain the same for LibreChat and
   Meilisearch. Verify the new key rejects an unlisted model and a management
   route before trusting its scope.

## deploy

run from the repository root:

```sh
curl --resolve auth.omv.mousses.xyz:443:10.9.20.14 \
  -fsS -o /dev/null -w 'authentik via NAS HTTPS: %{http_code}\n' \
  https://auth.omv.mousses.xyz/application/o/librechat/.well-known/openid-configuration
kubectl -n librechat get secret librechat-oidc -o name
kubectl kustomize apps/librechat
kubectl apply -f apps/litellm/networkpolicy.yaml
kubectl apply -k apps/librechat

kubectl -n librechat rollout status statefulset/mongodb --timeout=180s
kubectl -n librechat rollout status statefulset/meilisearch --timeout=180s
kubectl -n librechat rollout status deployment/librechat --timeout=180s
```

do not register a local first user. Authentik's `librechat_admin` group grants
the LibreChat administrator role. Email registration is already disabled.

## verify

```sh
kubectl -n librechat get pods,svc,pvc,certificate,ingress
kubectl -n librechat logs deployment/librechat --tail=100
kubectl -n librechat logs statefulset/mongodb --tail=100
kubectl -n librechat logs statefulset/meilisearch --tail=100
curl -fsS https://librechat.omv.mousses.xyz/health
```

the API pod must be `Ready`, both statefulsets must be `Ready`, all five PVCs
must be `Bound`, the certificate must be `Ready=True`, and `/health` must
return successfully.

in separate private browser sessions, test all three Authentik users: the
admin group maps to LibreChat admin; `librechat_users` can chat but is not
admin; a user outside the group is denied. Verify logout and denial after
removing a user from the allowed group. Test streaming chat, vision input
and tool calls using `qwen3.8-27b`. For images, create an Agent with
**OpenAI Image Tools**, then generate a new image and edit an uploaded one.
The LiteLLM API validator did not exercise LibreChat's Agent-tool payloads.

only after OIDC passes, change `ALLOW_EMAIL_LOGIN` to `"false"` and add
`OPENID_AUTO_REDIRECT: "true"` in
[`configmap.yaml`](configmap.yaml), apply the kustomization and restart the
LibreChat deployment. `ALLOW_REGISTRATION` disables email sign-up; OIDC
admission is controlled by Authentik and LibreChat's required-role check.

## configuration changes

the user-facing LibreChat configuration is the `librechat.yaml` key in
[`configmap.yaml`](configmap.yaml). It exposes only the LiteLLM chat model.
Qwen Image is an Agent tool, not a chat-model selector entry. JevK5 is a typed
decision route, not a chat model.

the file is mounted with `subPath`, so restart the API after changing it:

```sh
kubectl -n librechat rollout restart deployment/librechat
kubectl -n librechat rollout status deployment/librechat --timeout=180s
```

the same restart is required after changing `librechat-env` or
`librechat-oidc` values used by the API. Restart Meilisearch too when changing
`MEILI_MASTER_KEY`:

```sh
kubectl -n librechat rollout restart statefulset/meilisearch
```

## network policy contract

the kustomization includes the shared default-deny and shared DNS profiles.
The app-local policy then permits only these flows:

| source | destination | port |
| --- | --- | ---: |
| Traefik in `kube-system` | LibreChat API | TCP 3080 |
| LibreChat API | MongoDB | TCP 27017 |
| LibreChat API | Meilisearch | TCP 7700 |
| LibreChat API | LiteLLM gateway in `litellm` | TCP 4000 |
| LibreChat API | Authentik via NAS HTTPS at `10.9.20.14` | TCP 443 |

adding an external model provider, web-search service, RAG API, MCP server, or
other outbound integration requires a matching egress rule in
[`networkpolicy.yaml`](networkpolicy.yaml). Do not open broad public egress by
default.

Open WebUI still owns `chat.omv.mousses.xyz`. Stage and test on
`librechat.omv.mousses.xyz`; the later hostname cutover requires a second
strict Authentik callback and an ingress/certificate change. Never apply
both apps with competing routes for `chat.omv.mousses.xyz`. Keep the stopped
Open WebUI data volume for 30 days after a tested cutover.

## deferred RAG support

to enable document RAG later, add the LibreChat RAG API, a PostgreSQL/pgvector
database, persistent storage for the vector database, an embeddings provider,
and the required provider credential. Then enable file search in
`librechat.yaml`, add the component-to-component network-policy edges, and
allow only the provider destinations required by the selected embeddings
configuration.

see the [LibreChat RAG API documentation](https://www.librechat.ai/docs/configuration/rag_api)
for the provider-specific requirements.

## upstream references

- [environment variables](https://www.librechat.ai/docs/configuration/dotenv)
- [Meilisearch](https://www.librechat.ai/docs/configuration/meilisearch)
- [custom endpoints](https://www.librechat.ai/docs/quick_start/custom_endpoints)
- [Authentik OIDC](https://www.librechat.ai/docs/configuration/authentication/OAuth2-OIDC/authentik)
- [image generation and editing](https://www.librechat.ai/docs/features/image_gen)
- [LibreChat Helm values](https://github.com/danny-avila/LibreChat/blob/main/helm/librechat/values.yaml)
