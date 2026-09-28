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
- `redqueen.mousses.xyz` currently resolves to `10.9.20.242`, while the machine's configured hostname is `amd-halo`. retain the DNS alias, but reserve the address before creating a Kubernetes `EndpointSlice` that depends on it;
- AMD vendor OS `rex`, kernel `6.18.44+rex+5-amd64`, ROCm `7.14`, HIP `7.14.60850` and KFD topology target `110501` (`gfx1151`);
- Micron 4600 2 TB NVMe with the Btrfs root filesystem and about 1.7 TB free. `/srv/ai` will be a stable directory on this existing NVMe; no repartition or additional mount is required. the host has no swap, so the stress gate must exercise real memory pressure and confirm that service limits prevent a host OOM;
- persistent TTM configuration already present at `/etc/modprobe.d/ttm.conf` with `pages_limit=24692260`, exposing 101,139,496,960 bytes (about 94.2 GiB) of GTT. this satisfies the roughly 96 GB target and must not be rewritten without contrary test evidence;
- vendor ROCm-enabled `llama.cpp-tools` `9413+dfsg-1+rex1bvdebian13.1`, `libllama0` at the same build, and `libggml0` plus `libggml0-backend-hip` `0.13.1-1+rex2bvdebian13.1` from `https://debs.ryai.dev/`. use this exact installed build for validation instead of compiling a redundant copy;
- rootless Podman `5.4.2`, cgroup v2 and the required subordinate UID/GID ranges are already functional. Python `3.13.5` and the vendor PyTorch ROCm `2.10` packages are also preinstalled;
- the vendor `lemond.service` is already active as the non-login `lemonade` account, and AMD's root-owned `/var/cache/models` contains existing Lemonade/Hugging Face models. these are pre-existing platform assets, not authoritative project storage; do not mutate or adopt them in place of `/srv/ai/models`;
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

post-reboot verification passed on 2026-09-26:

- `ai` is a member of `render` and `video` and can read/write `/dev/kfd`, `/dev/dri/renderD128` and `/dev/dri/card0` without `sudo`;
- `rocminfo` exits successfully and enumerates the Radeon 8060S as `gfx1151`; `llama-server --list-devices` reports `ROCm0`;
- all planned `/srv/ai` directories exist with mode `0750` and owner `ai:ai` on the NVMe-backed Btrfs root;
- the four ROCm `llama.cpp` packages are held, user linger is enabled, and the 101,139,496,960-byte GTT setting survived reboot;
- system and `ai` user service managers report zero failed units;
- the first ROCm Qwen smoke test passed using AMD's existing read-only `Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf`: expected text was generated at 63.6 tokens/s after a 125.4 tokens/s prompt evaluation. the interactive `llama-cli` frontend did not exit on closed stdin and was terminated after successful inference; use `llama-server` for subsequent automated tests. GPU memory returned to its pre-test baseline.

pending step-1 gate: the owner must provide the privileged kernel log for the completed one-hour mixed GPU/memory stress window so it can be checked for driver resets, faults, hangs and host OOM evidence. every unprivileged step-1 runtime gate has passed.

### 2. deploy the local inference runtimes

- mount the local NVMe filesystem at `/srv/ai`; if the vendor OS requires a different physical mount point, use a bind mount so `/srv/ai` remains the stable service-facing path.
- use the existing dedicated, non-sudo `ai` account and grant it ownership only where required under `/srv/ai`; systemd or Podman Quadlet service definitions must use explicit CPU, memory, file and restart limits.
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

#### step 2 execution record — 2026-09-27–28

the first host-runtime deployment is live under the non-sudo `ai` account:

