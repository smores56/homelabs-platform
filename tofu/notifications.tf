# Tunnel health for both tunnels: the host tunnel (nix-config) and the cluster
# tunnel. An empty cluster_tunnel_id is dropped, because a notification filter
# with an empty tunnel id fails validation.
resource "cloudflare_notification_policy" "tunnel_health" {
  account_id = var.account_id
  name       = "tunnel health"
  enabled    = true
  alert_type = "tunnel_health_event"

  filters = {
    tunnel_id = compact([var.host_tunnel_id, var.cluster_tunnel_id])
  }

  mechanisms = {
    email = [{
      id = var.email
    }]
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_notification_policy" "service_token_expiry" {
  account_id = var.account_id
  name       = "service token expiry"
  enabled    = true
  alert_type = "expiring_service_token_alert"

  mechanisms = {
    email = [{
      id = var.email
    }]
  }

  lifecycle {
    prevent_destroy = true
  }
}
