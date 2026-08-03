# dev environment — module wiring lands per phase (see /PLAN.md).
#
# Phase 2:
#   module "network"    { source = "../../modules/network"    ... }
#   module "iam"        { source = "../../modules/iam"        ... }
#   module "data"       { source = "../../modules/data"       ... }
#   module "messaging"  { source = "../../modules/messaging"  ... }
# Phase 3:
#   module "api" { source = "../../modules/api-lambda" ... }
# Phase 4:
#   module "eks" { source = "../../modules/eks" ... }
# Phase 5:
#   module "monitoring" { source = "../../modules/monitoring" ... }
