## recommendation

use **LiteLLM as the sole model gateway/aggregator**.

do **not** make k3s directly manage the AMD Halo GPU workloads in v1. run the inference engines as host-managed services, then join the machine as a tainted worker for monitoring and future experimentation. forcing three GPU pods through Kubernetes’ single-device accounting would sabotage the true concurrency you asked for.

| layer | selection |
|---|---|
| api gateway | LiteLLM (MIT for the open-source core) |
| chat ui | LibreChat (MIT) |
| identity | Authentik OIDC (MIT for the community core) |
| Qwen3.8 runtime | `llama.cpp` (MIT) with ROCm (component-specific licenses); Qwen3.8-27B weights (Apache-2.0) |
| Qwen Image 2.1 runtime | ComfyUI (GPL-3.0) + thin OpenAI-compatible Images adapter (planned Apache-2.0); Qwen Image 2.1 weights (Qwen Research License) |
| JevK5 runtime | separate `llama.cpp` server + `/v1/systemone` adapter; JevK5 code and weights (Apache-2.0) |
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

## licenses and use constraints

**Qwen Image 2.1 is the licensing outlier.** Its [Qwen Research License](https://huggingface.co/Qwen/Qwen-Image-2.1/blob/main/LICENSE) is a custom, non-OSI license that defines non-commercial use as research or evaluation only. It permits use, modification and redistribution only for those purposes; commercial use requires a separate license from Qwen. Redistribution also requires the license and attribution notice, and using its materials or outputs to improve a distributed AI model triggers a “Built with Qwen” or “Improved using Qwen” notice. A private home-lab deployment is within the stated grant only when it is genuinely research or evaluation. Do not use this model for paid, client-facing or other business activity without obtaining the separate commercial license. This is a project risk classification, not legal advice.

| product or model | license | plan impact |
|---|---|---|
| [Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B/blob/main/LICENSE) | Apache-2.0 | permissive model license; preserve required notices when redistributing weights or derivatives. |
| [Qwen Image 2.1](https://huggingface.co/Qwen/Qwen-Image-2.1/blob/main/LICENSE) | Qwen Research License | non-commercial research/evaluation only unless a separate commercial license is obtained. |
| [JevK5 runtime and model](https://github.com/allebee/jevk5/blob/main/LICENSE) | Apache-2.0 | permissive; the selected [GGUF weights](https://huggingface.co/alibiserikbay/JevK5-GGUF) carry the same license. |
| [`llama.cpp`](https://github.com/ggml-org/llama.cpp/blob/master/LICENSE) | MIT | permissive. |
| [LiteLLM](https://github.com/BerriAI/litellm/blob/main/LICENSE) | MIT for the open-source core | anything under its `enterprise/` directory has separate terms and is excluded from this plan. |
| [LibreChat](https://github.com/LibreChat-AI/LibreChat/blob/main/LICENSE) | MIT | permissive. |
| [Authentik](https://github.com/goauthentik/authentik/blob/main/LICENSE) | MIT for the community core and client JavaScript | enterprise-directory code has separate terms; website content is CC BY-SA 4.0. this plan uses community features only. |
| [ComfyUI](https://github.com/Comfy-Org/ComfyUI/blob/master/LICENSE) | GPL-3.0 | internal execution over the network does not itself distribute ComfyUI; distributing a modified build requires GPL compliance and corresponding source. |
| [ROCm](https://github.com/ROCm/rocm-systems#license) | component-specific; no single umbrella license | audit the exact installed driver, firmware, runtime and library packages during node validation. do not label the whole stack “MIT.” |
| [Podman](https://github.com/podman-container-tools/podman/blob/main/LICENSE) | Apache-2.0 | permissive. |
| [systemd](https://github.com/systemd/systemd/blob/main/LICENSES/README.md) | LGPL-2.1-or-later generally | udev programs include GPL-2.0-or-later code; normal service use creates no project-specific distribution requirement. |
| [PostgreSQL](https://www.postgresql.org/about/licence/) | PostgreSQL License | permissive. |
| [k3s](https://github.com/k3s-io/k3s) | Apache-2.0 | permissive. |
| [Kubernetes](https://github.com/kubernetes/kubernetes) | Apache-2.0 | permissive. |
| [Traefik Proxy](https://github.com/traefik/traefik/blob/master/LICENSE.md) | MIT | the separate Traefik Helm chart is Apache-2.0. |
| [Helm](https://github.com/helm/community/blob/main/governance/governance.md#dco-and-licenses) | Apache-2.0 for code | documentation is CC BY 4.0. |
| [AMD GPU Operator](https://github.com/ROCm/gpu-operator) | Apache-2.0 | referenced but explicitly not deployed in v1. |
| [Open WebUI](https://github.com/open-webui/open-webui/blob/main/LICENSE) | Open WebUI License for v0.6.6+; older code is MIT/BSD-3-Clause by commit history | current license is not OSI-approved and restricts branding changes; retain its branding while it remains deployed, then retire it as planned. |
| [OpenMediaVault](https://github.com/openmediavault/openmediavault/blob/master/COPYING) | GPL-3.0 unless a component states otherwise | normal internal use is fine; comply with source obligations if distributing modified builds. |
| [Ollama](https://github.com/ollama/ollama/blob/main/LICENSE) | MIT | currently used but replaced by the selected runtime path. model licenses remain independent of Ollama’s software license. |
| [vLLM](https://github.com/vllm-project/vllm/blob/main/LICENSE) | Apache-2.0 | evaluated but not selected for this hardware. |
| [SGLang](https://github.com/sgl-project/sglang/blob/main/LICENSE) | Apache-2.0 | evaluated but not selected for this hardware. |
| [Redis](https://redis.io/legal/licenses/) | version-specific: BSD-3-Clause through 7.2, RSALv2/SSPLv1 for 7.4–7.8, and RSALv2/SSPLv1/AGPL-3.0 choice for 8+ | mentioned only because Authentik no longer requires it; not deployed by this plan. |
| planned image and JevK5 adapters | Apache-2.0, to be declared when created | first-party code does not exist yet; add an explicit license before distributing it. |
| OpenAI-compatible API shapes | interface compatibility, not bundled OpenAI software | no OpenAI model, SDK or service license is implied by implementing compatible request and response schemas. |

the vendor OS, Linux kernel, AMD firmware and third-party container contents are aggregate works with component-level terms. capture their package manifests and license notices during validation instead of pretending they have one project-wide license.

## execution sequence

### 1. validate redqueen

- connect with `ssh ai@redqueen.mousses.xyz -i ~/.ssh/redqueen` and capture the exact hardware, firmware, OS, kernel, installed packages and storage layout.
- audit every step-1 command as the unprivileged `ai` user. record necessary owner-run commands separately from optional privileged diagnostics; lack of `sudo` on `ai` is intentional and is not itself a failed gate.
- confirm the APU and operating-system combination against AMD’s current support matrix; require working `rocminfo`, expected `gfx1151` enumeration and access to `/dev/kfd` and `/dev/dri`.
- establish a rollback-safe TTM/GTT configuration targeting roughly 96 GB for GPU-accessible memory, then confirm that the setting persists across reboot without starving the host.
- compile or install a pinned ROCm-capable `llama.cpp`, run a minimal Qwen model, and complete a ComfyUI image smoke test before downloading the full production models.
- run simultaneous GPU-process and memory-pressure tests; stop implementation if the driver resets, hangs, corrupts output or requires an unsupported kernel/driver combination.
- **exit criterion:** the node survives reboot plus a one-hour mixed GPU stress test, and its validated software versions and configuration are recorded in the implementation commit.

#### step 1 execution record — 2026-09-26

the read-only inventory was run over the documented SSH path as the non-sudo `ai` user. observed baseline:

- official [AMD Ryzen AI Developer Platform](https://www.amd.com/en/blogs/2026/amd-ryzen-ai-developer-platform-open-ready-and-built.html) `RAH-001`, Ryzen AI Max+ 395 / Radeon 8060S, 16 cores / 32 threads, 125 GiB RAM, BIOS `03.04` dated 2026-07-27;
- AMD vendor OS `rex`, kernel `6.18.44+rex+5-amd64`, ROCm `7.14`, HIP `7.14.60850` and KFD topology target `110501` (`gfx1151`);
- Micron 4600 2 TB NVMe with the Btrfs root filesystem and about 1.7 TB free. `/srv/ai` will be a stable directory on this existing NVMe; no repartition or additional mount is required;
- persistent TTM configuration already present at `/etc/modprobe.d/ttm.conf` with `pages_limit=24692260`, exposing 101,139,496,960 bytes (about 94.2 GiB) of GTT. this satisfies the roughly 96 GB target and must not be rewritten without contrary test evidence;
- vendor ROCm-enabled `llama.cpp-tools` `9413+dfsg-1+rex1bvdebian13.1`, `libllama0` at the same build, and `libggml0` plus `libggml0-backend-hip` `0.13.1-1+rex2bvdebian13.1` from `https://debs.ryai.dev/`. use this exact installed build for validation instead of compiling a redundant copy;
- rootless Podman `5.4.2`, cgroup v2 and the required subordinate UID/GID ranges are already functional;
- outbound HTTPS to GitHub, Hugging Face and AMD's OCI registry succeeds;
- AMD's global ComfyUI user template collides on port `8188` because the existing `peter` session already owns it. leave that instance untouched and use a dedicated `ai`-owned validation service on free port `8189`.

the following commands are necessary but blocked by the deliberate lack of `sudo`. the node owner must run them exactly once:

```bash
sudo usermod -aG render,video ai
sudo install -d -o ai -g ai -m 0750 \
  /srv/ai \
  /srv/ai/models \
  /srv/ai/models/qwen3.8-27b \
  /srv/ai/models/jevk5-4b-v0.3 \
  /srv/ai/models/qwen-image-2.1 \
  /srv/ai/cache \
  /srv/ai/comfyui
sudo loginctl enable-linger ai
sudo apt-mark hold llama.cpp-tools libllama0 libggml0 libggml0-backend-hip
sudo reboot
```

the reboot intentionally terminates SSH. after it returns, reconnect as `ai` and verify `id`, `rocminfo`, `llama-server --list-devices`, directory ownership, package holds and `loginctl show-user ai -p Linger` before any model download. the package hold is reversible with `sudo apt-mark unhold` for the same four packages; linger is reversible with `sudo loginctl disable-linger ai`; group membership is reversible with `sudo gpasswd -d ai render` and `sudo gpasswd -d ai video`.

privileged kernel-log collection is diagnostic rather than a standing permission requirement. after each stress run, the owner should run `sudo journalctl -k -b --since '<test start time>'` and provide the output for reset/OOM/fault review; do not add `ai` to broad journal-reading groups merely for convenience.

pending step-1 gates after the owner batch: unprivileged ROCm enumeration, a minimal Qwen GGUF smoke test, a dedicated ComfyUI generation smoke test on port `8189`, simultaneous GPU/memory pressure, reboot persistence and the final one-hour mixed stress run.

### 2. deploy the local inference runtimes

- mount the local NVMe filesystem at `/srv/ai`; if the vendor OS requires a different physical mount point, use a bind mount so `/srv/ai` remains the stable service-facing path.
- create the dedicated `ai-inference` account and grant it ownership only where required under `/srv/ai`; systemd or Podman Quadlet service definitions must use explicit CPU, memory, file and restart limits.
- use this storage layout:
  - `/srv/ai/models/qwen3.8-27b/` — Qwen3.8 weights, multimodal projection, license, source revision and `SHA256SUMS`;
  - `/srv/ai/models/jevk5-4b-v0.3/` — JevK5 GGUF, calibration metadata, license, source revision and `SHA256SUMS`;
  - `/srv/ai/models/qwen-image-2.1/` — Qwen Image model components, license, source revision and `SHA256SUMS`;
  - `/srv/ai/cache/` — disposable download, conversion and runtime caches; never the authoritative copy of a model;
  - `/srv/ai/comfyui/` — pinned ComfyUI checkout, immutable workflows and custom-node lock data.
- configure every runtime to load weights from the authoritative `/srv/ai/models/` directories directly or through read-only symlinks; do not duplicate unmanaged model copies inside application directories or container layers.
- download the pinned Qwen3.8-27B, JevK5 4B Q8_0 and Qwen Image 2.1 artifacts; record source revisions and SHA-256 hashes, and retain every required license/notice file beside the weights.
- run separate `llama-server` instances for Qwen3.8 and JevK5, including Qwen’s multimodal projection and each service’s health and metrics endpoints.
- install a pinned ComfyUI revision and immutable Qwen Image 2.1 workflows for generation, editing, transparency and reference-image input.
- bind inference ports only to the private interface and restrict the host firewall to required cluster sources; no raw model endpoint may be internet- or user-accessible.
- **exit criterion:** every backend starts automatically after reboot, passes its direct health/functional test and remains inaccessible outside the approved cluster path.

### 3. build the unified API layer

- implement and test the image adapter for `/v1/images/generations` and `/v1/images/edits`, including validation, timeouts, cancellation, queue limits and deterministic ComfyUI workflow mapping.
- implement and test the JevK5 `/v1/systemone` adapter with typed request/response validation, option-count limits and calibrated model settings.
- deploy LiteLLM and its dedicated PostgreSQL database with pinned images, non-committed Kubernetes Secrets, network policies, probes, resource limits and persistent storage.
- register only `qwen3.8-27b`, `qwen-image-2.1` and the exact authenticated `/v1/systemone` pass-through; prohibit wildcard pass-through and caller Authorization-header forwarding.
- create separate least-privilege virtual keys for LibreChat, administrators and each machine client; enable request, latency, error and queue metrics without logging prompts or image contents by default.
- **exit criterion:** one API hostname serves all three capabilities, rejects missing or incorrectly scoped keys, and exposes no route that bypasses LiteLLM authentication.

### 4. deploy identity and the user interface

- deploy Authentik and its dedicated PostgreSQL database with pinned versions, persistent storage, initial bootstrap secrets and an MFA-protected local break-glass administrator.
- apply the Authentik blueprint for the LibreChat confidential OIDC client, strict callback URI, group claim and `librechat_users`/`librechat_admin` access controls.
- configure LibreChat to use only LiteLLM for chat and image operations; validate OIDC before disabling local login and email registration.
- issue LibreChat a restricted LiteLLM virtual key that cannot administer the gateway or access routes not required by the UI.
- add certificates, ingress, default-deny network policies, monitoring and nightly database dumps copied to an off-host backup destination.
- **exit criterion:** an authorized user can sign in through Authentik and use chat/image features, an unauthorized user is denied, administrative role mapping works, and both databases pass a restore test.

### 5. integrate, test and cut over

- join redqueen as a k3s agent only after the host-runtime gates pass; apply the AI labels and `ai.mousses.xyz/inference=true:NoSchedule` taint.
- grant tolerations only to explicitly approved monitoring components; keep inference engines host-managed for v1 rather than placing them behind Kubernetes GPU resource claims.
- run API-contract, OIDC, authorization, network-isolation, reboot-recovery and backup-restore tests, followed by the 30-minute mixed concurrency test defined below.
- move `chat.omv.mousses.xyz` to LibreChat, preserve the former LibreChat hostname as a redirect and retain the stopped Open WebUI data volume for 30 days.
- verify the rollback path by restoring the prior ingress target, then return traffic to LibreChat; commit and push each independently reviewable implementation phase without staging unrelated changes.
- **exit criterion:** the new path passes every acceptance test under normal and reboot conditions, monitoring is green, rollback is proven, and the repository and remote branch are clean and synchronized.
- [ ] **owner todo after every exit criterion passes:** choose the final NAS archive destination, manually copy `/srv/ai/models/` from redqueen to it, copy the adjacent licenses/source revisions/`SHA256SUMS`, and verify every destination hash against the source. record the NAS path in this plan after the copy. the NAS archive is a recovery copy; inference continues to load weights from redqueen’s local NVMe.

## implementation plan

### 1. validate the Halo node before touching k3s

- access the AI node with `ssh ai@redqueen.mousses.xyz -i ~/.ssh/redqueen`.
- record exact hardware, vendor OS, kernel and firmware.
- require:
  - the distro/kernel combination appears in AMD’s supported matrix;
  - `rocminfo` exposes the expected `gfx1151` device;
  - ROCm `llama.cpp` completes a Qwen smoke test;
  - ComfyUI completes a Qwen Image generation and edit;
  - multiple processes can share `/dev/kfd` without resets.
- keep the vendor OS only if those gates pass. otherwise stop. pretending an unsupported Debian derivative is “close enough” is how you get weeks of bullshit GPU debugging.
- reserve roughly 96 GB of the 128 GB unified memory for GPU-accessible TTM/GTT, leaving about 32 GB for the OS and k3s.
- require at least 250 GB of local NVMe model/cache space mounted at the stable path `/srv/ai`.

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
  - before deployment, confirm that all intended use is non-commercial research/evaluation under the **Qwen Research License**, or obtain Qwen’s separate commercial license.

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
