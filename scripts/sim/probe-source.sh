#!/usr/bin/env bash
# Probe the source container's data plane with the caller's own Entra token and print
# the raw service answer (az CLI hides whether RBAC or the network perimeter refused).
set -euo pipefail
ACCOUNT="${1:?storage account}"
TOKEN="$(az account get-access-token --resource https://storage.azure.com/ --query accessToken -o tsv)"
echo "egress IP: $(curl -s https://ifconfig.me)"
curl -s -w "\nHTTP %{http_code}\n" -H "Authorization: Bearer $TOKEN" -H "x-ms-version: 2023-11-03" \
  "https://$ACCOUNT.blob.core.windows.net/datasets?restype=container&comp=list&maxresults=3" \
  | sed -E 's#.*<Code>([^<]*)</Code><Message>([^.]*\.).*#\1: \2#' | grep -v -E '^(RequestId|Time)'
