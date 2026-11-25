output "resource_groups" {
  value = { for k, v in local.rgs : k => split("/", v)[4] }
}

output "acr_name" {
  value = azurerm_container_registry.this.name
}

output "acr_login_server" {
  value = azurerm_container_registry.this.login_server
}

output "source_account" {
  value = azurerm_storage_account.source.name
}

output "groups" {
  value = { for k, g in azuread_group.this : k => { name = g.display_name, id = g.object_id } }
}

output "jobs" {
  value = {
    transfer  = local.managed_transfer ? azapi_resource.job_transfer[0].name : null
    seed      = azapi_resource.job_seed.name
    collector = azapi_resource.job_collector.name
    connector = local.ticket_intake ? azapi_resource.connector[0].name : null
  }
}

output "workbook_id" {
  value = azurerm_application_insights_workbook.ledger.id
}

# Consumed by the `xl` CLI (scripts/deploy.sh writes it to .xl.env).
output "cli_env" {
  value = {
    XL_SUBSCRIPTION_ID      = var.subscription_id
    XL_GOV_RG               = azurerm_resource_group.gov.name
    XL_WORKSPACE_ID         = azurerm_log_analytics_workspace.this.workspace_id
    XL_BUS_NAMESPACE        = local.managed_transfer ? "${local.bus_namespace_name}.servicebus.windows.net" : ""
    XL_BUS_QUEUE            = local.bus_queue_name
    XL_SOURCE_ACCOUNT_URL   = azurerm_storage_account.source.primary_blob_endpoint
    XL_SOURCE_CONTAINER     = azurerm_storage_container.source_datasets.name
    XL_TRANSFER_JOB         = local.managed_transfer ? azapi_resource.job_transfer[0].name : ""
    XL_KEY_VAULT_ID         = local.key_vault_id
    XL_KEY_VAULT_URI        = local.key_vault_uri
    XL_SEED_JOB             = azapi_resource.job_seed.name
    XL_LEDGER_ENDPOINT      = var.enable_confidential_ledger ? azurerm_confidential_ledger.this[0].ledger_endpoint : ""
    XL_ROLES                = jsonencode({ for k, r in local.data_roles : k => { scope = r.scope, role = r.role } })
    XL_SELF_SERVICE         = tostring(var.enable_self_service)
    XL_DATASETS             = jsonencode(var.datasets)
    XL_FEISHU_APP_ID        = local.ticket_intake ? var.feishu_app_id : ""
    XL_FEISHU_APPROVAL_CODE = var.feishu_approval_code
    XL_FEISHU_DOMAIN        = var.feishu_domain
    XL_FEISHU_FIELDS        = jsonencode(var.feishu_fields)
    XL_CONNECTOR_APP        = local.ticket_intake ? azapi_resource.connector[0].name : ""
  }
}

output "image" {
  value = var.container_image
}

output "source_public_access" {
  value = {
    mode          = var.source_public_access
    allowed_cidrs = local.perimeter_enabled ? local.perimeter_ips : []
  }
}

output "managed_transfer" {
  value = local.managed_transfer ? {
    egress_ip = azurerm_public_ip.egress[0].ip_address
    key_vault = azurerm_key_vault.this[0].name
  } : null
}
