# -----------------------------------------------------------------------------
# Transfer plane: serverless Container Apps Jobs.
#   job-transfer  : OPTIONAL (enable_managed_transfer); event-driven (KEDA Service Bus scaler); one execution per
#                   queued shard; rclone source -> destination object storage + verification.
#   job-collector : scheduled; turns PIM / Activity / Storage logs into ledger
#                   events.
#   job-vendor    : PoC only; manual; simulates the supplier delivering data.
# All run as user-assigned managed identities — no keys, no SAS, no secrets.
# -----------------------------------------------------------------------------

resource "azurerm_container_registry" "this" {
  name                = "acr${local.sa_name}"
  resource_group_name = azurerm_resource_group.gov.name
  location            = azurerm_resource_group.gov.location
  sku                 = "Basic"
  admin_enabled       = false
  tags                = var.tags
}

resource "azurerm_container_app_environment" "this" {
  name                       = "cae-${local.name}"
  resource_group_name        = azurerm_resource_group.gov.name
  location                   = azurerm_resource_group.gov.location
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id
  infrastructure_subnet_id   = azurerm_subnet.cae.id
  tags                       = var.tags

  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
  }
}

locals {
  common_env = [
    { name = "XL_SOURCE_ACCOUNT_URL", value = azurerm_storage_account.source.primary_blob_endpoint },
    { name = "XL_SOURCE_CONTAINER", value = azurerm_storage_container.source_datasets.name },
    { name = "XL_BUS_NAMESPACE", value = var.enable_managed_transfer ? "${local.bus_namespace_name}.servicebus.windows.net" : "" },
    { name = "XL_BUS_QUEUE", value = local.bus_queue_name },
    { name = "XL_STATE_CONTAINER_URL", value = "${azurerm_storage_account.ctl.primary_blob_endpoint}${azurerm_storage_container.state.name}" },
    { name = "XL_AUDIT_CONTAINER_URL", value = "${azurerm_storage_account.audit.primary_blob_endpoint}${azurerm_storage_container.ledger.name}" },
    { name = "XL_DCE_ENDPOINT", value = azurerm_monitor_data_collection_endpoint.this.logs_ingestion_endpoint },
    { name = "XL_DCR_ID", value = azurerm_monitor_data_collection_rule.ledger.immutable_id },
    { name = "XL_DCR_STREAM", value = local.ledger_stream },
    { name = "XL_WORKSPACE_ID", value = azurerm_log_analytics_workspace.this.workspace_id },
    { name = "XL_LEDGER_ENDPOINT", value = var.enable_confidential_ledger ? azurerm_confidential_ledger.this[0].ledger_endpoint : "" },
    { name = "XL_SUBSCRIPTION_ID", value = var.subscription_id },
    { name = "XL_SCOPES", value = join(",", values(local.rgs)) },
    { name = "XL_SOURCE_SCOPE", value = azurerm_storage_account.source.id },
    { name = "XL_CTL_SCOPE", value = local.bus_namespace_id },
    { name = "XL_OPERATOR_ROLE_ID", value = local.role_ids.bus_sender },
    { name = "XL_KNOWN_SERVICES", value = jsonencode(var.known_service_principals) },
    { name = "XL_SERVICE_IDENTITIES", value = join(",", [azurerm_user_assigned_identity.transfer.principal_id, azurerm_user_assigned_identity.collector.principal_id, azurerm_user_assigned_identity.vendor.principal_id]) },
  ]

  registries = local.image_is_placeholder ? [] : [{
    server   = azurerm_container_registry.this.login_server
    identity = azurerm_user_assigned_identity.transfer.id
  }]
}

