# module: eks (Phase 4)

EKS cluster in private subnets, one small managed node group, OIDC provider +
IRSA binding for the worker service account. Workloads (Helm) are deployed
outside Terraform — this module owns only the cluster.
