#!/usr/bin/env bash
# PoC: generate a sample dataset in the (private) source account.
# Runs the seed Container Apps Job (dedicated managed identity, inside the VNet)
# and waits for it. The load itself is recorded in the ledger as `dataset.loaded`.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/infra"
RG="$(terraform output -json resource_groups | python3 -c 'import json,sys;print(json.load(sys.stdin)["gov"])')"
JOB="$(terraform output -json jobs | python3 -c 'import json,sys;print(json.load(sys.stdin)["seed"])')"

EXEC="$(az containerapp job start -n "$JOB" -g "$RG" --query name -o tsv)"
echo "==> sample data load running: $EXEC"
while :; do
  STATUS="$(az containerapp job execution show -n "$JOB" -g "$RG" --job-execution-name "$EXEC" --query properties.status -o tsv)"
  case "$STATUS" in
    Succeeded) echo "==> loaded"; exit 0 ;;
    Failed|Stopped|Degraded) echo "==> $STATUS — see: az containerapp job logs show -n $JOB -g $RG --execution $EXEC --container seed"; exit 1 ;;
    *) sleep 15 ;;
  esac
done