resource "azapi_resource" "job_transfer" {
  count     = var.enable_managed_transfer ? 1 : 0
  type      = "Microsoft.App/jobs@2025-01-01"
  name      = "job-${local.name}-transfer"
  parent_id = azurerm_resource_group.gov.id
  location  = azurerm_resource_group.gov.location
  tags      = var.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.transfer.id]
  }

  ignore_null_property = true
  body = {
    properties = {
      environmentId       = azurerm_container_app_environment.this.id
      workloadProfileName = "Consumption"
      configuration = {
        triggerType       = "Event"
        replicaTimeout    = 86400 # 24h per shard; shards are idempotent and resumable
        replicaRetryLimit = 1
        registries        = local.registries
        eventTriggerConfig = {
          parallelism            = 1
          replicaCompletionCount = 1
          scale = {
            minExecutions   = 0
            maxExecutions   = var.transfer_parallelism
            pollingInterval = 30
            rules = [{
              name = "transfer-requests"
              type = "azure-servicebus"
              metadata = {
                namespace    = local.bus_namespace_name
                queueName    = local.bus_queue_name
                messageCount = "1"
              }
              identity = azurerm_user_assigned_identity.transfer.id
            }]
          }
        }
      }
      template = {
        containers = [{
          name    = "transfer"
          image   = local.image
          command = local.image_is_placeholder ? null : ["crossledger", "worker"]
          resources = {
            cpu    = 2
            memory = "4Gi"
          }
          env = concat(local.common_env, [
            { name = "AZURE_CLIENT_ID", value = azurerm_user_assigned_identity.transfer.client_id },
            { name = "XL_WORKER_IDENTITY", value = azurerm_user_assigned_identity.transfer.principal_id },
            { name = "XL_DEST_SECRET_URI", value = local.dest_secret_uri },
            { name = "XL_RCLONE_TRANSFERS", value = tostring(var.rclone_transfers) },
          ])
        }]
      }
    }
  }

  depends_on = [azurerm_role_assignment.standing, azurerm_private_endpoint.blob]

  lifecycle {
    # Jobs live inside the environment: rebuild them whenever it is replaced.
    replace_triggered_by = [azurerm_container_app_environment.this.id]
  }
}

resource "azapi_resource" "job_collector" {
  type      = "Microsoft.App/jobs@2025-01-01"
  name      = "job-${local.name}-collector"
  parent_id = azurerm_resource_group.gov.id
  location  = azurerm_resource_group.gov.location
  tags      = var.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.collector.id]
  }

  ignore_null_property = true
  body = {
    properties = {
      environmentId       = azurerm_container_app_environment.this.id
      workloadProfileName = "Consumption"
      configuration = {
        triggerType       = "Schedule"
        replicaTimeout    = 1200
        replicaRetryLimit = 0
        registries = local.image_is_placeholder ? [] : [{
          server   = azurerm_container_registry.this.login_server
          identity = azurerm_user_assigned_identity.collector.id
        }]
        scheduleTriggerConfig = {
          cronExpression         = var.collector_cron
          parallelism            = 1
          replicaCompletionCount = 1
        }
      }
      template = {
        containers = [{
          name    = "collector"
          image   = local.image
          command = local.image_is_placeholder ? null : ["crossledger", "collect"]
          resources = {
            cpu    = 0.5
            memory = "1Gi"
          }
          env = concat(local.common_env, [
            { name = "AZURE_CLIENT_ID", value = azurerm_user_assigned_identity.collector.client_id },
            { name = "XL_WORKER_IDENTITY", value = azurerm_user_assigned_identity.transfer.principal_id },
          ])
        }]
      }
    }
  }

  depends_on = [azurerm_role_assignment.standing, azurerm_private_endpoint.blob]

  lifecycle {
    # Jobs live inside the environment: rebuild them whenever it is replaced.
    replace_triggered_by = [azurerm_container_app_environment.this.id]
  }
}

resource "azapi_resource" "job_vendor" {
  type                 = "Microsoft.App/jobs@2025-01-01"
  name                 = "job-${local.name}-vendor"
  parent_id            = azurerm_resource_group.gov.id
  location             = azurerm_resource_group.gov.location
  tags                 = var.tags
  ignore_null_property = true

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.vendor.id]
  }

  body = {
    properties = {
      environmentId       = azurerm_container_app_environment.this.id
      workloadProfileName = "Consumption"
      configuration = {
        triggerType       = "Manual"
        replicaTimeout    = 1800
        replicaRetryLimit = 0
        registries = local.image_is_placeholder ? [] : [{
          server   = azurerm_container_registry.this.login_server
          identity = azurerm_user_assigned_identity.vendor.id
        }]
        manualTriggerConfig = { parallelism = 1, replicaCompletionCount = 1 }
      }
      template = {
        containers = [{
          name      = "vendor"
          image     = local.image
          command   = local.image_is_placeholder ? null : ["crossledger", "seed"]
          resources = { cpu = 0.5, memory = "1Gi" }
          env = concat(local.common_env, [
            { name = "AZURE_CLIENT_ID", value = azurerm_user_assigned_identity.vendor.client_id },
            { name = "XL_SEED_BATCHES", value = var.seed_batches },
            { name = "XL_SEED_IDENTITY", value = azurerm_user_assigned_identity.vendor.principal_id },
          ])
        }]
      }
    }
  }

  depends_on = [azurerm_role_assignment.standing, azurerm_private_endpoint.blob]

  lifecycle {
    # Jobs live inside the environment: rebuild them whenever it is replaced.
    replace_triggered_by = [azurerm_container_app_environment.this.id]
  }
}

moved {
  from = azapi_resource.job_transfer
  to   = azapi_resource.job_transfer[0]
}
