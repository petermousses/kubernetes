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

## public inference validation

System One models share one public route: `POST /typesafe/v1/systemone`. Set
`model` in the JSON body to select JevK5 or a local Clef variant. LiteLLM
forwards this TypeSafe pass-through to the redqueen adapter and substitutes
`TYPESAFE_API_KEY` for the client's virtual key. This follows LiteLLM's
[native TypeSafe pass-through contract](https://docs.litellm.ai/docs/pass_through/typesafe).

| request `model` | selected model |
| --- | --- |
| `jev-latest`, `jevk5`, or `jevk5-4b-v0.3` | `jevk5-4b-v0.3` |
| `clef-flash` | `clef-flash-bf16` (default precision) |
| `clef-flash-bf16` | `clef-flash-bf16` |
| `clef-flash-q8` | `clef-flash-q8` |
| `clef-flash-q4` | `clef-flash-q4` |
| `clef` or `clef-q4` | `clef-q4` |

An omitted `model` defaults to `jevk5-4b-v0.3`. The authenticated
`GET /typesafe/v1/models` route lists accepted model aliases. This TypeSafe
inventory is separate from LiteLLM's `/v1/models`, which lists the configured
chat and image `model_list` entries.

Request bodies use `model`, `state`, and `questions`; Clef requests may also
include up to four embedded PNG/JPEG/WebP `data:` URLs in `images`. Remote image
URLs are rejected. The adapter limits each decoded image to 4 MiB and 16
megapixels, and the total decoded image bytes to 8 MiB. LiteLLM's TypeSafe cost
registry retains per-model pricing using the canonical model returned by the
adapter.

run the combined JevK5 and Qwen Image contract check from a workstation:

```sh
./apps/litellm/validate_inference.py
```

enter a disposable or restricted LiteLLM virtual key at the hidden prompt, not
the master key. by default the key must permit `qwen-image-2.1` plus the exact
image and TypeSafe routes exercised below. when using `--text-model`, it must
also permit each selected chat alias and `/v1/chat/completions`; when repeating
`--image-model`, it must permit each selected image alias. the validator checks
missing-key and wrong-method rejection, requires JevK5 to classify a
misdelivered parcel correctly, generates one 512×512 image, edits that generated
image, validates both PNG structures and prints the private temporary directory
containing both outputs for visual inspection. the expanded host cutover, model
IDs and multi-model invocation are recorded in
[`AI_INFERENCE_PLAN.md`](../../AI_INFERENCE_PLAN.md).
