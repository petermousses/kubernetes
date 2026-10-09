# redqueen

## source of truth

this repository contains the Kubernetes side of the inference deployment and still has a `hosts/redqueen/` tree. Redqueen's host runtime and configuration source of truth is the sibling [inference repository](https://github.com/petermousses/inference), checked out locally at `../inference` and deployed on redqueen at `/srv/ai/inference`. make host runtime, adapter, model-router, ComfyUI configuration, systemd, firewall, model metadata, archive, and host-test changes in that repository.

the inference repository's root `README.md` describes its contents and how its link scripts connect the checkout to the live host paths. model weights remain under `/srv/ai/models`; credentials remain under `/srv/ai/secrets` and must never be committed. the upstream ComfyUI checkout, virtual environment, state, caches, and compiled runtime binaries are host data outside the Git checkout.

## connect to redqueen

from the workstation, log in as the unprivileged `ai` account with the Redqueen key:

```bash
ssh ai@redqueen.mousses.xyz -i ~/.ssh/redqueen
```

at the remote shell, use the deployed checkout:

```bash
cd /srv/ai/inference
```

on redqueen, run `hosts/redqueen/link-runtime.sh` as `ai` after updating the checkout. it links the tracked user-owned runtime files into their live `/srv/ai` and user-systemd paths, then reloads the user unit definitions. it does not restart services. for changes to the root-owned firewall files only, run `sudo hosts/redqueen/link-system-runtime.sh`; it links those files and reloads the system unit definitions. follow the migration plan for any required service restart and verification.

## ComfyUI operator access

ComfyUI stays bound to redqueen's loopback address `127.0.0.1:8189`. from the workstation, open the planned SSH tunnel:

```bash
ssh -fN -T \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=15 \
  -o ServerAliveCountMax=3 \
  -L 127.0.0.1:18189:127.0.0.1:8189 \
  -i ~/.ssh/redqueen \
  ai@redqueen.mousses.xyz
```

then open <http://127.0.0.1:18189>. keep the ComfyUI UI/API loopback-only; the tunnel is the operator access path.

## migration status

[`../../AI_INFERENCE_PLAN.md`](../../AI_INFERENCE_PLAN.md) is the detailed source for architecture, rollout commands, validation, rollback, and current migration gates. Redqueen's host runtimes are host-managed and remain outside k3s. the plan records the host runtime/router work as deployed and smoke-tested, while the wider migration still has NAS-side LiteLLM/model rollout, least-privilege key and Prometheus checks, LibreChat OIDC/model validation, recovery and mixed-load gates, and the first verified encrypted runtime archive outstanding. read the plan's latest status before treating the migration as complete.