- the cached AMD image `oci-registry.ryai.dev/ryai-comfyui@sha256:7ba03a5d07aa1687f4d1163aced5a1b15af7f672ba82d563acef523d4309184c` contains ComfyUI `0.21.1` from 2026-09-03 and predates native Qwen Image 2.1 support. its containerized PyTorch also needs an otherwise undocumented torch-library `LD_LIBRARY_PATH` adjustment before lazy ROCm initialization. do not use that image for this deployment;
- ComfyUI now runs directly from the clean detached checkout `4ef23c34d950eecc37040a21ee1741a49d2e44b1` (`0.37.0`) at `/srv/ai/comfyui/source`, using the vendor ROCm PyTorch `2.10.0`/ROCm `7.14` through an isolated system-site virtual environment. the venv-local dependency lock is versioned at `hosts/redqueen/comfyui/requirements.lock`;
- the official UI workflows are pinned to `Comfy-Org/workflow_templates@99e3d43745926b78466d99f937c0cd2bb622423a`. deterministic BF16 variants replace only the diffusion model and Qwen3-VL encoder defaults; the Qwen3.5 T2I/I2I prompt-enhancer files remain upstream INT8 because Comfy-Org does not publish BF16 equivalents in this repository;
- `comfyui.service` is enabled in the lingering `ai` user manager, binds only `127.0.0.1:8189`, and runs in the shared `ai-inference.slice`. the service has explicit CPU, memory, task, file-descriptor, restart and filesystem-sandbox limits. the documented SSH forward to local port `18189` returned both the UI and `/system_stats` successfully;
- `qwen38.service` is enabled on `127.0.0.1:8081` with a 65,536-token slot, Q4_K_M language weights, the Q8_0 multimodal projector, Jinja chat templates, metrics and no llama.cpp UI. `jevk5.service` is enabled on `127.0.0.1:8082` with four concurrent 8,192-token slots; llama.cpp divides `--ctx-size` across slots, so the correct aggregate setting is `32768`, not `8192`;
- the aggregate slice caps the three inference services at 30 CPU cores, 104 GiB memory high-water and 112 GiB hard memory maximum. the image generation/edit run with both llama.cpp backends resident peaked at 34,417,971,200 cgroup bytes (about 32.1 GiB), and all three services remained active with zero failed `ai` user units.
- after enabling private keyrings, personality locking, namespace denial and explicit socket-family allowlists, `systemd-analyze security` rates each user unit `5.7 MEDIUM`. its `User=`/root findings are an analysis artifact for user-manager units: the processes run as `ai`. the remaining device, network and writable-executable-memory exposure is intentional for the GPU runtimes and loopback servers.

authoritative model artifacts are complete on the local NVMe, read-only to the service processes, accompanied by source revisions, licenses/model cards and verified `SHA256SUMS`:

| backend | pinned source and selected artifacts | bytes |
|---|---|---:|
| Qwen3.8-27B | `ggml-org/Qwen3.8-27B-GGUF@71bc7b627595dc8a91039addd9c791ae548d6747`: `Qwen3.8-27B-Q4_K_M.gguf` plus `mmproj-Qwen3.8-27B-Q8_0.gguf`; license/model card from `Qwen/Qwen3.8-27B@1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0` | 19,603,117,536 |
| JevK5 4B v0.3 | `alibiserikbay/JevK5-GGUF@ec67b0bfce5119a8b11a2cdb430bb43e3fa3e82a`: `jevk5-4b-v0.3-Q8_0.gguf`; license from `allebee/jevk5@f944fe37ff1d5ed3830aa4c8d88b7189c8c1268a` | 4,482,402,720 |
| Qwen Image 2.1 | `Comfy-Org/Qwen-Image-2.1@9a44dbdb47cefd046be9c0a13476192f34c8db8e`: BF16 diffusion model, BF16 Qwen3-VL encoder, INT8 T2I/I2I prompt enhancers and BF16 VAE; license/model card from `Qwen/Qwen-Image-2.1@790c92633540aa0cb11d9abf19eb46d861714758` | 51,382,269,424 |

direct functional evidence:

