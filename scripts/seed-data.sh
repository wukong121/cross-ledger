#!/usr/bin/env bash
# PoC: simulate the vendor delivering a dataset into the (private) source account.
# Runs the vendor-delivery Container Apps Job (vendor managed identity, inside the VNet)
# and waits for it. The delivery itself is recorded in the ledger as `dataset.delivered`.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/infra"
RG="$(terraform output -json resource_groups | python3 -c 'import json,sys;print(json.load(sys.stdin)["gov"])')"
JOB="$(terraform output -json jobs | python3 -c 'import json,sys;print(json.load(sys.stdin)["vendor"])')"

EXEC="$(az containerapp job start -n "$JOB" -g "$RG" --query name -o tsv)"
echo "==> vendor delivery running: $EXEC"
while :; do
  STATUS="$(az containerapp job execution show -n "$JOB" -g "$RG" --job-execution-name "$EXEC" --query properties.status -o tsv)"
  case "$STATUS" in
    Succeeded) echo "==> delivered"; exit 0 ;;
    Failed|Stopped|Degraded) echo "==> $STATUS — see: az containerapp job logs show -n $JOB -g $RG --execution $EXEC --container vendor"; exit 1 ;;
    *) sleep 15 ;;
  esac
done
