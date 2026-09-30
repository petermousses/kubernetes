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

privileged kernel-log collection is diagnostic rather than a standing permission requirement. immediately after each stress run and before another reboot, the owner should run `sudo journalctl -k --boot 0 --since '<test start time>' --until '<test end time>' --no-pager` and provide the output for reset/OOM/fault review. when selecting an older boot explicitly, use the 32-character hexadecimal boot ID reported by `journalctl --list-boots`, without UUID hyphens; `journalctl` rejects the hyphenated form as an invalid match. do not add `ai` to broad journal-reading groups merely for convenience.

post-reboot verification passed on 2026-09-26:

- `ai` is a member of `render` and `video` and can read/write `/dev/kfd`, `/dev/dri/renderD128` and `/dev/dri/card0` without `sudo`;
- `rocminfo` exits successfully and enumerates the Radeon 8060S as `gfx1151`; `llama-server --list-devices` reports `ROCm0`;
- all planned `/srv/ai` directories exist with mode `0750` and owner `ai:ai` on the NVMe-backed Btrfs root;
- the four ROCm `llama.cpp` packages are held, user linger is enabled, and the 101,139,496,960-byte GTT setting survived reboot;
- system and `ai` user service managers report zero failed units;
- the first ROCm Qwen smoke test passed using AMD's existing read-only `Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf`: expected text was generated at 63.6 tokens/s after a 125.4 tokens/s prompt evaluation. the interactive `llama-cli` frontend did not exit on closed stdin and was terminated after successful inference; use `llama-server` for subsequent automated tests. GPU memory returned to its pre-test baseline.

step 1 is **complete**. the one-hour mixed GPU/memory stress gate, reboot recovery and all unprivileged runtime gates passed. the owner-provided privileged kernel journal for the exact stress window contained one benign `perf` sampling-rate throttle (`interrupt took too long (2514 > 2500)`) and no AMDGPU/KFD reset, GPU fault, hang, watchdog, timeout, OOM or killed-process evidence.

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
- the privileged kernel journal for the exact mixed-stress window contained only one adaptive `perf` sampling-rate throttle (`interrupt took too long (2514 > 2500), lowering kernel.perf_event_max_sample_rate to 79500`). it contained no AMDGPU/KFD reset, GPU fault, ring failure, hang, watchdog, timeout, OOM or killed-process evidence. the temporary exported journal file was deleted after review;
- ComfyUI's Model Library loads folders lazily. select **Load All Folders** or expand `diffusion_models`, `text_encoders` and `vae`; the live UI then reports 1, 3 and 1 installed files respectively. the upstream workflow's direct model links are ordinary browser downloads to the workstation, not server-side installation controls. the redqueen workflow variants replace that misleading note with the authoritative server path and inventory;
- ComfyUI reported that its flash-attention probe is unsupported on this `gfx1151` build and automatically selected its sub-quadratic fallback. this is a performance caveat, not a correctness failure. the system-site venv also makes `pip check` report unrelated vendor-OS packages with missing or mismatched optional dependencies, so targeted imports, compilation and live API tests are the meaningful gates.
- after accepting the Xcode license, the repository validator exposed a separate local toolchain mismatch: Xcode `27.0` cannot load its `CoreDevice` framework on macOS `26.6.2`, while Command Line Tools `26.6` work correctly. `DEVELOPER_DIR=/Library/Developer/CommandLineTools cargo run --manifest-path scripts/validate-network-policies/Cargo.toml --locked` passes and validates all 21 app kustomizations plus shared platform invariants; repair or upgrade the full Xcode installation before selecting it again.

step 2's host-runtime deployment and validation are **complete**. every runtime, image-correctness, reboot, one-hour stress and privileged kernel-log gate passed. raw ports deliberately remain loopback-only until step 3 supplies the adapters, cluster source CIDRs, upstream credentials and owner-installed firewall rules. do not weaken that boundary merely to make cluster wiring easier.

#### deferred exploration: image runtime and mobile editing workflow

ComfyUI remains the initial step-2 implementation so validation can proceed against a known Qwen Image workflow, but it is not a permanent architectural requirement. after the baseline works, evaluate these alternatives before finalizing the image stack:

