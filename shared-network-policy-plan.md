# references

1. [Kubernetes: declarative management with Kustomize](https://kubernetes.io/docs/tasks/manage-kubernetes-objects/kustomization/) — bases, overlays, resources, and namespace transforms; its example references a sibling base directory as `../base`.
2. [Kustomize: first kustomization](https://github.com/kubernetes-sigs/kustomize/blob/master/site/content/en/docs/Getting%20started/first_kustomization.md) — composing reusable bases with overlays.
3. [Kustomize recognized filenames](https://github.com/kubernetes-sigs/kustomize/blob/master/api/konfig/general.go) — recognized kustomization filenames and one recognized file per directory.
4. [kubectl kustomize reference](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_kustomize/) — directory build targets and the default `LoadRestrictionsRootOnly` loader.
5. [Kubernetes: NetworkPolicies](https://kubernetes.io/docs/concepts/services-networking/network-policies/) — namespace scope, selectors, default deny, DNS, additive allows, and the requirement for both source egress and destination ingress to permit a connection.
6. [Kubernetes recommended labels](https://kubernetes.io/docs/concepts/overview/working-with-objects/common-labels/) — `app.kubernetes.io/part-of` and `app.kubernetes.io/component` identify an application and its architectural components.

# shared network policy plan

**status:** draft for joint editing. this plan changes no manifests.

## recommendation: option a

Keep each existing `apps/<app>/kustomization.yaml` as the deployable entrypoint. Put reusable policy bundles under `apps/_shared/network-policy/`. This gives the repository shared policy sources without changing app build paths; option b would add a source/overlay reorganization but no extra policy-composition capability.

Kustomize can reference a sibling **base directory** containing its own kustomization file. The upstream guide demonstrates `../base` ([1]). In a temporary fixture, `kubectl v1.34.1` with embedded Kustomize `v5.7.1` built both the option-a and option-b sibling-base shapes using the default loader. A direct resource-file reference such as `../networkpolicy.yaml` is different and is rejected by the default root-only loader ([4]).

“shared” means shared source rendered into each target app namespace, not one cluster-wide NetworkPolicy object. NetworkPolicies are namespace-scoped ([5]).

## proposed structure

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
      component-flow/
        kustomization.yaml
        frontend-egress-to-backend.yaml
        backend-ingress-and-egress.yaml
        database-ingress-from-backend.yaml
  immich/
    kustomization.yaml
    networkpolicy.yaml  # app-specific rules after the split
    ...
  paperless-ngx/
    kustomization.yaml
    networkpolicy.yaml  # app-specific rules after the split
    ...
```

Each app entrypoint lists the baseline directory as a resource. It lists the DNS and component-flow directories only when those profiles fit:

```yaml
resources:
  - namespace.yaml
  - deployment.yaml
  - ../_shared/network-policy/baseline
  # include per app when required:
  - ../_shared/network-policy/dns-egress
  - ../_shared/network-policy/component-flow
  - networkpolicy.yaml
```

Each bundle has its own recognized `kustomization.yaml` and lists all policies in that bundle. A consumer includes a bundle directory as a unit; it does not select individual files from inside that base. Kustomize still permits intentional changes in the consuming kustomization, and a consumer can omit the base entirely. This is a maintainable composition convention, not an enforcement boundary. If policy presence or allowed-flow limits must be mandatory, enforce them with repository CI or admission policy.

Kustomize discovers `kustomization.yaml`, `kustomization.yml`, or `Kustomization`, and only one recognized kustomization file per directory ([3]). Do not place sibling files named `app-a-kustomization.yaml` and `app-b-kustomization.yaml` and expect the build command to select one; `kubectl kustomize` takes a directory ([4]).

## policy profiles

### required baseline: default deny

- The target is for every app entrypoint to include the shared baseline.
- The baseline contains default-deny ingress and egress for all pods in that app namespace.
- Audit the rollout to Rancher and External Routes, which currently have no `networkpolicy.yaml`; check their controller/workload traffic before applying the baseline.
- Default-deny egress also blocks DNS. Workloads that need DNS must include an allow-DNS profile ([5]).
- The cluster network plugin must enforce Kubernetes NetworkPolicies or the resources have no effect ([5]).

### optional DNS egress

- Keep DNS permission in a separate bundle so apps that do not need it do not receive that allow rule.
- Compare DNS selectors, namespace labels, ports, and any IP blocks in the 11 current `allow-dns-egress` resources before consolidating them.
- Scope the rule to the intended pods. A `podSelector: {}` permits DNS egress for every pod in that policy's namespace.

### component-flow profile

For a multi-component app, share a narrow flow graph such as frontend → backend → database when its labels and ports fit the shared contract. Use pod-template labels such as `app.kubernetes.io/part-of` and `app.kubernetes.io/component`; the latter is intended to identify an architectural component ([6]). These are recommended labels, not labels Kubernetes adds automatically ([6]).

With ingress and egress both default-denied, each edge needs both sides allowed:

- frontend egress to backend on the backend's required port, and backend ingress from frontend on that port;
- backend egress to database on the database's required port, and database ingress from backend on that port.

The bundle should encode only those edges and required ports. Keep flows app-specific when ports, component labels, namespaces, or dependencies differ; do not broaden ports or selectors just to reuse YAML. NetworkPolicy rules are additive, so the rendered policy set is the effective union of allows ([5]).

The current Immich policy already expresses component-to-component traffic for server, database, Redis, and machine learning components, with explicit ports. Its pod templates carry `app.kubernetes.io/component` labels ([Immich policies](apps/immich/networkpolicy.yaml), [server](apps/immich/server-deployment.yaml), [database](apps/immich/database-deployment.yaml), [Redis](apps/immich/redis-deployment.yaml), [machine learning](apps/immich/ml-deployment.yaml)). Use it as a concrete candidate when deciding whether a stable shared contract exists; do not assume its app-specific rules can be generalized unchanged.

### Traefik and remaining app-specific flows

- Compare selectors, source namespace/pod selectors, and destination ports across the 15 current `allow-traefik-ingress` resources. Keep ingress app-local unless the selector and port contract are truly common.
- Keep database/cache dependencies, public egress, peer traffic, and other app-specific requirements local unless the audit proves they fit a shared profile.
- Preserve any exceptions explicitly. Do not treat repeated resource names as evidence that rule bodies are identical.

## current repository facts

- The repository has 19 app `kustomization.yaml` files and 17 app `networkpolicy.yaml` files.
- All 17 policy files are listed by their app's kustomization. `apps/rancher/` and `apps/external-routes/` currently have no `networkpolicy.yaml`.
- By resource name, 16 policies are named `default-deny-all`, 15 `allow-traefik-ingress`, and 11 `allow-dns-egress`. These counts do **not** establish that the rules are semantically identical.
- Other policies contain app-specific flows. Examples: [Board Games](apps/board-games/networkpolicy.yaml), [Paperless-ngx](apps/paperless-ngx/networkpolicy.yaml), and [SearXNG](apps/searxng/networkpolicy.yaml).
- Namespace handling is mixed: Board Games, Homepage, SearXNG, and External Routes set `namespace:` in their kustomization; many policy manifests instead set `metadata.namespace` directly.

## namespace handling

Every rendered shared NetworkPolicy must land in the intended app namespace. Decide this while reviewing each app build: preserve explicit `metadata.namespace` where appropriate, or use the app kustomization's `namespace:` transform only after checking all namespaced resources it affects. Kustomize's namespace setting is cross-cutting, not limited to NetworkPolicies ([1]).

Keep shared policy sources free of app-specific namespace assumptions. The selected composition must render one policy instance in each target namespace and must not move unrelated resources across namespaces.

## implementation and review sequence

1. [ ] Confirm the baseline target is every app and identify rollout exceptions, including Rancher and External Routes.
2. [ ] Compare the repeated default-deny, DNS, and Traefik rules field by field; document real differences before moving YAML.
3. [ ] Define the component label and port contract, then map each candidate app's actual flows to it. Put only matching flows in the component-flow bundle.
4. [ ] Add `apps/_shared/network-policy/{baseline,dns-egress,component-flow}/` and split approved common policies from app-specific policies.
5. [ ] Migrate one representative app with a default-deny baseline, then one DNS opt-in and one component-flow app. Render each and compare with its pre-change output.
6. [ ] Build all 19 app entrypoints with the repository's deployment toolchain. Check namespaces, selectors, peers, ports, policy types, and the full set of additive allows.
7. [ ] Add a CI check that every required app entrypoint includes the baseline and that rendered policies meet the agreed flow constraints.
8. [ ] Review all per-app diffs; deploy only through the repository's normal workflow.

## acceptance criteria

- Existing `kubectl kustomize apps/<app>` and `kubectl apply -k apps/<app>` entrypoints remain valid.
- Every in-scope app renders the shared default-deny baseline in its intended namespace.
- Only apps that need DNS include the DNS profile.
- Component-flow profiles allow only the agreed component edges and ports; any app that does not fit remains explicit and app-specific.
- Rendered policies preserve current required traffic and do not introduce broader selectors, ports, or destinations.
- CI detects a missing baseline reference and checks the agreed rendered-policy constraints.
- Builds and diffs are reviewed before deployment; build success alone is not evidence of equivalent network behavior.

## decisions for our next edit

- Should Rancher and External Routes join the default-deny baseline after their traffic is audited, or remain documented exceptions?
- Which apps actually match the shared frontend/backend/database labels and port contract?
- Should Traefik ingress remain app-specific, or does the audit establish a truly common selector/port rule?
- What namespace strategy preserves current cross-namespace resources while assigning shared policies correctly?
- What CI rule is sufficient to flag removal of the baseline and unintended additional allows?