- Qwen3.8 returned exactly `pong` through `/v1/chat/completions` at 10.56 generated tokens/s and accepted a data-URL image through its loaded multimodal projector;
- the official JevK5 GGUF client selected `misdelivered` with calibrated confidence `0.921107539238246` from a three-option parcel example, using temperature `1.22` and knockout temperature `0.93`;
- Qwen Image generated a valid 512×512 RGBA red-cube PNG in 56.25 seconds at four smoke-test steps, then used that file as a reference and changed it to a blue cube while preserving its geometry/background in 27.03 seconds. the output SHA-256 values are `74e4c0219fa06879a2b61fea8a853c360ad09decfbe08abbfcc6b040d474b8a5` and `ee6bafb0fb5b1ab5c1ce6b7406239a005cc2d6165a6c798a782a051a7748ee1f` respectively;
- the fixed transparency workflow produced a 512×512 RGBA green-sphere PNG with alpha spanning 0–255: 198,934 pixels were non-opaque, including 35,892 partially transparent edge pixels. its SHA-256 is `e41ef055d856431ae9e9baae9380382723d6673fedfd8534332775700288a308`;
- the fixed two-reference workflow uses orthogonal evidence: the original red cube is the only source of composition/geometry, while a deterministic solid-color swatch is the only source of its unnamed replacement color. the 512×512 output acquired the swatch color while retaining a `0.9751` foreground-mask intersection-over-union against the source cube; visual inspection confirmed the preserved cube/background, and its SHA-256 is `88b1186c9dffd8262fd4a4e7d6d6aa84da51c31c84832cdf267f9e6bec7606c9`;
- the previously suspect exact-1024 edit path passed on the pinned ComfyUI revision: it produced a valid 1024×1024 RGBA image, changed the reference cube to green and retained a `0.9896` foreground-mask intersection-over-union against the scaled source. its SHA-256 is `4d4f8c6f827c60678e44cdc2a87d824096f0a3758e1ff7a2dd497fdd05aca7bd`, so the 992/1056 workaround is not required for this deployment;
- a cold-start 2048×2048 generation completed in 261.75 seconds with a valid RGBA output (`ff699e2eaec7b97ed6b190859a9695aeb190680cc85a6915443023358c7f10b6`). ComfyUI peaked at 32,250,105,856 cgroup bytes; sampled aggregate-slice use reached 58,193,334,272 bytes and remained below the recorded 61,035,143,168-byte peak. every service remained active with zero restarts, and all service/slice `oom`, `oom_kill` and `oom_group_kill` counters remained zero;
- `hosts/redqueen/tests/run-step2-image-gates.sh` makes these tests reproducible: it cold-restarts ComfyUI to prevent an in-memory cache false pass, creates the orthogonal color-swatch fixture, submits all five fixed workflows, requires valid PNG decoding and exact dimensions, checks transparency classes, enforces swatch chromaticity/saturation, and compares edit/multi-reference structure against the source image before accepting the output;
- the final mixed stress run used `hosts/redqueen/tests/step2-stress.sh` from `2026-09-28T12:32:48-07:00` through `2026-09-28T13:33:22-07:00`, including worker drain. run-specific seeds prevented ComfyUI cache reuse. all 716 operations succeeded: 88 Qwen3.8 requests, 604 JevK5 requests across four slots and 24 image jobs, including five successful 2048×2048 generations. all 24 PNGs decoded at their declared dimensions. peak GTT use was 81,436,590,080 bytes (75.85 GiB), minimum host-available memory was 14,955,782,144 bytes (13.93 GiB), and the aggregate inference cgroup peaked at 48,621,404,160 bytes (45.28 GiB). all three services stayed active with zero restarts; every service/slice `oom`, `oom_kill` and `oom_group_kill` counter stayed at zero; and the `ai` user manager ended with no failed units;
- both model downloaders passed an idempotent rerun over the complete 75.5 GB store. disposable corrupt-prefix fixtures also proved that each downloader discards a checksum-failing partial and retries from byte zero instead of remaining permanently wedged;
- reboot recovery passed on 2026-09-28: linger restored all three enabled user services, each raw listener returned on its loopback-only port, and the pinned text-to-image request reproduced the original 512×512 RGBA output byte-for-byte (`74e4c0219fa06879a2b61fea8a853c360ad09decfbe08abbfcc6b040d474b8a5`);
- ComfyUI's Model Library loads folders lazily. select **Load All Folders** or expand `diffusion_models`, `text_encoders` and `vae`; the live UI then reports 1, 3 and 1 installed files respectively. the upstream workflow's direct model links are ordinary browser downloads to the workstation, not server-side installation controls. the redqueen workflow variants replace that misleading note with the authoritative server path and inventory;
- ComfyUI reported that its flash-attention probe is unsupported on this `gfx1151` build and automatically selected its sub-quadratic fallback. this is a performance caveat, not a correctness failure. the system-site venv also makes `pip check` report unrelated vendor-OS packages with missing or mismatched optional dependencies, so targeted imports, compilation and live API tests are the meaningful gates.
- after accepting the Xcode license, the repository validator exposed a separate local toolchain mismatch: Xcode `27.0` cannot load its `CoreDevice` framework on macOS `26.6.2`, while Command Line Tools `26.6` work correctly. `DEVELOPER_DIR=/Library/Developer/CommandLineTools cargo run --manifest-path scripts/validate-network-policies/Cargo.toml --locked` passes and validates all 21 app kustomizations plus shared platform invariants; repair or upgrade the full Xcode installation before selecting it again.

