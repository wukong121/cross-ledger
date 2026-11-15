#!/usr/bin/env bash
# Tear down every PoC resource (resource groups, Entra groups, PIM policies).
# NOTE: the ledger WORM container has a time-based retention policy. While it is
# *unlocked* (default) Terraform can delete it; once locked, the storage account
# cannot be deleted until all blobs have aged out — by design.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/infra"
IMAGE="$(terraform output -raw image 2>/dev/null || true)"
terraform destroy -input=false ${IMAGE:+-var "container_image=$IMAGE"} "$@"
rm -f "$ROOT/.xl.env"
