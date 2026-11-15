#!/usr/bin/env bash
# One-shot deploy: infra (phase 1) -> build image in ACR -> infra (phase 2, real image) -> .xl.env
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/infra"

[[ -f terraform.tfvars ]] || { echo "infra/terraform.tfvars missing — copy terraform.tfvars.example"; exit 1; }

# One-time subscription prerequisite for VNet-injected Container Apps environments.
if [[ "$(az feature show --namespace Microsoft.Network --name AllowBringYourOwnPublicIpAddress --query properties.state -o tsv 2>/dev/null)" != "Registered" ]]; then
  echo "==> registering Microsoft.Network/AllowBringYourOwnPublicIpAddress (one-time)"
  az feature register --namespace Microsoft.Network --name AllowBringYourOwnPublicIpAddress -o none
  until [[ "$(az feature show --namespace Microsoft.Network --name AllowBringYourOwnPublicIpAddress --query properties.state -o tsv)" == "Registered" ]]; do sleep 20; done
  az provider register -n Microsoft.Network --wait
fi

terraform init -input=false -upgrade >/dev/null
existing_image="$(terraform output -raw image 2>/dev/null || true)"

echo "==> phase 1: infrastructure"
terraform apply -input=false -auto-approve ${existing_image:+-var "container_image=$existing_image"}

ACR="$(terraform output -raw acr_name)"
TAG="$(date -u +%Y%m%d%H%M%S)-$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo local)"
IMAGE="$(terraform output -raw acr_login_server)/crossledger:$TAG"

echo "==> building $IMAGE in ACR (no local Docker needed)"
az acr build --registry "$ACR" --image "crossledger:$TAG" --platform linux/amd64 "$ROOT/app" --no-logs -o none

echo "==> phase 2: roll jobs to the new image"
terraform apply -input=false -auto-approve -var "container_image=$IMAGE"

echo "==> writing $ROOT/.xl.env for the xl CLI"
terraform output -json cli_env | python3 -c '
import json, sys
for k, v in json.load(sys.stdin).items():
    print(f"{k}={v}")' > "$ROOT/.xl.env"
echo "done. try:  cd app && uv run xl roles"
