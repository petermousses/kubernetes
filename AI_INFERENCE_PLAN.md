## recommendation

use **LiteLLM as the sole model gateway/aggregator**.

do **not** make k3s directly manage the AMD Halo GPU workloads in v1. run the inference engines as host-managed services, then join the machine as a tainted worker for monitoring and future experimentation. forcing three GPU pods through Kubernetes’ single-device accounting would sabotage the true concurrency you asked for.

| layer | selection |
|---|---|
| api gateway | LiteLLM |
| chat ui | LibreChat |
| identity | Authentik OIDC |
| Qwen3.8 runtime | `llama.cpp` with ROCm |
| Qwen Image 2.1 runtime | ComfyUI + thin OpenAI Images adapter |
| JevK5 runtime | separate `llama.cpp` server + `/v1/systemone` adapter |
| machine authentication | scoped LiteLLM virtual keys |
| browser authentication | Authentik SSO |

AMD officially supports `llama.cpp` on supported Ryzen APUs, while vLLM/SGLang remain a much shakier choice on Strix Halo. [`llama.cpp` is already running Qwen3.8-27B on Ryzen AI Max+ hardware](https://github.com/ggml-org/llama.cpp/discussions/27154). [AMD’s compatibility matrix](https://rocm.docs.amd.com/en/latest/compatibility/compatibility-matrix.html) and [Strix Halo tuning guidance](https://rocm.docs.amd.com/en/docs-7.2.0/how-to/system-optimization/strixhalo.html) should be treated as the gatekeepers.

```text
browser ──> traefik ──> LibreChat ──> LiteLLM
                 └────> Authentik       │
api clients ──> api.ai... ──────────────┤
                                        ├─> llama.cpp: Qwen3.8-27B
                                        ├─> image adapter ─> ComfyUI
                                        └─> JevK5 adapter ─> llama.cpp
```

raw Halo endpoints remain private and firewall-restricted to the cluster.

## implementation plan

### 1. validate the Halo node before touching k3s

- record exact hardware, vendor OS, kernel and firmware.
- require:
  - the distro/kernel combination appears in AMD’s supported matrix;
  - `rocminfo` exposes the expected `gfx1151` device;
  - ROCm `llama.cpp` completes a Qwen smoke test;
  - ComfyUI completes a Qwen Image generation and edit;
  - multiple processes can share `/dev/kfd` without resets.
- keep the vendor OS only if those gates pass. otherwise stop. pretending an unsupported Debian derivative is “close enough” is how you get weeks of bullshit GPU debugging.
- reserve roughly 96 GB of the 128 GB unified memory for GPU-accessible TTM/GTT, leaving about 32 GB for the OS and k3s.
- require at least 250 GB of local NVMe model/cache space.

### 2. deploy the inference backends on the host

use pinned Podman Quadlets or systemd services running under a dedicated `ai-inference` account:

- **Qwen3.8-27B**
  - start with a pinned Q4_K-class GGUF, 64k context and multimodal projection.
  - expose only an internal OpenAI-compatible `llama-server`.
  - enable streaming, vision inputs, tool calling and metrics.
  - Qwen3.8-27B is a native vision-language model under Apache-2.0. [official model card](https://huggingface.co/Qwen/Qwen3.8-27B)

- **JevK5**
  - use JevK5 v0.3 4B Q8_0, not the larger 9B model; the project reports the 4B model as both smaller and more accurate on its hard tier.
  - run a dedicated `llama-server` and adapter exposing exactly `/v1/systemone`.
  - do not show JevK5 in LibreChat’s chat-model picker. it is a typed decision system, not a conversational model. [official JevK5 repository](https://github.com/allebee/jevk5)

- **Qwen Image 2.1**
  - use the official BF16 model in a pinned ComfyUI build with native Qwen Image 2.1 nodes.
  - add a small adapter translating `/v1/images/generations` and `/v1/images/edits` into fixed, versioned ComfyUI workflows.
  - support `b64_json`, prompt, size, seed, transparency and reference images.
  - explicitly regression-test 1024px editing because a current ComfyUI bug affects that path; use 992 or 1056 until the pinned version proves it fixed. [model](https://huggingface.co/Qwen/Qwen-Image-2.1), [ComfyUI issue](https://github.com/Comfy-Org/ComfyUI/issues/16435)
  - acceptance of the **Qwen Research License** is required before deployment.

### 3. add the cluster control plane

- deploy **LiteLLM** as one pinned replica with a dedicated PostgreSQL database.
- expose:

  - `qwen3.8-27b` through `/v1/chat/completions`
  - `qwen-image-2.1` through `/v1/images/generations` and `/v1/images/edits`
  - JevK5 through exact `/v1/systemone` pass-through

- use LiteLLM virtual keys:
  - one restricted key for LibreChat;
  - separate per-client keys for scripts and applications;
  - model allowlists, request limits and audit metadata per key;
  - master key usable only for administration.

[LiteLLM supports virtual keys, routing and OpenAI image endpoints](https://docs.litellm.ai/docs/). for JevK5, use an **exact authenticated pass-through**, never wildcard `include_subpath`, and never forward the caller’s Authorization header; current bugs make those patterns unsafe or unreliable. [wildcard auth issue](https://github.com/BerriAI/litellm/issues/36508), [header-forwarding issue](https://github.com/BerriAI/litellm/issues/32202)

### 4. deploy Authentik and connect LibreChat

- deploy Authentik’s pinned Helm chart plus a separately managed PostgreSQL 16 StatefulSet; Authentik no longer needs Redis. [official Kubernetes installation](https://docs.goauthentik.io/install-config/install/kubernetes), [2025.10 Redis removal](https://docs.goauthentik.io/releases/2025.10)
- expose `auth.omv.mousses.xyz`.
- provision declaratively through an Authentik blueprint:
  - confidential OIDC provider for LibreChat;
  - strict callback `https://chat.omv.mousses.xyz/oauth/openid/callback`;
  - `openid profile email groups` scopes;
  - `librechat_users` and `librechat_admin` groups;
  - group-bound application access.
- inject the OIDC client secret with Authentik’s supported `!Env` blueprint tag rather than committing it. [blueprint tags](https://docs.goauthentik.io/customize/blueprints/v1/tags/)
- retain one Authentik local break-glass administrator protected by MFA.
- configure LibreChat v0.8.7—currently the latest non-RC release—to:
  - automatically redirect to Authentik;
  - deny users lacking `librechat_users`;
  - map `librechat_admin` to its admin role;
  - disable email registration and local login after OIDC passes;
  - use only the LiteLLM endpoint;
  - use LiteLLM for its image-generation and editing tools. LibreChat supports both [OIDC](https://www.librechat.ai/docs/configuration/authentication/OAuth2-OIDC) and [custom OpenAI image endpoints](https://www.librechat.ai/docs/features/image_gen).

### 5. join and isolate the node

after the mixed-load test passes:

- join it as a k3s agent.
- apply:

```yaml
labels:
  ai.mousses.xyz/accelerator: strix-halo
  node-role.kubernetes.io/ai: "true"

taint:
  ai.mousses.xyz/inference=true:NoSchedule
```

the taint means ordinary cluster workloads cannot land there unless explicitly granted a toleration. monitoring agents receive that toleration; random apps do not.

do not install the AMD GPU Operator or move the inference services into pods in v1. the standard device-plugin model exposes the APU as one exclusive resource, which conflicts with independently running chat, image and JevK5 services.

## cutover and verification

- replace the current direct Ollama references in [Open WebUI](/Users/petermousses/Local/Github/kubernetes/apps/open-webui/configmap.yaml) and [LibreChat](/Users/petermousses/Local/Github/kubernetes/apps/librechat/configmap.yaml) with LiteLLM.
- make `chat.omv.mousses.xyz` canonical for LibreChat; redirect the current LibreChat hostname there.
- keep Open WebUI running during acceptance, then remove its ingress and workload while retaining its PV for 30 days. rollback is restoring the old ingress.
- pass all of these before cutover:
  - invalid API key → 401; scoped keys cannot access other models;
  - Authentik group admission, admin mapping, logout and revoked-user denial;
  - streaming chat, vision input and tool calls;
  - image generation, editing, transparency and multiple references;
  - typed JevK5 decisions through `/v1/systemone`;
  - 30-minute mixed load: two Qwen streams + one JevK5 request + one image job, with no OOM, driver reset, stalled stream or 5xx;
  - raw Halo ports unreachable from users;
  - Authentik and LiteLLM PostgreSQL restore tests.

nightly database dumps must be copied off the OpenMediaVault host. a dump sitting beside the database on the same machine is not a backup.

the only intentionally unresolved input is the final off-host backup destination; it does not change the platform architecture.
