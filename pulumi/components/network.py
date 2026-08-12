"""VPC: public + private subnets across N AZs, no NAT, free gateway endpoints.

No NAT gateway — deliberately. The chat Lambda runs OUTSIDE the VPC and reaches
Bedrock / DynamoDB / SQS over their public API endpoints; the KB uses serverless
S3 Vectors. Private subnets get local-only routing plus the free S3/DynamoDB
gateway endpoints. If a future in-VPC worker needs an AWS service, add a
per-service INTERFACE endpoint — never a blanket ~$32/mo NAT.
"""

from __future__ import annotations

import ipaddress

import pulumi_aws as aws

import pulumi


class Network(pulumi.ComponentResource):
    def __init__(
        self,
        prefix: str,
        region: str,
        azs: list[str],
        cidr: str = "10.20.0.0/16",
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("docpipe:network:Network", prefix, None, opts)
        me = pulumi.ResourceOptions(parent=self)

        self.vpc = aws.ec2.Vpc(
            f"{prefix}-vpc",
            cidr_block=cidr,
            enable_dns_support=True,
            enable_dns_hostnames=True,
            tags={"Name": f"{prefix}-vpc"},
            opts=me,
        )
        self.vpc_cidr = cidr

        igw = aws.ec2.InternetGateway(
            f"{prefix}-igw", vpc_id=self.vpc.id, tags={"Name": f"{prefix}-igw"}, opts=me
        )

        # cidrsubnet(vpc, 8, i) → carve the /16 into /24s.
        subnets = list(ipaddress.ip_network(cidr).subnets(new_prefix=24))
        n = len(azs)

        public_rt = aws.ec2.RouteTable(
            f"{prefix}-public-rt",
            vpc_id=self.vpc.id,
            routes=[{"cidr_block": "0.0.0.0/0", "gateway_id": igw.id}],
            tags={"Name": f"{prefix}-public-rt"},
            opts=me,
        )
        # Local-route-only: no 0.0.0.0/0 = no egress path (no NAT needed).
        private_rt = aws.ec2.RouteTable(
            f"{prefix}-private-rt",
            vpc_id=self.vpc.id,
            tags={"Name": f"{prefix}-private-rt"},
            opts=me,
        )

        self.public_subnet_ids: list[pulumi.Output[str]] = []
        self.private_subnet_ids: list[pulumi.Output[str]] = []
        for i, az in enumerate(azs):
            pub = aws.ec2.Subnet(
                f"{prefix}-public-{i}",
                vpc_id=self.vpc.id,
                availability_zone=az,
                cidr_block=str(subnets[i]),
                map_public_ip_on_launch=True,
                tags={"Name": f"{prefix}-public-{i}", "Tier": "public"},
                opts=me,
            )
            aws.ec2.RouteTableAssociation(
                f"{prefix}-public-rta-{i}", subnet_id=pub.id, route_table_id=public_rt.id, opts=me
            )
            self.public_subnet_ids.append(pub.id)

            # Private CIDRs offset by az_count so they don't overlap the public ones.
            priv = aws.ec2.Subnet(
                f"{prefix}-private-{i}",
                vpc_id=self.vpc.id,
                availability_zone=az,
                cidr_block=str(subnets[n + i]),
                tags={"Name": f"{prefix}-private-{i}", "Tier": "private"},
                opts=me,
            )
            aws.ec2.RouteTableAssociation(
                f"{prefix}-private-rta-{i}",
                subnet_id=priv.id,
                route_table_id=private_rt.id,
                opts=me,
            )
            self.private_subnet_ids.append(priv.id)

        # Free gateway endpoints — S3 + DynamoDB traffic over the AWS backbone.
        for svc in ("s3", "dynamodb"):
            aws.ec2.VpcEndpoint(
                f"{prefix}-{svc}-endpoint",
                vpc_id=self.vpc.id,
                service_name=f"com.amazonaws.{region}.{svc}",
                vpc_endpoint_type="Gateway",
                route_table_ids=[private_rt.id],
                tags={"Name": f"{prefix}-{svc}-endpoint"},
                opts=me,
            )

        self.register_outputs(
            {"vpc_id": self.vpc.id, "private_subnet_ids": self.private_subnet_ids}
        )
