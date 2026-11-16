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

output "landing_account" {
  value = azurerm_storage_account.landing.name
}

output "groups" {
  value = { for k, g in azuread_group.this : k => { name = g.display_name, id = g.object_id } }
}

output "jobs" {
  value = {
    transfer  = var.enable_managed_transfer ? azapi_resource.job_transfer[0].name : null
    vendor    = azapi_resource.job_vendor.name
    collector = azapi_resource.job_collector.name
  }
}

output "workbook_id" {
  value = azurerm_application_insights_workbook.ledger.id
}

# Consumed by the `xl` CLI (scripts/deploy.sh writes it to .xl.env).
output "cli_env" {
  value = {
    XL_SUBSCRIPTION_ID     = var.subscription_id
    XL_GOV_RG              = azurerm_resource_group.gov.name
    XL_WORKSPACE_ID        = azurerm_log_analytics_workspace.this.workspace_id
    XL_BUS_NAMESPACE       = var.enable_managed_transfer ? "${local.bus_namespace_name}.servicebus.windows.net" : ""
    XL_BUS_QUEUE           = local.bus_queue_name
    XL_SOURCE_ACCOUNT_URL  = azurerm_storage_account.source.primary_blob_endpoint
    XL_LANDING_ACCOUNT_URL = azurerm_storage_account.landing.primary_blob_endpoint
    XL_SOURCE_CONTAINER    = azurerm_storage_container.source_datasets.name
    XL_LANDING_CONTAINER   = azurerm_storage_container.landing_datasets.name
    XL_TRANSFER_JOB        = var.enable_managed_transfer ? azapi_resource.job_transfer[0].name : ""
    XL_VENDOR_JOB          = azapi_resource.job_vendor.name
    XL_LEDGER_ENDPOINT     = var.enable_confidential_ledger ? azurerm_confidential_ledger.this[0].ledger_endpoint : ""
    XL_ROLES               = jsonencode({ for k, r in local.data_roles : k => { scope = r.scope, role = r.role } })
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
