# librechat onboarding

this directory deploys a single-replica LibreChat instance in the `librechat`
namespace at `chat.omv.mousses.xyz`.

the stack is deliberately limited to:

- LibreChat API/UI, pinned to `v0.8.7`
- MongoDB for users, sessions, conversations, and application state
- Meilisearch for conversation search
- Authentik OIDC and a restricted LiteLLM key for the static chat models and
  the standard Qwen Image tool

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

3. `chat.omv.mousses.xyz` resolves to the Traefik entrypoint. The
   [`Certificate`](certificate.yaml) uses the existing
   `letsencrypt-production-cloudflare` ClusterIssuer.
4. the Authentik OIDC discovery URL returns HTTP 200 through Traefik's
   `10.43.204.62` ClusterIP, and `librechat-oidc` exists in the `librechat`
   namespace. LibreChat maps the Authentik hostname to that Service IP inside
   its pod; the public hostname remains the TLS identity and browser DNS is
   unaffected. Verify the actual Service IP before applying the manifests.
5. add your Authentik user to `librechat_users` and `librechat_admin`. Prepare
   a non-admin user in `librechat_users` and one outside that group for tests.
   The provider's ID token must contain the `groups` claim.
6. create a LiteLLM virtual key limited to chat models `qwen3.8-27b`,
   `gemma-4-e4b-it`, `gemma-4-12b-it`, `gemma-4-26b-a4b-it`, and
   `qwen3.6-35b-a3b`, plus the standard image model `qwen-image-2.1`, with
   `allowed_routes` limited to `/v1/models` (used with `GET`),
   `/v1/chat/completions`, `/v1/images/generations`, and `/v1/images/edits`.
   Do not add `qwen-image-2.1-uncensored` to the shared LibreChat key: LibreChat
   has one global OpenAI image-tool model setting, which remains the standard
   `qwen-image-2.1`. Use a separate explicitly scoped key for direct API tests
   of the uncensored alias. Never use the master key. Use the
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

   After deployment, if the LiteLLM key needs a different model scope, create a
   replacement least-privilege virtual key in LiteLLM and run
   `./apps/librechat/replace-litellm-key.sh`. It prompts without echo, patches
   only `LITELLM_API_KEY`, preserves all persistent encryption keys, and restarts
   LibreChat.

## deploy

run from the repository root. Stop if the Service IP check or discovery request
fails; update the policy and pod mapping together before deploying:

```sh
traefik_service_ip="$(kubectl -n kube-system get svc traefik -o jsonpath='{.spec.clusterIP}')"
test "${traefik_service_ip}" = 10.43.204.62
curl --resolve auth.omv.mousses.xyz:443:"${traefik_service_ip}" \
  -fsS -o /dev/null -w 'authentik via traefik ClusterIP: %{http_code}\n' \
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
curl -fsS https://chat.omv.mousses.xyz/health
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
[`configmap.yaml`](configmap.yaml). the custom endpoint uses `models.fetch: true`
to populate the chat picker from LiteLLM; `models.default` retains only
`qwen3.8-27b` as the required fallback if discovery fails. its LiteLLM virtual
key must allow `/v1/models` or discovery fails; the key's model scope, not
the fallback, enforces inference access. qwen image remains an Agent tool, not a
chat-model selector entry; its single global image-tool setting stays on
`qwen-image-2.1`. The uncensored image alias is exposed through LiteLLM for
direct API evaluation with a separately scoped virtual key, not through the
shared LibreChat image tool. JevK5 is a typed decision route, not a chat model.

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

the kustomization includes the shared default-deny, DNS, and opt-in
[Authentik OIDC egress](../_shared/network-policy/authentik-oidc-egress/allow-authentik-oidc-egress.yaml)
profiles. Only the API pod carries `mousses.xyz/authentik-oidc-client: "true"`;
MongoDB and Meilisearch do not gain this egress. The app-local policy and
shared profile permit only these flows:

| source | destination | port |
| --- | --- | ---: |
| Traefik in `kube-system` | LibreChat API | TCP 3080 |
| LibreChat API | MongoDB | TCP 27017 |
| LibreChat API | Meilisearch | TCP 7700 |
| LibreChat API | LiteLLM gateway in `litellm` | TCP 4000 |
| LibreChat API | Traefik `10.43.204.62` Service IP | TCP 443 |
| LibreChat API | Traefik `kube-system` pods after Service translation | named `websecure` port |

to give another deployment server-side Authentik OIDC access, add
`../_shared/network-policy/authentik-oidc-egress` to its kustomization, label
only the OIDC-calling pod template with
`mousses.xyz/authentik-oidc-client: "true"`, and map
`auth.omv.mousses.xyz` to `10.43.204.62` in that pod's `hostAliases`. Keep the
public issuer URL in the app's OIDC settings for valid TLS/SNI. This is only
network access; the app still needs its own Authentik provider, credentials,
and user authorization. If Traefik's Service ClusterIP changes, update the
shared policy and each adopter's `hostAliases` together. Kubernetes does not
guarantee whether NetworkPolicy sees a Service IP before or after translation,
so the shared policy explicitly permits both the Service VIP and the selected
Traefik backend pods. Verify discovery from inside each newly deployed client
pod; a host-side HTTP 200 does not prove that pod egress works.
This L3/L4 policy cannot limit the HTTP hostname: an opted-in pod can also
reach other virtual hosts served on Traefik's HTTPS port.

adding an external model provider, web-search service, RAG API, MCP server, or
other outbound integration requires a matching egress rule in
[`networkpolicy.yaml`](networkpolicy.yaml). Do not open broad public egress by
default.

Open WebUI currently owns `chat.omv.mousses.xyz` in the live cluster, while
this repository now assigns that host to LibreChat. They must never have
competing routes. The owner approved deleting Open WebUI completely, including
its retained PV and local data, rather than keeping a rollback copy. Before
applying this cutover, delete namespace `open-webui`, PV `open-webui-data`,
StorageClass `open-webui-local`, and the exact node directory
`/filesystem/k3s/data/open-webui`; then apply the LibreChat kustomization so
cert-manager issues `chat.omv.mousses.xyz` for LibreChat. DNS already targets
Traefik, so this is an ingress/certificate switch, not a DNS change. There is
no redirect from the former `librechat.omv.mousses.xyz` hostname.

run the destructive Open WebUI retirement from the repository root on the NAS.
this permanently deletes its settings and history; verify the targets before
running it:

```sh
set -euo pipefail

kubectl -n open-webui get pods,pvc
kubectl get pv open-webui-data -o wide
kubectl delete namespace open-webui --wait=true --ignore-not-found
test -z "$(kubectl get pvc --all-namespaces \
  --field-selector spec.volumeName=open-webui-data -o name)"
kubectl delete pv open-webui-data --ignore-not-found
kubectl delete storageclass open-webui-local --ignore-not-found

webui_data=/filesystem/k3s/data/open-webui
if [ -d "${webui_data}" ]; then
  resolved_data="$(realpath -e -- "${webui_data}")"
  test "${resolved_data}" = "${webui_data}"
  sudo rm -rf --one-file-system -- "${resolved_data}"
fi

kubectl apply -k apps/librechat
kubectl -n librechat rollout status deployment/librechat --timeout=180s
kubectl -n librechat get certificate,ingress
```

stop if the resolved directory differs from the path above or if deleting the
namespace/PV reports another owner. do not run the LibreChat apply until the
retained Open WebUI PV and directory are gone; their old ingress must not
compete for the shared hostname.

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
