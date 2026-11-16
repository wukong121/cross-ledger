#!/usr/bin/env bash
# PoC ONLY — create / delete a "domestic environment" simulation host.
#
# Plays the role of (A) a dev container on a domestic cloud ML platform and
# (B) a data team's transfer host. It is deliberately NOT part of the solution's
# IaC and NOT connected to the solution network:
#   * its own resource group + VNet, no peering
#   * reaches the source over the public internet via its static public IP
#   * no inbound ports at all; operated with `az vm run-command`
#   * rclone + Azure CLI; users sign in with their OWN Entra ID (device code)
#   * `rclone serve s3` on localhost = stand-in for the domestic S3-compatible object storage
#
#   ./scripts/sim-host.sh up      # prints the public IP -> add it to perimeter_allowed_ips
#   ./scripts/sim-host.sh down
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RG="${SIM_RG:-rg-xl-sim-host}"
VM="${SIM_VM:-vm-xl-sim}"
# Capacity for small sizes varies by region/time: try candidates in order.
LOCATIONS="${SIM_LOCATIONS:?set SIM_LOCATIONS, e.g. \"regionA regionB\"}"
SIZES="${SIM_SIZES:-Standard_B2s Standard_D2s_v5 Standard_D2as_v5 Standard_B2ms}"

up() {
  local src s3_secret tmp
  src="$(terraform -chdir="$ROOT/infra" output -raw source_account)"
  s3_secret="$(openssl rand -hex 16)"
  tmp="$(mktemp)"
  sed -e "s/\${source_account}/$src/g" -e "s/\${s3_secret}/$s3_secret/g" "$ROOT/scripts/sim/cloud-init.yaml" > "$tmp"
  for loc in $LOCATIONS; do
    az group create -n "$RG" -l "$loc" --tags solution=cross-ledger role=simulation -o none
    for size in $SIZES; do
      echo "==> trying $size in $loc"
      if az vm create -g "$RG" -n "$VM" -l "$loc" --size "$size" --image Ubuntu2404 \
           --admin-username xl --generate-ssh-keys --custom-data "$tmp" \
           --public-ip-sku Standard --public-ip-address-allocation static \
           --nsg-rule NONE --vnet-address-prefix 192.168.100.0/24 --subnet-address-prefix 192.168.100.0/26 \
           -o none 2>/tmp/sim-host.err; then
        rm -f "$tmp"
        echo "==> created $VM ($size, $loc)"
        echo "public IP: $(az vm show -d -g "$RG" -n "$VM" --query publicIps -o tsv)"
        echo "add it to infra/terraform.tfvars:  perimeter_allowed_ips = [\"<ip>/32\"]  then terraform apply"
        return 0
      fi
      grep -q -E "SkuNotAvailable|AllocationFailed|ZonalAllocationFailed|OverconstrainedAllocationRequest" /tmp/sim-host.err \
        || { cat /tmp/sim-host.err; exit 1; }
      az resource list -g "$RG" --query "[?type=='Microsoft.Compute/virtualMachines'].id" -o tsv | xargs -r az resource delete --ids >/dev/null
    done
    az group delete -n "$RG" --yes
  done
  echo "no capacity in: $LOCATIONS"; exit 1
}

down() { az group delete -n "$RG" --yes --no-wait && echo "deleting $RG"; }

"${1:-up}"
