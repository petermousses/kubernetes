# LiteLLM deployment

deploy LiteLLM from the repository root with:

```sh
./apps/litellm/deploy.sh
```

run this script for every LiteLLM deployment instead of running
`kubectl apply -k apps/litellm` directly.

LiteLLM runs with automatic schema updates disabled. The script therefore
applies PostgreSQL and its network policies first, waits for PostgreSQL, stops
the gateway, recreates the version-pinned migration Job, and waits for the
migration to succeed before applying and restarting the gateway. A completed
Kubernetes Job does not rerun when its manifest is applied again, and
`kubectl apply` does not wait for that Job to complete before reconciling the
Deployment. Directly applying the kustomization can consequently start a new
LiteLLM version against an old database schema.

The script also prints retained migration logs and Job details on failure,
removes the obsolete Kubernetes Ingress left by the move to a Traefik
IngressRoute, waits for the new gateway Pod to become ready, and prints the
resulting resources.

Before the first deployment, create the uncommitted `litellm-env` Secret:

```sh
./apps/litellm/bootstrap-secrets.sh
```

The deploy script is intentionally idempotent. Rerunning the pinned migration
is safer than guessing whether a manifest edit requires it.

## Admin UI through Traefik and Authentik

The Admin UI uses the separate host `admin.ai.omv.mousses.xyz`; the public
inference host `api.ai.omv.mousses.xyz` remains limited to its exact inference
routes. LiteLLM's native generic OIDC integration handles the login and role
mapping; Traefik does not blindly forward-auth the Admin UI.

Before first deployment, create matching OIDC client Secrets in `authentik`
and `litellm`. Run this once from the repository root on the NAS; the generated
values are not printed:

```sh
./apps/authentik/bootstrap-litellm-admin-oidc-secrets.sh
```

The Authentik blueprint creates the confidential OIDC provider, the
`litellm_admin` group, and an application restricted to that group. Its custom
`litellm_role` claim maps that group to LiteLLM's `proxy_admin`; the proxy uses
`ui_access_mode: admin_only`. Add only authorized accounts to
`litellm_admin` in Authentik. The pinned LiteLLM version supports Admin UI SSO
for up to five users without an Enterprise license. See [LiteLLM's generic
SSO configuration](https://docs.litellm.ai/docs/proxy/admin_ui_sso).

After pulling the change, verify that `admin.ai.omv.mousses.xyz` resolves to
Traefik, then run both app deploy scripts. They apply the blueprint, TLS
Certificate, IngressRoute and OIDC egress policy. Confirm the certificate is
ready and test SSO using a user in `litellm_admin`; also verify an account
outside that group cannot open the Authentik application. The exact OIDC
callback is `https://admin.ai.omv.mousses.xyz/sso/callback`.

LiteLLM's master-key fallback login route `/fallback/login` is separately
restricted by Traefik to `10.9.20.0/24`. From outside that LAN, use the SSH
port-forward below for break-glass access. Do not broaden the fallback route
or put the master key in an Authentik user session.

## private break-glass administration

the Admin UI is not exposed through the public API hostname. retrieve its
master key on the NAS with:

```sh
kubectl -n litellm get secret litellm-env \
  -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 --decode
printf '\n'
```

or retrieve it from a workstation through the normal NAS SSH account:

```sh
ssh peter@openmediavault \
  "kubectl -n litellm get secret litellm-env \
    -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 --decode; printf '\\n'"
```

the master key is a root credential. store it in a password manager, never put
it in shell history or the repository, and use restricted virtual keys for
ordinary inference.

from the workstation, open one SSH connection that creates a local listener
and runs the Kubernetes port-forward on the NAS:

```sh
ssh -t \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -L 127.0.0.1:14000:127.0.0.1:14000 \
  peter@openmediavault \
  'kubectl -n litellm port-forward --address 127.0.0.1 service/litellm 14000:4000'
```

open <http://127.0.0.1:14000/ui>, choose the master-key fallback login, and
supply the master key when prompted. keep the SSH process in the foreground and press `Ctrl-C`
to terminate both forwarding layers. neither listener accepts non-loopback
connections. see the
[LiteLLM Admin UI quickstart](https://docs.litellm.ai/docs/proxy/docker_quick_start)
for the upstream login behavior.

## decision models and public inference validation

use LiteLLM's native `POST /v1/systemone` for System One bodies and
`POST /v1/decisions` for OpenAI Decisions bodies. both routes use the same
static decision models in `model_list`. each uses LiteLLM's `openai/` decision
provider and calls the OpenAI-format `/v1/decisions` route on the local
`jevk5-redqueen:8191` adapter using the stored Redqueen adapter key. the
adapter translates that request into the existing local System One contract.
the five canonical model IDs are `jevk5-4b-v0.3`,
`clef-flash-bf16`, `clef-flash-q8`, `clef-flash-q4`, and `clef-q4`.

those aliases are real LiteLLM catalog entries in this repository. after the
config is deployed, authenticated `GET /v1/models` returns them to keys whose
model scopes include them. the
provider/model table in LiteLLM's docs is a support catalog; it does not
automatically add models to this proxy. see LiteLLM's [Decisions API
documentation](https://docs.litellm.ai/docs/decisions) and [v1.104.2 release
notes](https://docs.litellm.ai/release_notes/v1.104.2/v1-104-2).

the native OpenAI-format `/v1/decisions` route accepts `input_image` parts for
the four local Clef models. Redqueen rejects remote image URLs, validates
embedded PNG/JPEG/WebP `data:` URLs, and limits each decoded image to 4 MiB and
16 megapixels, with four images and 8 MiB total. JevK5 is text-only and returns
a clear 400 if sent an image. `/v1/systemone` remains available; LiteLLM
translates it to the same local OpenAI-format decision endpoint.

the old `POST /typesafe/v1/systemone` and `GET /typesafe/v1/models` routes
remain during migration for compatibility and rollback. keep the dedicated
legacy virtual key restricted to those exact routes: the TypeSafe pass-through
reads its upstream key from the proxy environment and does not enforce its
`models` allowlist. retire those routes only after the live native Clef image
check succeeds and callers have moved.

keep the decision IDs off the shared LibreChat key. its dynamic `/v1/models`
picker should remain limited to chat models, and its image tool should keep
using only `qwen-image-2.1`.

run the combined decision, Clef-image compatibility, and Qwen Image contract
check from a workstation:

```sh
./apps/litellm/validate_inference.py
```

enter a disposable or restricted LiteLLM virtual key at the hidden prompt, not
the master key. by default the key must allow all five decision aliases, so
`GET /v1/models` can verify the catalog; `jevk5-4b-v0.3` on both native
decision routes; `clef-flash-bf16` with an inline image on the native Decisions
route and through the temporary legacy image route;
`qwen-image-2.1`; and `allowed_routes` for `/v1/models`, `/v1/systemone`,
`/v1/decisions`, `/typesafe/v1/systemone`, `/typesafe/v1/models`,
`/v1/images/generations`, and `/v1/images/edits`. when using `--text-model`,
it must also allow each selected chat alias and `/v1/chat/completions`; when
repeating `--image-model`, it must allow each selected image alias. the
validator checks auth and method boundaries,
exercises both native request formats, verifies Clef image input through both
the native and legacy routes, generates and edits a 512×512 PNG, validates both
image files, and prints their private temporary directory for visual
inspection. the live rollout and eventual legacy-route retirement remain
operator steps; see [`AI_INFERENCE_PLAN.md`](../../AI_INFERENCE_PLAN.md).
