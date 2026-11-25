# -----------------------------------------------------------------------------
# Ticket connector (intake = "feishu"): an always-on Container App with NO ingress.
#   * holds the Feishu long connection (outbound websocket through the NAT egress IP)
#   * records every approval-instance event on the `ticket-events` queue, then
#   * re-reads the instance through the Feishu API, re-checks it against the dataset /
#     destination registries and the IT approver list, writes the ledger and queues
#     exactly one transfer shard per approved ticket.
# Its identity can read the Feishu App Secret and the registry *tags* — never the
# destination credentials (only the transfer worker can).
# -----------------------------------------------------------------------------

resource "azurerm_user_assigned_identity" "connector" {
  name                = "id-${local.name}-connector"
  resource_group_name = azurerm_resource_group.gov.name
  location            = azurerm_resource_group.gov.location
  tags                = var.tags
}

resource "azapi_resource" "connector" {
  count     = local.ticket_intake ? 1 : 0
  type      = "Microsoft.App/containerApps@2025-01-01"
  name      = "ca-${local.name}-connector"
  parent_id = azurerm_resource_group.gov.id
  location  = azurerm_resource_group.gov.location
  tags      = var.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.connector.id]
  }

  ignore_null_property = true
  body = {
    properties = {
      environmentId       = azurerm_container_app_environment.this.id
      workloadProfileName = "Consumption"
      configuration = {
        activeRevisionsMode = "Single"
        # no ingress: the connector only makes outbound connections
        registries = local.image_is_placeholder ? [] : [{
          server   = azurerm_container_registry.this.login_server
          identity = azurerm_user_assigned_identity.connector.id
        }]
      }
      template = {
        containers = [{
          name      = "connector"
          image     = local.image_is_placeholder ? "mcr.microsoft.com/k8se/quickstart:latest" : local.image
          command   = local.image_is_placeholder ? null : ["crossledger", "connector"]
          resources = { cpu = 0.25, memory = "0.5Gi" }
          env = concat(local.common_env, [
            { name = "AZURE_CLIENT_ID", value = azurerm_user_assigned_identity.connector.client_id },
          ])
        }]
        # exactly one long connection (Feishu pushes each event to one random connection)
        scale = { minReplicas = 1, maxReplicas = 1 }
      }
    }
  }

  depends_on = [azurerm_role_assignment.standing, azurerm_private_endpoint.blob]

  lifecycle {
    replace_triggered_by = [azurerm_container_app_environment.this.id]
  }
}
