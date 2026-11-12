# Public identifiers for the zone, so an apply needs no exports. The provider
# credential is the only input that still comes from the environment; everything
# here is an identifier that docs/ prints anyway, and the tests assert this file
# carries no credential.
#
# The cluster host set is not here on purpose: it follows `local.cluster_ingress`
# in tofu/cluster.tf, so DNS moves with the tunnel route.

account_id        = "385f7d47e2e8438fbda5d6f76d760434"
domain            = "sammohr.dev"
email             = "sam@sammohr.dev"
host_tunnel_id    = "583026c2-4efb-453b-8392-3ce6dfcfa0d0"
cluster_tunnel_id = "16a461e2-b28e-4535-b79d-eca2f6e9b001"

# Empty until the in-VM CSR client has registered; see docs/manual-steps.md
# step 16. The CAA records are created only once this is set.
acme_account_id = ""
