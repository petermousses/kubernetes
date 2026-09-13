# references

1. [Kubernetes: declarative management with Kustomize](https://kubernetes.io/docs/tasks/manage-kubernetes-objects/kustomization/) — `resources`, bases and overlays, namespace transforms, and `kubectl apply -k`.
2. [Kustomize: first kustomization](https://github.com/kubernetes-sigs/kustomize/blob/master/site/content/en/docs/Getting%20started/first_kustomization.md) — canonical `kustomization.yaml` files and separate overlays that reuse a base.
3. [Kustomize recognized filenames](https://github.com/kubernetes-sigs/kustomize/blob/master/api/konfig/general.go) — the three discovered filenames and the one-file-per-directory rule.
4. [kubectl kustomize reference](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_kustomize/) — build input is a directory; `LoadRestrictionsRootOnly` is the default.
5. [Kubernetes: NetworkPolicies](https://kubernetes.io/docs/concepts/services-networking/network-policies/) — policies apply to pods in a given namespace; `namespaceSelector` selects peer namespaces.

# shared network policy and repository structure plan

**status:** draft for joint editing. this file plans the change; it does not change any manifests.

## goal

Reuse genuinely common NetworkPolicy rules while keeping each app independently buildable. Decide whether to preserve the existing `apps/<app>/` build paths or separate app manifests from per-app overlay entrypoints. Render the shared policy into each selected app namespace, and keep app-specific traffic requirements explicit.

“shared” means shared source/configuration here, not one cluster-wide NetworkPolicy object: NetworkPolicies are namespace-scoped ([5]). Kustomize bases can be included by separate overlays, as in the official `../base` example ([1], [2]).

## current repository facts

- The repository has 19 app `kustomization.yaml` files and 17 app `networkpolicy.yaml` files.
- All 17 policy files are listed by their app’s kustomization. `apps/rancher/` and `apps/external-routes/` currently have no `networkpolicy.yaml`.
- By resource name, 16 policies are named `default-deny-all`, 15 `allow-traefik-ingress`, and 11 `allow-dns-egress`. These counts do **not** establish that the rules are semantically identical.
- Policies also contain app-specific flows. Examples include [Immich](apps/immich/networkpolicy.yaml), [Board Games](apps/board-games/networkpolicy.yaml), [Paperless-ngx](apps/paperless-ngx/networkpolicy.yaml), and [SearXNG](apps/searxng/networkpolicy.yaml).
- Namespace handling is mixed: `apps/board-games/`, `apps/homepage/`, `apps/searxng/`, and `apps/external-routes/` set `namespace:` in their kustomization; many policy manifests instead set `metadata.namespace` directly.

## structure options to compare

### option a: keep app directories as build entrypoints

This is the smaller migration: retain each existing `apps/<app>/kustomization.yaml`, and put reusable policy sources in a dedicated base directory:

```text
apps/
  _shared/
    network-policy/
      kustomization.yaml
      default-deny.yaml          # only if approved as common
      allow-dns-egress.yaml      # only if approved as common
  immich/
    kustomization.yaml
    networkpolicy.yaml           # app-specific rules after the split
    ...
  paperless-ngx/
    kustomization.yaml
    networkpolicy.yaml           # app-specific rules after the split
    ...
```

Each opted-in app would reference `../_shared/network-policy` from its own `kustomization.yaml`, alongside its local resources. The shared directory must have its own recognized kustomization file to be used as a base ([1], [2]). This preserves `kubectl kustomize apps/<app>` and `kubectl apply -k apps/<app>` as per-app entrypoints ([1]).

### option b: separate app manifests from build entrypoints

This is a larger restructure and closer to the proposed `app-a` / `app-b` layout. Give every resource directory and every build entrypoint its own canonical kustomization:

```text
apps/
  app/
    immich/
      kustomization.yaml
      namespace.yaml
      server-deployment.yaml
      networkpolicy.yaml
      ...
    paperless-ngx/
      kustomization.yaml
      namespace.yaml
      paperless.yaml
      networkpolicy.yaml
      ...
  shared/
    network-policy/
      kustomization.yaml
      default-deny.yaml
      allow-dns-egress.yaml
  overlays/
    immich/
      kustomization.yaml
    paperless-ngx/
      kustomization.yaml
```

Each `apps/overlays/<app>/kustomization.yaml` would include that app directory and the shared policy base, then apply any app-level namespace or other customization. The app and shared directories each need a kustomization file to be referenced as bases ([1], [2]). The build command would target `apps/overlays/<app>/` ([4]). This separates source manifests from deployable variants, but changes the current build paths and requires updating any external deployment references.

Do not put several files named `<app>-kustomization.yaml` beside each other and expect Kustomize to select one by filename. Kustomize accepts `kustomization.yaml`, `kustomization.yml`, or `Kustomization`, and allows only one recognized match per directory ([3]); the build argument is a directory ([4]).

## policy split to decide

Treat the repeated names as audit candidates, not copy/paste proof:

- **Default deny:** compare selectors and `policyTypes` across the 16 current `default-deny-all` resources. Decide whether it belongs in the shared base and identify the app without one.
- **DNS egress:** compare DNS destinations, selectors, ports, and IP blocks in the 11 current `allow-dns-egress` resources. Decide whether this is universal or opt-in. NetworkPolicy rules are additive, so the rendered result must be checked as a whole ([5]).
- **Traefik ingress:** compare pod selectors, source namespace/pod selectors, and destination ports in the 15 current `allow-traefik-ingress` resources. Keep differences app-local unless a safe shared selector and port contract is established.
- **App-specific rules:** retain database, cache, dependency, peer, public egress, and app-service flows locally unless review proves they are common. Do not broaden a selector just to make manifests look uniform.
- **Apps without policies:** decide separately whether Rancher or External Routes needs NetworkPolicy resources; do not opt them in by default.

The shared source should contain only approved common rules. Each app kustomization should opt in by referencing the shared base; its local policy file should contain only the remaining app-specific rules.

## namespace strategy to decide

The simplest candidate is to set `namespace: <app-namespace>` in each app kustomization so Kustomize assigns that namespace to the namespaced resources from both the app and shared base. The Kubernetes Kustomize guide demonstrates the namespace transform on resources composed from a base ([1], [2]). Because this repository currently mixes top-level namespace transforms with explicit `metadata.namespace` values, first check every affected resource for cross-namespace intent; `namespace:` is a cross-cutting transform, not a policy-only setting ([1]).

If that would rewrite unrelated resources, keep their current namespace fields and choose a policy-specific way to set the shared NetworkPolicies’ namespace. Record the chosen approach here before migration.

## implementation and review sequence

1. [ ] Agree on the shared policy scope, app opt-in list, and namespace strategy.
2. [ ] Compare the repeated rules field-by-field; record real differences and exceptions before moving YAML.
3. [ ] Add the shared base and split only the approved common rules from app-local policies.
4. [ ] Migrate one representative app first; review its rendered output against the current build before repeating.
5. [ ] Build all 19 app targets at the paths selected above and inspect the rendered NetworkPolicies for correct namespace, selectors, peers, policy types, and ports. Confirm no existing app resources disappear or gain broader access.
6. [ ] Review the final per-app diffs and then apply through the repository’s normal deployment workflow.

## acceptance criteria

- Every app has one clear, independently buildable entrypoint; if option b is chosen, update deployment references from the current `apps/<app>/` paths.
- Each selected app receives the approved shared policy rules in the intended namespace.
- App-specific flows and exceptions remain represented, with no selector or port broadened merely to enable reuse.
- Apps not selected for the shared baseline remain unchanged.
- Rendered manifests are compared before deployment; build success alone is not treated as proof of equivalent network behavior.

## decisions for our next edit

- Should the shared base contain only default deny, or default deny plus DNS egress?
- Is Traefik ingress truly common, or should it remain app-specific because selectors and service ports differ?
- Should all apps with a current policy opt into the base, or should opt-in be limited to apps whose rules match the approved baseline?
- Should per-app kustomizations use `namespace:`, or should the shared policy namespace be set without changing other resources?
- Should we choose option a (keep existing build paths) or option b (separate `apps/app/` sources and `apps/overlays/` entrypoints)?
- Should the shared directory be named `apps/_shared/network-policy/` or use another repo convention?
