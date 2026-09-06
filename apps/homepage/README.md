# Homepage maintenance

Homepage is deployed in the `homepage` Kubernetes namespace. Its source
configuration lives in this directory and should be changed here rather than
by editing the live ConfigMap in the cluster.

## Configuration files

Most day-to-day changes belong in [`config/`](config/):

| File | Purpose |
| --- | --- |
| [`config/services.yaml`](config/services.yaml) | Application cards, links, site monitors, and service widgets |
| [`config/settings.yaml`](config/settings.yaml) | Page title, theme, background, layout, and general behavior |
| [`config/widgets.yaml`](config/widgets.yaml) | Header widgets such as weather, search, resources, and date/time |
| [`config/bookmarks.yaml`](config/bookmarks.yaml) | Bookmark groups and links |
| [`config/custom.css`](config/custom.css) | Custom styling |
| [`config/custom.js`](config/custom.js) | Custom browser-side behavior |
| [`config/kubernetes.yaml`](config/kubernetes.yaml) | Kubernetes integration settings |
| [`config/docker.yaml`](config/docker.yaml) | Docker integration settings |
| [`config/proxmox.yaml`](config/proxmox.yaml) | Proxmox integration settings |

Reusable, non-secret environment variables such as the base domain are in
[`configmap-public.yaml`](configmap-public.yaml).

Kubernetes resources such as the Deployment, Service, Ingress, certificates,
RBAC, and network policies are the other YAML files in this directory. The
[`kustomization.yaml`](kustomization.yaml) file combines all resources and
generates the `homepage-config` ConfigMap from the files under `config/`.

## Deploying a change

Run these commands from the repository root:

```sh
# Preview what Kubernetes would change.
kubectl diff -k k8s/apps/homepage

# Apply the manifests and regenerate the Homepage ConfigMap.
kubectl apply -k k8s/apps/homepage

# Reload configuration files mounted with subPath.
kubectl -n homepage rollout restart deployment/homepage

# Wait for the replacement pod to become ready.
kubectl -n homepage rollout status deployment/homepage --timeout=120s
```

The restart is required after changing files under `config/` because the
Deployment mounts each ConfigMap key using `subPath`. Changes to the Deployment
pod template trigger a rollout automatically, but running the restart command
is safe and keeps the update process consistent.

## Verification

Open [Homepage](https://home.omv.mousses.xyz/) and confirm the updated content.
The following commands provide additional checks:

```sh
kubectl -n homepage get pods
kubectl -n homepage logs deployment/homepage --tail=100
curl -fsS -o /dev/null -w '%{http_code}\n' https://home.omv.mousses.xyz/
```

A healthy deployment has one `Running` and `Ready` Homepage pod, and the HTTP
check returns `200`.

## Secrets

Do not commit passwords, API tokens, or other credentials to these files.
Homepage receives sensitive values from the `homepage-secrets` Kubernetes
Secret through `envFrom` in [`deployment.yaml`](deployment.yaml). Update that
Secret separately when credentials need to change.