step 2 is **in progress**, not complete. every unprivileged runtime, image-correctness, reboot and one-hour stress gate has passed; only the owner-provided privileged kernel-log review for the exact stress window remains. raw ports deliberately remain loopback-only until the cluster source CIDRs, upstream credentials and owner-installed firewall rules are ready. do not weaken that boundary merely to make cluster wiring easier.

#### deferred exploration: image runtime and mobile editing workflow

ComfyUI remains the initial step-2 implementation so validation can proceed against a known Qwen Image workflow, but it is not a permanent architectural requirement. after the baseline works, evaluate these alternatives before finalizing the image stack:

- **official Diffusers BF16 worker:** run `QwenImage21Pipeline` directly behind a small OpenAI-compatible `/v1/images/generations` and `/v1/images/edits` service. treat this as the reference implementation for output quality and model behavior because it follows the official pipeline without a graph runtime;
- **`stable-diffusion.cpp` `sd-server`:** test its native OpenAI-compatible generation/edit endpoints, asynchronous jobs and HIP backend. it could replace both ComfyUI and the translation adapter if `gfx1151` stability, features and output quality match the Diffusers reference;
- **SGLang Diffusion or vLLM-Omni:** reconsider only after Qwen Image 2.1 support is released and the project documents or demonstrates reliable ROCm operation on the Radeon 8060S/`gfx1151`; current datacenter-AMD or unmerged support is insufficient;
- **InvokeAI:** consider if a rich desktop canvas, layers, masks and reusable visual workflows become more important than a narrow API service. verify Qwen Image 2.1 and this exact ROCm device before adoption;
- **mobile clients:** use LibreChat first for phone-friendly prompt, upload, multi-reference and conversational edit flows. evaluate Open WebUI as a PWA alternative only if it materially improves the experience; for touch masks, crop/outpaint framing and reference ordering, prefer a small dedicated mobile-first PWA behind Authentik over trying to operate a node graph on a phone.

the deferred comparison must use the official BF16 Diffusers pipeline as the behavioral baseline and cover text-to-image, image editing, multiple references, masks, RGBA transparency, the known 1024px edit regression, 2K memory pressure, queueing, cancellation, restart recovery and OpenAI Images API compatibility. no alternative replaces ComfyUI until it passes those gates and preserves the `/srv/ai/models/` storage contract, private-network boundary and LiteLLM/Authentik access model.

- **later exploration todo:** after the initial image backend and unified API are working, benchmark the candidates above and record the keep/replace decision before declaring step 2 final.

#### image API and ComfyUI access are separate paths

LiteLLM is the authenticated model API gateway; it is not the reverse proxy for the ComfyUI browser application. use this split:

```text
application/API path:
client -> Traefik -> LiteLLM /v1/images/* -> Kubernetes Service
       -> EndpointSlice 10.9.20.242:8190 -> image adapter
       -> ComfyUI 127.0.0.1:8189

operator UI path:
browser http://127.0.0.1:18189 -> SSH local forward
       -> redqueen 127.0.0.1:8189 -> ComfyUI
```

