# authentik forward auth for an app

this directory supplies a namespaced traefik `Middleware` and an `ExternalName` service alias for authentik's embedded outpost. each app still needs its own authentik application/provider and a protected `IngressRoute`. [authentik's proxy-provider guide](https://docs.goauthentik.io/add-secure-apps/providers/proxy/create-proxy-provider/) describes the same split.

## add an app

1. in the app's `kustomization.yaml`, include `../_shared/ingress-authentik-forward-auth` and `../_shared/network-policy/traefik-http-ingress`. the kustomization must set the app namespace. remove any app-local `allow-traefik-ingress` manifest first, or kustomize will reject the duplicate resource. the shared manifest keeps that name, so applying it updates the existing policy. add `../_shared/network-policy/baseline` if the namespace does not already use it.
2. label the **backend pod template** `mousses.xyz/traefik-http-backend: "true"` and name its listening TCP container port `http`. the shared policy allows only pods labeled `app.kubernetes.io/name: traefik` in `kube-system` to reach that named port. the service can use any numeric port, but its `targetPort` must resolve to the pod's `http` port. see `apps/n8n/deployment.yaml` and `apps/n8n/service.yaml` for an example. for a different protocol or port name, write an app-specific policy instead of widening the shared one. [kubernetes defines named ports in network policies](https://kubernetes.io/docs/reference/kubernetes-api/networking/network-policy-v1/#NetworkPolicyPort).
3. in the authentik admin ui, go to **applications → applications → new application**. create the application with a **proxy provider**, select **forward auth (single application)**, and set **external host** to the exact public url, including `https://` (for example, `https://n8n.omv.mousses.xyz`). bind any intended access policies. then go to **applications → outposts**, edit **authentik embedded outpost**, add the new application, and update it. [provider setup](https://docs.goauthentik.io/add-secure-apps/providers/proxy/create-proxy-provider/) · [embedded outpost](https://docs.goauthentik.io/add-secure-apps/outposts/embedded/).
4. add an app `IngressRoute` on `websecure` with tls. route ``Host(`<host>`) && PathPrefix(`/outpost.goauthentik.io/`)`` to `authentik-outpost:http` **without** the auth middleware, at a higher priority. route the rest of the host to the app service with `authentik-forward-auth` middleware. `apps/n8n/ingress.yaml` is the working example. authentik requires the outpost path on the protected host for this mode. [authentik's traefik guide](https://docs.goauthentik.io/add-secure-apps/providers/proxy/server_traefik/). if homepage should discover the route, set its `gethomepage.dev/href` annotation to the app's full external url; homepage ignores `IngressRoute` objects without it. [homepage docs](https://gethomepage.dev/configs/kubernetes/#traefik-ingressroute-support).
5. remove any other `Ingress` or `IngressRoute` that serves the same host without auth. `kubectl apply -k` does **not** delete an old route that you removed from the manifests; delete that specific stale object after identifying it. check every entry point that can serve the host.
6. run `kubectl apply -k apps/<app>` from the repository root. this does not require a traefik redeploy. the cluster's traefik config already permits `ExternalName` services and resolves cluster service names before the `mousses.xyz` search suffix (`platform/traefik/helmchartconfig.yaml`). authentik already allows ingress from the `kube-system` traefik pods (`apps/authentik/networkpolicy.yaml`).

## verify

replace `<namespace>` and `<host>` below. the ping path should return `204`; a fresh private browser visit to the app should redirect to authentik before reaching the app. after authentication, the app might still require its own login because forward auth does not create an app-native sso session.

```sh
kubectl -n <namespace> get ingress,ingressroute
kubectl -n <namespace> get networkpolicy allow-traefik-ingress -o yaml
curl -sk -o /dev/null -w '%{http_code}\n' https://<host>/outpost.goauthentik.io/ping
```

webhook or api routes that cannot use a browser login need their own deliberate route and access controls; review those per app before exposing them.
