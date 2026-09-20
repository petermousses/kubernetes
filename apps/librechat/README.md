# librechat onboarding

this directory deploys a single-replica LibreChat instance in the `librechat`
namespace.

the stack is deliberately limited to:

- LibreChat API/UI, pinned to `v0.8.7`
- MongoDB for users, sessions, conversations, and application state
- Meilisearch for conversation search
- the existing Ollama service at `tower.mousses.xyz:11434`

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
4. `tower.mousses.xyz` resolves to the existing Ollama host, currently
   `10.9.20.7`, and Ollama serves the OpenAI-compatible API on TCP `11434`.
5. create the `librechat-env` Secret. Do not commit it:

   ```sh
   kubectl create namespace librechat --dry-run=client -o yaml | kubectl apply -f -

   kubectl -n librechat create secret generic librechat-env \
     --from-literal=CREDS_KEY="$(openssl rand -hex 32)" \
     --from-literal=CREDS_IV="$(openssl rand -hex 16)" \
       --from-literal=JWT_SECRET="$(openssl rand -hex 32)" \
       --from-literal=JWT_REFRESH_SECRET="$(openssl rand -hex 32)" \
       --from-literal=ALLOW_REGISTRATION=true \
       --from-literal=MEILI_MASTER_KEY="$(openssl rand -base64 32)" \
       --dry-run=client -o yaml | kubectl apply -f -
     ```

   `CREDS_KEY` and `CREDS_IV` are persistent encryption material. Do not
   regenerate them after the instance has stored credentials. `MEILI_MASTER_KEY`
   must be at least 16 bytes and must remain the same for LibreChat and
   Meilisearch. The current Ollama endpoint does not require an external API
   key. Add provider-specific keys to this Secret only when adding providers to
   `librechat.yaml`.

## deploy

run from the repository root:

```sh
kubectl kustomize apps/librechat
kubectl apply -k apps/librechat

kubectl -n librechat rollout status statefulset/mongodb --timeout=180s
kubectl -n librechat rollout status statefulset/meilisearch --timeout=180s
kubectl -n librechat rollout status deployment/librechat --timeout=180s
```

the first user can register through the web UI at
     <https://librechat.omv.mousses.xyz>. Treat that account as the instance
     administrator. After it exists, set `ALLOW_REGISTRATION=false` in the
     Secret and restart the API to prevent further self-service registration:

     ```sh
     kubectl -n librechat patch secret librechat-env \
       --type=merge \
       --patch='{"stringData":{"ALLOW_REGISTRATION":"false"}}'
     kubectl -n librechat rollout restart deployment/librechat
     ```

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

## configuration changes

the user-facing LibreChat configuration is the `librechat.yaml` key in
[`configmap.yaml`](configmap.yaml). It currently exposes the local Ollama
endpoint and defaults to `llama3.2:3b` while allowing LibreChat to fetch the
available model list.

the file is mounted with `subPath`, so restart the API after changing it:

```sh
kubectl -n librechat rollout restart deployment/librechat
kubectl -n librechat rollout status deployment/librechat --timeout=180s
```

the same restart is required after changing `librechat-env` values used by the
API. Restart Meilisearch too when changing `MEILI_MASTER_KEY`:

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
| LibreChat API | existing Ollama host `10.9.20.7` | TCP 11434 |

adding an external model provider, web-search service, RAG API, MCP server, or
other outbound integration requires a matching egress rule in
[`networkpolicy.yaml`](networkpolicy.yaml). Do not open broad public egress by
default.

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
- [LibreChat Helm values](https://github.com/danny-avila/LibreChat/blob/main/helm/librechat/values.yaml)