- bind ComfyUI only to `127.0.0.1:8189`. do not create a Kubernetes Ingress or LAN listener for the raw ComfyUI UI/API in v1;
- run the image adapter as `ai`, bind it to redqueen's private address on port `8190`, require a separate upstream credential and permit the port through the host firewall only from the cluster node addresses;
- make the adapter accept only the planned OpenAI-compatible `/v1/images/generations` and `/v1/images/edits` schemas and map them to immutable, versioned workflows. it must not expose arbitrary ComfyUI prompt graphs, filesystem paths, uploads outside the bounded request schema or ComfyUI administrative routes;
- represent the host adapter in Kubernetes with a selectorless `Service` and manually managed `EndpointSlice`. Kubernetes explicitly supports selectorless Services for backends outside the cluster; the endpoint must use redqueen's reserved non-loopback address, not `127.0.0.1`. [Kubernetes Service documentation](https://kubernetes.io/docs/concepts/services-networking/service/#services-without-selectors)
- register the adapter's cluster Service as the `qwen-image-2.1` backend in LiteLLM. LiteLLM supplies virtual-key authentication, model allowlisting, rate/queue controls and the public OpenAI-compatible image endpoints; the adapter supplies the ComfyUI-specific translation. [LiteLLM supported endpoints](https://docs.litellm.ai/docs/supported_endpoints)
- from any authorized workstation, start the UI tunnel with:

```bash
ssh -fN -T \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=15 \
  -o ServerAliveCountMax=3 \
  -L 127.0.0.1:18189:127.0.0.1:8189 \
  -i ~/.ssh/redqueen \
  ai@redqueen.mousses.xyz
```

sever the local tunnel without touching any remote service:

```bash
pid="$(lsof -tiTCP:18189 -sTCP:LISTEN)"
[[ -n "${pid}" ]] && kill "${pid}"
```

then open `http://127.0.0.1:18189`. `-fN` backgrounds the tunnel; do not suspend a foreground tunnel with `Ctrl-Z`. the keepalive options terminate a dead or unresponsive SSH transport and release its stale local listening socket after the server reboots. `ExitOnForwardFailure` also reports an occupied local port immediately. if an older tunnel already owns the port while requests fail, identify only that listener with `lsof -nP -iTCP:18189 -sTCP:LISTEN`, terminate its exact PID with `kill <pid>`, and rerun the command above. provision a separate SSH key for each workstation instead of copying one private key among machines.

SSH authentication is the v1 security boundary for the operator UI. if browser-only SSO access is desired later, add `comfy.omv.mousses.xyz` as a separate Traefik route protected by an Authentik single-application forward-auth provider; never route it through LiteLLM. Authentik documents this mode for applications without native OIDC, and Traefik's `ForwardAuth` middleware delegates the authorization check. [Authentik proxy-provider documentation](https://docs.goauthentik.io/add-secure-apps/providers/proxy/create-proxy-provider/), [Traefik ForwardAuth documentation](https://doc.traefik.io/traefik/reference/routing-configuration/http/middlewares/forwardauth/)

### 3. build the unified API layer

- implement and test the image adapter for `/v1/images/generations` and `/v1/images/edits`, including validation, timeouts, cancellation, queue limits and deterministic ComfyUI workflow mapping.
- implement and test the JevK5 `/v1/systemone` adapter with typed request/response validation, option-count limits and calibrated model settings.
- deploy LiteLLM and its dedicated PostgreSQL database with pinned images, non-committed Kubernetes Secrets, network policies, probes, resource limits and persistent storage.
- register only `qwen3.8-27b`, `qwen-image-2.1` and the exact authenticated `/v1/systemone` pass-through; prohibit wildcard pass-through and caller Authorization-header forwarding.
- create separate least-privilege virtual keys for LibreChat, administrators and each machine client; enable request, latency, error and queue metrics without logging prompts or image contents by default.
- **exit criterion:** one API hostname serves all three capabilities, rejects missing or incorrectly scoped keys, and exposes no route that bypasses LiteLLM authentication.

### 4. deploy identity and the user interface

- deploy Authentik and its dedicated PostgreSQL database with pinned versions, persistent storage, initial bootstrap secrets and an MFA-protected local break-glass administrator.
- require an Authentik build containing the `CVE-2026-25748` forward-auth fix (`2025.10.4`, `2025.12.4` or a later patched stable release); affected builds are forbidden for any optional ComfyUI browser route. [Authentik advisory](https://docs.goauthentik.io/security/cves/CVE-2026-25748/)
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

use pinned Podman Quadlets or systemd services running under the dedicated, non-sudo `ai` account:

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