- **official Diffusers BF16 worker:** run `QwenImage21Pipeline` directly behind a small OpenAI-compatible `/v1/images/generations` and `/v1/images/edits` service. treat this as the reference implementation for output quality and model behavior because it follows the official pipeline without a graph runtime;
- **`stable-diffusion.cpp` `sd-server`:** test its native OpenAI-compatible generation/edit endpoints, asynchronous jobs and HIP backend. it could replace both ComfyUI and the translation adapter if `gfx1151` stability, features and output quality match the Diffusers reference;
- **SGLang Diffusion or vLLM-Omni:** reconsider only after Qwen Image 2.1 support is released and the project documents or demonstrates reliable ROCm operation on the Radeon 8060S/`gfx1151`; current datacenter-AMD or unmerged support is insufficient;
- **InvokeAI:** consider if a rich desktop canvas, layers, masks and reusable visual workflows become more important than a narrow API service. verify Qwen Image 2.1 and this exact ROCm device before adoption;
- **mobile clients:** use LibreChat first for phone-friendly prompt, upload, multi-reference and conversational edit flows. evaluate Open WebUI as a PWA alternative only if it materially improves the experience; for touch masks, crop/outpaint framing and reference ordering, prefer a small dedicated mobile-first PWA behind Authentik over trying to operate a node graph on a phone.

the deferred comparison must use the official BF16 Diffusers pipeline as the behavioral baseline and cover text-to-image, image editing, multiple references, masks, RGBA transparency, the known 1024px edit regression, 2K memory pressure, queueing, cancellation, restart recovery and OpenAI Images API compatibility. no alternative replaces ComfyUI until it passes those gates and preserves the `/srv/ai/models/` storage contract, private-network boundary and LiteLLM/Authentik access model.

- **later exploration todo:** after the initial image backend and unified API are working, benchmark the candidates above and record the keep/replace decision before replacing the validated ComfyUI baseline. this non-blocking comparison does not reopen the completed step-2 host-runtime gate.

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
- register only `qwen3.8-27b`, `qwen-image-2.1` and LiteLLM's native authenticated `/typesafe/v1/systemone` pass-through, which forwards the exact `/v1/systemone` suffix to the redqueen adapter; prohibit wildcard pass-through and caller Authorization-header forwarding. [LiteLLM TypeSafe pass-through](https://docs.litellm.ai/docs/pass_through/typesafe)
- create separate least-privilege virtual keys for LibreChat, administrators and each machine client; enable request, latency, error and queue metrics without logging prompts or image contents by default.
- **exit criterion:** one API hostname serves all three capabilities, rejects missing or incorrectly scoped keys, and exposes no route that bypasses LiteLLM authentication.

#### step 3 preflight and deployment record — 2026-09-28

the NAS preflight passed with these observed facts:

- k3s client/server `v1.33.5+k3s1` and Kustomize `v5.6.0`; the sole node `openmediavault` is `Ready` at `10.9.20.14` on Debian 12;
- the repeated `/etc/rancher/k3s/config.yaml: permission denied` warnings are noisy but non-fatal because all requested API reads succeeded. do not broaden kubeconfig permissions merely to suppress them;
- `letsencrypt-production-cloudflare` is `Ready=True`, and the default `local-path` provisioner plus the existing static local-storage pattern are available;
- `/filesystem/k3s/data` resolves onto the 37 TB k3s filesystem, with 33 TB free. LiteLLM PostgreSQL gets a dedicated retained static volume at `/filesystem/k3s/data/litellm/postgres` rather than borrowing another application's storage class;
- the initial `git status` failed only because it was run outside the repository clone. rerun it after changing into the clone; it says nothing about cluster health.

