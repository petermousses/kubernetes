#!/usr/bin/env bash
set -euo pipefail

readonly test_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly host_root="$(cd -- "${test_root}/.." && pwd)"
readonly repo_root="$(cd -- "${host_root}/../.." && pwd)"
readonly router_config="${host_root}/model-router/config.yaml"
readonly litellm_config="${repo_root}/apps/litellm/configmap.yaml"

readonly -a text_models=(
  qwen3.8-27b
  gemma-4-e4b-it
  gemma-4-12b-it
  gemma-4-26b-a4b-it
  qwen3.6-35b-a3b
  glm-5.3-flash-abliterated
)
readonly -a image_models=(qwen-image-2.1 qwen-image-2.1-uncensored)

for model in "${text_models[@]}"; do
  grep -Fq "  ${model}:" "${router_config}"
  grep -Fq -- "--alias ${model}" "${router_config}"
  grep -Fq "      - ${model}" "${router_config}"
  grep -Fq "      - model_name: ${model}" "${litellm_config}"
done

for model in "${image_models[@]}"; do
  grep -Fq "      - model_name: ${model}" "${litellm_config}"
done

grep -Fq '  - "${env.QWEN_API_KEY}"' "${router_config}"
grep -Fq 'globalTTL: 300' "${router_config}"
grep -Fq '          swap: true' "${router_config}"
grep -Fq '          exclusive: true' "${router_config}"
grep -Fq '      store_model_in_db: false' "${litellm_config}"
if grep -Fq 'deepseek-v4.1-flash-q2' "${router_config}" \
  || grep -Fq 'deepseek-v4.1-flash-q2' "${litellm_config}"; then
  printf 'DeepSeek is not eligible for registration until its weights verify\n' >&2
  exit 1
fi

for file in \
  "${host_root}/model-router/create-env.sh" \
  "${host_root}/model-router/install-glm53-runtime.sh" \
  "${host_root}/model-router/glm-5.3-flash-abliterated-launcher.sh" \
  "${host_root}/model-router/install.sh" \
  "${host_root}/comfyui/install-qwen-image-gguf.sh"; do
  bash -n "${file}"
done

printf 'static model-router and image-pipeline checks passed\n'
