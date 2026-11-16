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
#   ./scripts/sim-host.sh expose <worker-egress-ip>/32 > destination.json   # managed transfer PoC
#   ./scripts/sim-host.sh down
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RG="${SIM_RG:-rg-xl-sim-host}"
VM="${SIM_VM:-vm-xl-sim}"
# Capacity for small sizes varies by region/time: try candidates in order.
LOCATIONS="${SIM_LOCATIONS:-}"
SIZES="${SIM_SIZES:-Standard_B2s Standard_D2s_v5 Standard_D2as_v5 Standard_B2ms}"

up() {
  [[ -n "$LOCATIONS" ]] || { echo "set SIM_LOCATIONS, e.g. \"regionA regionB\""; exit 1; }
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

# Expose the host's S3-compatible endpoint over TLS to ONE source CIDR (the managed
# transfer worker's fixed egress IP) and print the destination secret for Key Vault.
#   ./scripts/sim-host.sh expose <egress-ip>/32 > /tmp/destination.json
expose() {
  local allowed="${1:?allowed CIDR, e.g. the worker egress IP /32}" ip
  ip="$(az vm show -d -g "$RG" -n "$VM" --query publicIps -o tsv)"
  # Traffic must pass every NSG on the path (NIC and subnet — some subscriptions attach a
  # subnet NSG automatically), so open 9443 for the worker's egress IP on all of them.
  for nsg in $(az network nsg list -g "$RG" --query "[].name" -o tsv); do
    az network nsg rule create -g "$RG" --nsg-name "$nsg" -n allow-s3-tls-from-worker --priority 200 \
      --direction Inbound --access Allow --protocol Tcp --destination-port-ranges 9443 \
      --source-address-prefixes "$allowed" -o none >&2
  done
  az vm run-command invoke -g "$RG" -n "$VM" --command-id RunShellScript --query "value[0].message" -o tsv --scripts "
set -e
mkdir -p /etc/sim-s3 && cd /etc/sim-s3
if [ ! -f ca.pem ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj '/CN=sim-s3-ca' -keyout ca.key -out ca.pem 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj '/CN=sim-s3' -keyout server.key -out server.csr 2>/dev/null
  printf 'subjectAltName=IP:$ip' > san.ext
  openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 30 -extfile san.ext -out server.pem 2>/dev/null
fi
SECRET=\$(grep secret_access_key /root/.config/rclone/rclone.conf | awk '{print \$3}')
cat > /etc/systemd/system/sim-s3-tls.service <<UNIT
[Unit]
Description=Public TLS S3-compatible endpoint (stand-in for domestic object storage)
After=network-online.target
[Service]
ExecStart=/usr/bin/rclone serve s3 /data/s3 --addr :9443 --cert /etc/sim-s3/server.pem --key /etc/sim-s3/server.key --auth-key xlsimaccesskey,\$SECRET
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
mkdir -p /data/s3/managed
systemctl daemon-reload && systemctl enable --now sim-s3-tls >/dev/null 2>&1 && sleep 2
python3 -c \"import json,sys; print(json.dumps({'name':'domestic','bucket':'managed','type':'s3','provider':'Rclone','endpoint':'https://$ip:9443','access_key_id':'xlsimaccesskey','secret_access_key':sys.argv[1],'ca_cert_pem':open('/etc/sim-s3/ca.pem').read()}))\" \"\$SECRET\"
" | sed -n '/^{/p'
}

down() { az group delete -n "$RG" --yes --no-wait && echo "deleting $RG"; }

cmd="${1:-up}"; shift || true
"$cmd" "$@"