the v1 boundary is frozen: redqueen remains outside k3s, while one LiteLLM replica and one dedicated PostgreSQL 16 instance run on `openmediavault`. selectorless Services and manually managed EndpointSlices represent redqueen at reserved address `10.9.20.242`; Kubernetes explicitly supports this external-backend pattern. [Kubernetes selectorless Services](https://kubernetes.io/docs/concepts/services-networking/service/#services-without-selectors)

the implementation is versioned under `apps/litellm/` and `hosts/redqueen/` with these controls:

- LiteLLM `v1.103.0` is pinned by a Cosign-verified OCI index digest, runs as UID/GID 101 with a read-only root filesystem, one worker, one replica, a fixed 1 CPU/4 GiB envelope, production mode and probes. schema updates remain disabled during normal startup. the Admin UI is enabled inside the Pod but no UI or management path is present on the public API IngressRoute;
- a separate, explicitly ordered migration Job runs before the gateway. it uses LiteLLM's dedicated, offline `litellm-migrations:v1.103.0` image pinned to a Cosign-verified index digest, its native v2 resolver, UID/GID 65532 and a read-only root filesystem. `apps/litellm/deploy.sh` exists because plain `kubectl apply -k .` would create a migration/startup race; use the script for installs and upgrades;
- PostgreSQL `16.14-bookworm` is pinned by digest, runs as UID/GID 999, uses the retained static PV, and is reachable only from the gateway and migration pods;
- secrets are never committed. `bootstrap-secrets.sh` prompts for the three redqueen upstream keys, generates the database password, LiteLLM master key and permanent salt, then refuses accidental rotation if the Secret already exists;
- the redqueen image adapter exposes only bounded `/v1/images/generations` and `/v1/images/edits` contracts, maps requests into fixed workflows, limits execution to one active/four waiting jobs, cleans request-scoped inputs/outputs, verifies returned PNG dimensions and the requested alpha contract, and composites RGBA output onto a white matte for `background=opaque` so the response is truly opaque RGB;
- the JevK5 adapter exposes only `/v1/systemone`, enforces TypeSafe-style typed inputs/outputs and uses the selected Q8_0 file's documented `temperature=1.22` and `knockout_temperature=0.93` calibration. its vendored prompt/readout is attributed to JevK5 v0.3.0 and SemIf;
- Qwen binds to `10.9.20.242:8081`; the image and Jev adapters bind to `10.9.20.242:8190` and `:8191`. separate 256-bit upstream credentials protect every inference operation, and the host firewall admits only the NAS, redqueen itself and the k3s pod CIDR to those ports. llama.cpp leaves `/v1/models` metadata unauthenticated even with `--api-key-file`; this is accepted only behind that source-IP firewall, while the public `/v1/models` route remains behind LiteLLM authentication;
- the public Traefik IngressRoute uses method-scoped allowlists: `GET`/`POST` only for the exact `/v1/responses` path and its slash-delimited subpaths, `GET` only for `/v1/models` and its slash-delimited subpaths, and `POST` only for the approved exact chat, image and `/typesafe/v1/systemone` paths. health, metrics and administrative routes remain cluster-only. the redqueen Jev adapter's internal route remains `/v1/systemone`.

the execution order is deliberately split at the privilege boundary:

1. [x] copy the committed host files to redqueen, install the adapters as `ai`, and stop before exposing listeners;
2. [x] the node owner installs the nftables package and enables the dedicated `redqueen-inference-firewall.service`, which owns only its `inet redqueen_inference` table and never flushes the host ruleset; the owner also creates `/srv/ai/secrets`, then `ai` runs `hosts/redqueen/create-secrets.sh` to create—but not print or overwrite—the three upstream credentials. the package, firewall and directory commands are owner work because `ai` intentionally has no sudo;
3. [x] enable and start the three authenticated redqueen listeners, then verify from the NAS that allowed health/API requests succeed and an ordinary LAN client is rejected. the listeners, redqueen-local checks and ordinary-LAN denial pass; the NAS routes to `10.9.20.242` from `10.9.20.14` and received HTTP 200 from `:8081/health`, `:8190/healthz` and `:8191/healthz`;
4. [x] the NAS storage directory and non-committed Secret exist, PostgreSQL is healthy, the migration completed, LiteLLM rolled out with zero restarts, and the TLS-protected public API passed authenticated access;
5. [ ] **deferred by owner on 2026-09-29:** create least-privilege LiteLLM virtual keys and run the full external contract/authentication/isolation matrix. broad-key functional success does not prove wrong/missing/scoped-key denial. complete this before relying on key isolation or calling the security acceptance gates passed.
6. [ ] **deferred by owner on 2026-09-29:** confirm the deployed dedicated LiteLLM Prometheus listener and ServiceMonitor yield a healthy scrape target and live request, error, latency and proxy queue-time series. the listener is unauthenticated on port 4001, so keep it off public Ingress and allow only Prometheus through bidirectional network policies.

live redqueen verification on 2026-09-28 established that the dedicated nftables unit loaded successfully, all three authenticated listeners were active on their intended addresses, ComfyUI remained loopback-only, and a non-allowlisted workstation timed out against ports `8081`, `8190` and `8191`. missing and invalid credentials were rejected for Qwen chat, image and Jev inference; valid credentials passed real Qwen chat and Jev decision requests. a real 512 px Qwen Image request initially exposed near-opaque alpha values (`254–255`) and was correctly redacted as `502`; commit `785e862` added deterministic opaque compositing, after which the same request returned a 512×512 RGB PNG. twelve adapter tests pass on both the development host and redqueen.

the first NAS deployment reached healthy PostgreSQL but the migration Job failed before the gateway was created. the original application exception is unrecoverable: `restartPolicy: OnFailure` exhausted the Job backoff by restarting one container, after which the controller deleted its only Pod and logs. the corrected Job uses LiteLLM's release-matched migration image instead of invoking the legacy migration module from the full gateway image, sets `restartPolicy: Never` and `backoffLimit: 0`, and retains the failed Pod. the deployment script now observes both `Complete=True` and `Failed=True`; failure or timeout stops before applying the gateway and prints the retained Pod logs plus Job diagnostics. [LiteLLM v1.103.0 migration image](https://github.com/BerriAI/litellm/blob/v1.103.0/migrations/Dockerfile), [LiteLLM v1.103.0 migration Job](https://github.com/BerriAI/litellm/blob/v1.103.0/helm/litellm/templates/migrations-job.yaml), [Kubernetes Job failure handling](https://kubernetes.io/docs/concepts/workloads/controllers/job/)

the retained failure and live network inspection on 2026-09-29 narrowed the next fault precisely: Prisma returned `P1001` for `postgres.litellm.svc.cluster.local:5432`; meanwhile the PostgreSQL Pod was ready, its headless Service and EndpointSlice selected `10.42.0.22:5432`, PostgreSQL logged listeners on IPv4 and IPv6, and both loopback and Pod-IP TCP connections succeeded inside the database Pod. a newly created, correctly labelled probe using the migration image resolved the Service but received immediate `ECONNREFUSED` on its first connection. the rendered LiteLLM policies use the same selector pattern as working PostgreSQL-backed applications and match the live Pod labels, so changing their allowed peers would be cargo-cult bullshit.

this symptom matches kube-router's documented startup race: k3s uses kube-router's network-policy controller, policy rules are applied asynchronously after Pod creation, and the default-deny tail can reject traffic before the Pod-specific firewall chain is programmed. both the migration Job and gateway therefore use a hardened init container that retries a real TCP `pg_isready` call for at most ten minutes before application startup. PostgreSQL's own startup, readiness and liveness probes now specify `-h 127.0.0.1`, so they verify the TCP listener rather than only its Unix socket. the next NAS deployment is the integration proof: success closes the race diagnosis; a full ten-minute init-container timeout instead proves persistent kube-router rule-programming failure and preserves the relevant logs. [k3s network-policy controller](https://docs.k3s.io/networking/networking-services), [kube-router policy-startup troubleshooting](https://github.com/cloudnativelabs/kube-router/blob/master/docs/troubleshoot.md)

the 2026-09-29 redeployment passed that integration proof: the migration Job completed, LiteLLM became `1/1 Running` with zero restarts, Traefik published the TLS IngressRoute, and authenticated `/v1/models` access succeeded. keep the public hostname limited to the method-scoped inference allowlist.

the LiteLLM Admin UI is intentionally available only through an operator tunnel until Authentik exists. from a workstation, use one SSH process that also starts the NAS-side Kubernetes port-forward:

retrieve the master key directly on the NAS:

```bash
kubectl -n litellm get secret litellm-env \
  -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 --decode
printf '\n'
```

or retrieve it from a workstation through the normal NAS SSH account:

```bash
ssh peter@openmediavault \
  "kubectl -n litellm get secret litellm-env \
    -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 --decode; printf '\\n'"
```

then open the tunnel:

```bash
ssh -t \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -L 127.0.0.1:14000:127.0.0.1:14000 \
  peter@openmediavault \
  'kubectl -n litellm port-forward --address 127.0.0.1 service/litellm 14000:4000'
```

open `http://127.0.0.1:14000/ui`, sign in as `admin` with the existing `LITELLM_MASTER_KEY`, and use `Ctrl-C` to sever both forwarding layers. the key is a root credential; retrieve it only when needed and store it in a password manager, never in shell history or the repository. [LiteLLM Admin UI quickstart](https://docs.litellm.ai/docs/proxy/docker_quick_start)

run `./apps/litellm/validate_inference.py` from a workstation with a disposable or restricted virtual key at its hidden prompt. it proves public wrong-method and missing/invalid-key denial, a semantic JevK5 decision, a 512×512 Qwen Image generation and an edit of the generated PNG. inspect both saved outputs before closing step 3.

the 2026-09-29 public validation passed with a virtual key: JevK5 chose `misdelivered` at confidence `0.998321`, and Qwen Image returned valid 512×512 generation and edit PNGs. visual inspection confirmed that the edit changed the centered cube from red to blue while preserving its shape and plain background. LiteLLM v1.103.0 forwards edit uploads as `image[]`; redqueen adapter commit `720cb2e` accepts that field through its existing bounded image validation. all 13 adapter tests passed on redqueen, and a live `image[]` edit returned HTTP 200 with a valid 512×512 PNG. the NAS-origin firewall allow probe subsequently passed. the owner elected to proceed to step 4 with the scoped-key matrix and live Prometheus scrape check explicitly deferred, not passed.

JevK5 is deliberately absent from `/v1/models`: that list contains LiteLLM `model_list` entries for OpenAI-compatible calls, while JevK5 is a typed decision API behind the native `/typesafe/v1/systemone` pass-through. a virtual key for JevK5 must be restricted by nonempty `allowed_routes` containing `/typesafe/v1/systemone`; its `models` allowlist does not govern this pass-through. do not add a fake chat model merely to make JevK5 appear in `/v1/models`. future decision models need an explicit typed route or a dedicated decision router with model-aware authorization; a shared unrestricted pass-through would make per-model key isolation impossible. verify the key's actual denials before relying on this boundary.

### 4. deploy identity and the user interface

- deploy Authentik and its dedicated PostgreSQL database with pinned versions, persistent storage, initial bootstrap secrets and an MFA-protected local break-glass administrator.
- require an Authentik build containing the `CVE-2026-25748` forward-auth fix (`2025.10.4`, `2025.12.4` or a later patched stable release); affected builds are forbidden for any optional ComfyUI browser route. [Authentik advisory](https://docs.goauthentik.io/security/cves/CVE-2026-25748/)
- apply the Authentik blueprint for the LibreChat confidential OIDC client, strict callback URI, group claim and `librechat_users`/`librechat_admin` access controls.
- add a separate `admin.ai.omv.mousses.xyz` OIDC client and Ingress for the LiteLLM Admin UI; never broaden `api.ai.omv.mousses.xyz` beyond its exact inference paths. use LiteLLM's native generic OIDC flow so authenticated identity and roles reach LiteLLM rather than placing a blind forward-auth gate in front of its root UI. retain the SSH-only master-key login as break glass. LiteLLM documents Admin UI SSO as free for up to five users; more users require its Enterprise license. [LiteLLM feature comparison](https://docs.litellm.ai/docs/enterprise)
- configure LibreChat to use only LiteLLM for chat and image operations; validate OIDC before disabling local login and email registration.
- issue LibreChat a restricted LiteLLM virtual key that cannot administer the gateway or access routes not required by the UI.
- add certificates, ingress, default-deny network policies, monitoring and nightly database dumps copied to an off-host backup destination.
- **exit criterion:** an authorized user can sign in through Authentik and use chat/image features, an unauthorized user is denied, administrative role mapping works, and both databases pass a restore test.

#### step 4 deployment sequence

the owner clarified on 2026-09-29 that LibreChat has never been deployed to this cluster. its repository manifests exist; phase 4b must perform a first deployment, not reset an existing LibreChat database or preserve an existing session. the OIDC bootstrap subsequently created its namespace and OIDC Secret, but not its database, volumes or application.

phase 4a installed Authentik `2026.8.3` (chart and digest-pinned server/worker image), a separate PostgreSQL 16 instance, retained local database and `/data` volumes, a private metrics ServiceMonitor and a certificate. the public `auth.omv.mousses.xyz` IngressRoute in `apps/authentik/ingress.yaml` remains excluded from `kubectl apply -k` and `apps/authentik/deploy.sh`; the owner applied it separately only after testing `akadmin` MFA. the chart has no Kubernetes service-account token or bundled database; the app is default-deny except for PostgreSQL, DNS, Traefik and Prometheus flows. [Authentik Kubernetes install](https://docs.goauthentik.io/install-config/install/kubernetes), [2026.8 release](https://docs.goauthentik.io/releases/2026.8/), [MFA stage behavior](https://docs.goauthentik.io/add-secure-apps/flows-stages/stages/authenticator_validate/)

the first NAS rollout exposed a DNS search-order bug, not a PostgreSQL or network-policy denial: both Authentik pods repeatedly failed their `wait-for-postgres` init container; the headless Service had endpoint `10.42.0.41:5432`, direct Pod-IP `pg_isready` succeeded from the Authentik init container, but `postgres.authentik.svc.cluster.local` resolved to `10.9.20.2` as `postgres.authentik.svc.cluster.local.mousses.xyz` and refused connections. the chart now sets `ndots: 1` for both server and worker so the cluster FQDN is queried before search suffixes, matching the live LiteLLM and planned LibreChat pod manifests. the owner subsequently confirmed successful redeployment, certificate readiness, public HTTPS login at `auth.omv.mousses.xyz`, and a fresh login requiring TOTP. the phase-4a live status is owner-reported rather than independently observed from this workstation. [Kubernetes pod DNS search behavior](https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/)

on the NAS, after pulling the commit into the clone and changing to its root:

```bash
sudo install -d -o 999 -g 999 -m 0700 /filesystem/k3s/data/authentik/postgres
sudo install -d -o 1000 -g 1000 -m 0700 /filesystem/k3s/data/authentik/data
./apps/authentik/bootstrap-secrets.sh
./apps/authentik/bootstrap-librechat-oidc-secrets.sh
kubectl apply -f apps/monitoring/networkpolicy.yaml
./apps/authentik/deploy.sh
```

`bootstrap-secrets.sh` prompts invisibly for a 20+ character `akadmin` password and creates the permanent Authentik signing key plus matching PostgreSQL credentials without printing them. it refuses rotation if `authentik-env` already exists. keep the password in a password manager. PostgreSQL and `/data` must later be included in off-host backup/restore tests; the backup destination is still an owner decision.

for the initial browser setup, from a workstation open this single NAS tunnel:

```bash
ssh -t -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:19000:127.0.0.1:19000 \
  peter@openmediavault \
  'kubectl -n authentik port-forward --address 127.0.0.1 service/authentik-server 19000:80'
```

visit `http://127.0.0.1:19000/` and sign in as `akadmin`. enroll TOTP, inspect the default authentication flow's Authenticator Validation stage and ensure its *Not configured action* cannot skip MFA for the admin. log out, start a fresh private browser session, and prove password alone cannot complete login. `Ctrl-C` closes the tunnel. confirm `auth.omv.mousses.xyz` resolves to Traefik and the Authentik Certificate is `Ready=True`; only then publish the public route with `kubectl apply -f apps/authentik/ingress.yaml` and verify HTTPS/browser login. WebAuthn may be added after the public HTTPS hostname is live; do not enroll an authenticator under the `localhost` tunnel and assume it will work on the public domain. [Authenticator validation stage](https://docs.goauthentik.io/add-secure-apps/flows-stages/stages/authenticator_validate/)

phase 4b, after Authentik is live, adds the OIDC blueprint and cross-namespace egress/ingress rules, deploys LibreChat for the first time with a restricted LiteLLM key, then independently enables LiteLLM Admin UI OIDC on its separate hostname. validate authorized, unauthorized and admin group tests before relying on OIDC as LibreChat's only login path. phase 4c adds nightly off-host database dumps and verifies restores; neither a successful chart rollout nor a Prometheus manifest alone proves those gates.

phase 4b begins with an Authentik blueprint for `librechat_users` and `librechat_admin`, a confidential authorization-code OIDC client, a single exact callback at LibreChat's *planned initial* `librechat.omv.mousses.xyz` hostname, and an application binding admitting only `librechat_users`. the `profile` scope contains the group claim in Authentik `2026.8.3`; the provider signs with Authentik's generated certificate and reads its client ID/secret through `!Env`. `apps/authentik/bootstrap-librechat-oidc-secrets.sh` creates the LibreChat namespace from its committed manifest and then creates matching Authentik/LibreChat namespace Secrets without printing values; it does not deploy LibreChat. do not move `chat.omv.mousses.xyz` (still Open WebUI) until the new LibreChat deployment and OIDC path have passed live tests. [Authentik default scopes](https://github.com/goauthentik/authentik/blob/version-2026.8/blueprints/system/providers-oauth2.yaml), [Authentik blueprint environment tags](https://docs.goauthentik.io/customize/blueprints/v1/tags/)

the first live OIDC check found the worker and both Secrets present but discovery returned 404. Authentik's blueprint dry-run rejected the OAuth provider because `invalidation_flow` was required; its error output also included the client secret. treat that initial credential as exposed, and do not paste raw Authentik blueprint output or logs. the corrected blueprint uses `default-provider-invalidation-flow`, which ends only the application's session by default. before deploying LibreChat, pull the correction and run `./apps/authentik/rotate-librechat-oidc-secrets.sh` on the NAS. that pre-deployment-only script replaces both matching Secrets, applies the corrected ConfigMap, restarts the worker, and validates/applies the blueprint while suppressing secret-bearing diagnostics; it refuses to rotate after LibreChat is deployed. then verify the public `application/o/librechat/.well-known/openid-configuration` endpoint returns 200. rotation and live discovery are **pending**, not completed. [Authentik default flows](https://docs.goauthentik.io/add-secure-apps/flows-stages/flow/default-flows), [blueprint application behavior](https://docs.goauthentik.io/customize/blueprints)

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
  - JevK5 through LiteLLM's native `/typesafe/v1/systemone` pass-through to redqueen's exact `/v1/systemone` adapter

- use LiteLLM virtual keys:
  - one restricted key for LibreChat;
  - separate per-client keys for scripts and applications;
  - model allowlists, request limits and audit metadata per key;
  - master key usable only for administration.

[LiteLLM supports virtual keys, routing and OpenAI image endpoints](https://docs.litellm.ai/docs/). for JevK5, use the native TypeSafe integration at `/typesafe/v1/systemone`, never wildcard `include_subpath`, and never forward the caller’s Authorization header; current bugs make those generic patterns unsafe or unreliable. [LiteLLM TypeSafe pass-through](https://docs.litellm.ai/docs/pass_through/typesafe), [wildcard auth issue](https://github.com/BerriAI/litellm/issues/36508), [header-forwarding issue](https://github.com/BerriAI/litellm/issues/32202)

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
  - typed JevK5 decisions through `/typesafe/v1/systemone`;
  - 30-minute mixed load: two Qwen streams + one JevK5 request + one image job, with no OOM, driver reset, stalled stream or 5xx;
  - raw Halo ports unreachable from users;
  - Authentik and LiteLLM PostgreSQL restore tests.

nightly database dumps must be copied off the OpenMediaVault host. a dump sitting beside the database on the same machine is not a backup.

the only intentionally unresolved input is the final off-host backup destination; it does not change the platform architecture.
