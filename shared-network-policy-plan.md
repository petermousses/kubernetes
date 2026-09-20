# references

1. [Kubernetes: declarative management with Kustomize](https://kubernetes.io/docs/tasks/manage-kubernetes-objects/kustomization/) — bases, overlays, resources, and namespace transforms; its example references a sibling base directory as `../base`.
2. [Kustomize: first kustomization](https://github.com/kubernetes-sigs/kustomize/blob/master/site/content/en/docs/Getting%20started/first_kustomization.md) — composing reusable bases with overlays.
3. [Kustomize recognized filenames](https://github.com/kubernetes-sigs/kustomize/blob/master/api/konfig/general.go) — recognized kustomization filenames and one recognized file per directory.
4. [kubectl kustomize reference](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_kustomize/) — directory build targets and the default `LoadRestrictionsRootOnly` loader.
5. [Kubernetes: NetworkPolicies](https://kubernetes.io/docs/concepts/services-networking/network-policies/) — namespace scope, selectors, default deny, DNS, additive allows, and the requirement for both source egress and destination ingress to permit a connection.
6. [Kubernetes recommended labels](https://kubernetes.io/docs/concepts/overview/working-with-objects/common-labels/) — `app.kubernetes.io/part-of` and `app.kubernetes.io/component` identify an application and its architectural components.

# shared network policy plan

**status:** implemented in manifests and CI; live cluster validation is pending.

## option a

Each `apps/<app>/kustomization.yaml` remains a deployable entrypoint and composes reusable policy directories under `apps/_shared/network-policy/`. Kustomize supports sibling base directories such as `../base` ([1], [2]) while preserving the default root-only loader. A direct parent-file reference is rejected by that loader ([4]).

“Shared” means one source rendered into each consuming namespace. NetworkPolicies remain namespace-scoped objects ([5]).

## implemented structure

```text
apps/
  _shared/
    network-policy/
      baseline/
        kustomization.yaml
        default-deny.yaml
      dns-egress/
        kustomization.yaml
        allow-dns-egress.yaml
  <app>/
    kustomization.yaml
    networkpolicy.yaml  # app-specific policies
scripts/
  validate-network-policies.rb
.github/workflows/
  validate-network-policies.yaml
```

Each shared directory has one recognized `kustomization.yaml` and owns its policy as a unit ([3]). Consumers reference the directory, not an individual file:

```yaml
namespace: immich
resources:
  - namespace.yaml
  - ../_shared/network-policy/baseline
  - ../_shared/network-policy/dns-egress
  - networkpolicy.yaml
```

Kustomize composition is still not an admission boundary: a consumer can intentionally remove a base or add another allow policy. The repository validator therefore requires the baseline reference and validates the rendered invariant. Cluster admission policy would be required to prevent out-of-repository changes.

## app policy matrix

Ordered from the fewest to the most exceptions from default deny. This ranks policy composition, not directly comparable network reach; a single broad egress rule can permit more traffic than several narrow component rules.

| order | apps | shared policies | app-local access |
| ---: | --- | --- | --- |
| 1 | External Routes | baseline | none; default deny only |
| 2 | IT-Tools, Kiwix, OpenSpeedTest, QR Code Generator, Text2Shop | baseline | Traefik ingress only |
| 3 | Board Games, Cloudflare, Crafty, Homepage, Jellyfin, n8n, Open WebUI, Syncthing, Vaultwarden | baseline | custom ingress and/or egress, including app-local DNS where needed |
| 4 | Immich, LibreChat, Paperless-ngx, SearXNG | baseline + DNS egress | custom ingress, egress, and component flows |
| 5 | Rancher | none | no rendered NetworkPolicy; networking is delegated to Helm-generated resources |

No app currently has a custom rendered NetworkPolicy without the shared baseline. Rancher is unclassified at the policy layer rather than an intentional unrestricted custom profile.

## baseline

The shared baseline selects every pod and isolates both ingress and egress. Nineteen of the 20 app entrypoints include it.

`rancher` is the explicit exception. Its Kustomize entrypoint renders a `HelmChart` controller object in `kube-system`; the controller later creates workloads in `cattle-system`. Those generated workloads and required flows are absent from this repository's rendered output, so applying default deny there without a chart-level traffic audit would be unsafe.

`cloudflare` now receives the baseline in addition to its existing explicit cloudflared egress policy. `external-routes` receives the baseline but currently renders no pods, so it establishes the namespace default for any future workload.

The cluster network plugin must implement Kubernetes NetworkPolicy for these resources to have an effect ([5]).

## DNS

The shared DNS profile is used by `immich`, `librechat`, `paperless-ngx`, and `searxng`. LibreChat needs DNS for its in-namespace MongoDB and Meilisearch Services and its configured Ollama hostname. These policies are semantically identical: kube-dns pods on TCP/UDP 53, the `10.43.0.10/32` service IP on TCP/UDP 53, and the `10.42.0.0/16` pod CIDR on TCP/UDP 53.

The eight workload-scoped DNS policies remain app-local because moving them to an all-pod shared selector would broaden access. Board Games also remains app-local because its DNS destination set is narrower. Default-deny egress blocks DNS unless an additive policy permits it ([5]).

## component and ingress flows

No generic component-flow bundle was created. The audited applications do not share one honest selector-and-port contract:

- Board Games uses web → API → PostgreSQL plus a migration → PostgreSQL edge.
- Immich uses server → PostgreSQL, Redis, and machine learning.
- Paperless-ngx uses web, database, cache, converter, extractor, AI, and GPT components with different edges.
- SearXNG uses SearXNG → Valkey.

LibreChat uses Traefik → API, API → MongoDB, API → Meilisearch, and API → the existing Ollama host. These flows remain app-local because their selectors and ports are specific to LibreChat.

With ingress and egress isolation, each internal connection still needs permission from the source egress side and destination ingress side ([5]). Those rules remain in each app's `networkpolicy.yaml`, as do Traefik, public egress, LAN, peer, and dependency rules. Reuse would require broader selectors or ports, which would weaken the current policy.

The existing `app.kubernetes.io/part-of` and `app.kubernetes.io/component` labels remain useful identifiers ([6]), but matching label names alone do not establish a reusable traffic contract.

## namespace handling

Every baseline consumer now declares its namespace in the app kustomization. The shared sources contain no app-specific namespace, and the parent namespace transformer places each rendered policy correctly ([1]).

`rancher` deliberately has no kustomization-level namespace because that would rewrite its `HelmChart` object from `kube-system` to `cattle-system`. Its manifests keep explicit namespaces.

A semantic before/after comparison of all rendered objects passed. Existing objects were unchanged; the only additions were `default-deny-all` in `cloudflare` and `external-routes`.

## automated checks

`scripts/validate-network-policies.rb` renders all 20 app entrypoints and both shared bundles with `kubectl kustomize`. It fails when:

- a required app removes the shared baseline reference;
- Rancher gains the baseline without updating the explicit exception contract;
- a baseline is missing, duplicated, placed in the wrong namespace, or changes from deny-all ingress and egress;
- a shared DNS consumer or rendered DNS policy diverges from the approved profile;
- any app renders duplicate resource identities; or
- any Kustomize build fails.

`.github/workflows/validate-network-policies.yaml` runs the validator for pull requests and pushes to `develop`, using kubectl `v1.34.1`, matching the local implementation toolchain.

## completed and pending

- [x] Compare repeated default-deny, DNS, and Traefik policies field by field.
- [x] Move the identical default-deny policy to one shared baseline.
- [x] Move only the identical all-pod DNS policies to one opt-in bundle.
- [x] Keep non-identical DNS, component, Traefik, and other allow rules app-local.
- [x] Normalize namespaces for baseline consumers.
- [x] Render all 20 app entrypoints.
- [x] Compare pre-change and post-change manifests semantically.
- [x] Add a CI invariant check and exercise its missing-baseline failure path.
- [ ] Apply through the normal deployment workflow.
- [ ] Confirm workload startup, DNS, ingress, internal component traffic, and required egress from live logs.
- [ ] Revisit Rancher only after chart-generated workloads and required traffic are audited.
