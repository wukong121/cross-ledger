data "azurerm_client_config" "current" {}
data "azurerm_subscription" "current" {}

resource "random_string" "suffix" {
  length  = 5
  upper   = false
  special = false
}

locals {
  gov_location    = coalesce(var.governance_location, var.source_location)
  ledger_location = coalesce(var.confidential_ledger_location, local.gov_location)
  sfx             = random_string.suffix.result
  name            = "${var.prefix}-${local.sfx}"
  # Storage account names: 3-24 lowercase alphanumerics.
  sa_name = "${var.prefix}${local.sfx}"

  # Placeholder used until the real image is built (see scripts/deploy.sh).
  image                = coalesce(var.container_image, "mcr.microsoft.com/k8se/quickstart-jobs:latest")
  image_is_placeholder = var.container_image == null
}

# Two resource groups = two blast radii with separate RBAC:
#   source  : the purchased dataset
#   gov     : transfer plane + governance / audit plane
resource "azurerm_resource_group" "source" {
  name     = "rg-${local.name}-source"
  location = var.source_location
  tags     = var.tags
}

resource "azurerm_resource_group" "gov" {
  name     = "rg-${local.name}-gov"
  location = local.gov_location
  tags     = var.tags
}
