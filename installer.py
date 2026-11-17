#!/usr/bin/env python3
"""
SageMaker Unified Studio (smus-kr) installer.

Creates VPC + Bedrock PrivateLink, IAM roles (agentic-work naming),
DataZone V2 domain with IdC SSO hooks, enables Tooling/DataLake blueprints,
creates default project profile + policy grants, usage-monitoring stack,
then writes config.json and invokes dashboard.py.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional, Sequence

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

# ---------------------------------------------------------------------------
# Constants (agentic-work naming style)
# ---------------------------------------------------------------------------
PROJECT_NAME = "smus-kr"
REGION = "ap-northeast-1"
DOMAIN_NAME = "smus-kr"
DOMAIN_VERSION = "V2"
ALLOWED_INSTANCE_TYPES = ["ml.t3.medium", "ml.t3.large", "ml.t3.xlarge"]
# Notebooks managed compute (sc.*) — post-hoc StopCompute allowlist
ALLOWED_NOTEBOOK_INSTANCE_TYPES = ["sc.t3.medium", "sc.t3.large", "sc.t3.xlarge"]
# StudioWebPortalSettings.HiddenInstanceTypes (API enum). Allowed types are excluded.
_STUDIO_INSTANCE_TYPE_ENUM = [
    "ml.t3.micro", "ml.t3.small", "ml.t3.medium", "ml.t3.large", "ml.t3.xlarge", "ml.t3.2xlarge",
    "ml.m5.large", "ml.m5.xlarge", "ml.m5.2xlarge", "ml.m5.4xlarge", "ml.m5.8xlarge",
    "ml.m5.12xlarge", "ml.m5.16xlarge", "ml.m5.24xlarge",
    "ml.m5d.large", "ml.m5d.xlarge", "ml.m5d.2xlarge", "ml.m5d.4xlarge", "ml.m5d.8xlarge",
    "ml.m5d.12xlarge", "ml.m5d.16xlarge", "ml.m5d.24xlarge",
    "ml.c5.large", "ml.c5.xlarge", "ml.c5.2xlarge", "ml.c5.4xlarge", "ml.c5.9xlarge",
    "ml.c5.12xlarge", "ml.c5.18xlarge", "ml.c5.24xlarge",
    "ml.p3.2xlarge", "ml.p3.8xlarge", "ml.p3.16xlarge", "ml.p3dn.24xlarge",
    "ml.g4dn.xlarge", "ml.g4dn.2xlarge", "ml.g4dn.4xlarge", "ml.g4dn.8xlarge",
    "ml.g4dn.12xlarge", "ml.g4dn.16xlarge",
    "ml.r5.large", "ml.r5.xlarge", "ml.r5.2xlarge", "ml.r5.4xlarge", "ml.r5.8xlarge",
    "ml.r5.12xlarge", "ml.r5.16xlarge", "ml.r5.24xlarge",
    "ml.g5.xlarge", "ml.g5.2xlarge", "ml.g5.4xlarge", "ml.g5.8xlarge", "ml.g5.16xlarge",
    "ml.g5.12xlarge", "ml.g5.24xlarge", "ml.g5.48xlarge",
    "ml.g6.xlarge", "ml.g6.2xlarge", "ml.g6.4xlarge", "ml.g6.8xlarge", "ml.g6.12xlarge",
    "ml.g6.16xlarge", "ml.g6.24xlarge", "ml.g6.48xlarge",
    "ml.g6e.xlarge", "ml.g6e.2xlarge", "ml.g6e.4xlarge", "ml.g6e.8xlarge", "ml.g6e.12xlarge",
    "ml.g6e.16xlarge", "ml.g6e.24xlarge", "ml.g6e.48xlarge",
    "ml.geospatial.interactive", "ml.p4d.24xlarge", "ml.p4de.24xlarge",
    "ml.trn1.2xlarge", "ml.trn1.32xlarge", "ml.trn1n.32xlarge",
    "ml.p5.48xlarge", "ml.p5en.48xlarge", "ml.p5.4xlarge", "ml.p6-b200.48xlarge",
    "ml.m6i.large", "ml.m6i.xlarge", "ml.m6i.2xlarge", "ml.m6i.4xlarge", "ml.m6i.8xlarge",
    "ml.m6i.12xlarge", "ml.m6i.16xlarge", "ml.m6i.24xlarge", "ml.m6i.32xlarge",
    "ml.m7i.large", "ml.m7i.xlarge", "ml.m7i.2xlarge", "ml.m7i.4xlarge", "ml.m7i.8xlarge",
    "ml.m7i.12xlarge", "ml.m7i.16xlarge", "ml.m7i.24xlarge", "ml.m7i.48xlarge",
    "ml.c6i.large", "ml.c6i.xlarge", "ml.c6i.2xlarge", "ml.c6i.4xlarge", "ml.c6i.8xlarge",
    "ml.c6i.12xlarge", "ml.c6i.16xlarge", "ml.c6i.24xlarge", "ml.c6i.32xlarge",
    "ml.c7i.large", "ml.c7i.xlarge", "ml.c7i.2xlarge", "ml.c7i.4xlarge", "ml.c7i.8xlarge",
    "ml.c7i.12xlarge", "ml.c7i.16xlarge", "ml.c7i.24xlarge", "ml.c7i.48xlarge",
    "ml.r6i.large", "ml.r6i.xlarge", "ml.r6i.2xlarge", "ml.r6i.4xlarge", "ml.r6i.8xlarge",
    "ml.r6i.12xlarge", "ml.r6i.16xlarge", "ml.r6i.24xlarge", "ml.r6i.32xlarge",
    "ml.r7i.large", "ml.r7i.xlarge", "ml.r7i.2xlarge", "ml.r7i.4xlarge", "ml.r7i.8xlarge",
    "ml.r7i.12xlarge", "ml.r7i.16xlarge", "ml.r7i.24xlarge", "ml.r7i.48xlarge",
    "ml.m6id.large", "ml.m6id.xlarge", "ml.m6id.2xlarge", "ml.m6id.4xlarge", "ml.m6id.8xlarge",
    "ml.m6id.12xlarge", "ml.m6id.16xlarge", "ml.m6id.24xlarge", "ml.m6id.32xlarge",
    "ml.c6id.large", "ml.c6id.xlarge", "ml.c6id.2xlarge", "ml.c6id.4xlarge", "ml.c6id.8xlarge",
    "ml.c6id.12xlarge", "ml.c6id.16xlarge", "ml.c6id.24xlarge", "ml.c6id.32xlarge",
    "ml.r6id.large", "ml.r6id.xlarge", "ml.r6id.2xlarge", "ml.r6id.4xlarge", "ml.r6id.8xlarge",
    "ml.r6id.12xlarge", "ml.r6id.16xlarge", "ml.r6id.24xlarge", "ml.r6id.32xlarge",
    "ml.g7.2xlarge", "ml.g7.4xlarge", "ml.g7.8xlarge", "ml.g7.12xlarge", "ml.g7.24xlarge",
    "ml.g7.48xlarge",
    "ml.g7e.2xlarge", "ml.g7e.4xlarge", "ml.g7e.8xlarge", "ml.g7e.12xlarge", "ml.g7e.24xlarge",
    "ml.g7e.48xlarge",
]
# Studio API allows max 20 HiddenInstanceTypes — prioritize common alternatives to t3.
HIDDEN_INSTANCE_TYPES = [
    t
    for t in [
        "ml.r5.large", "ml.r5.xlarge", "ml.r5.2xlarge", "ml.r5.4xlarge",
        "ml.m5.large", "ml.m5.xlarge", "ml.m5.2xlarge", "ml.m5.4xlarge",
        "ml.c5.large", "ml.c5.xlarge", "ml.c5.2xlarge", "ml.c5.4xlarge",
        "ml.t3.2xlarge", "ml.t3.micro", "ml.t3.small",
        "ml.g4dn.xlarge", "ml.g5.xlarge", "ml.g5.2xlarge",
        "ml.r6i.large", "ml.m6i.large",
    ]
    if t not in ALLOWED_INSTANCE_TYPES
][:20]
PROJECT_USER_ROLE_PREFIX = "datazone_usr_role_"
VPC_CIDR = "10.80.0.0/16"
PRIVATE_SUBNET_CIDRS = ["10.80.10.0/24", "10.80.11.0/24", "10.80.12.0/24"]
PUBLIC_SUBNET_CIDRS = ["10.80.0.0/24", "10.80.1.0/24"]

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")

ROLE_DOMAIN_EXEC = f"role-domain-execution-for-{PROJECT_NAME}-{REGION}"
ROLE_DOMAIN_SVC = f"role-domain-service-for-{PROJECT_NAME}-{REGION}"
ROLE_PROVISIONING = f"role-provisioning-for-{PROJECT_NAME}-{REGION}"
ROLE_MANAGE_ACCESS = f"role-manage-access-for-{PROJECT_NAME}-{REGION}"
ROLE_MODEL_PROV = f"role-model-provisioning-for-{PROJECT_NAME}-{REGION}"
ROLE_MODEL_CONS = f"role-model-consumption-for-{PROJECT_NAME}-{REGION}"
ROLE_LAMBDA_USAGE = f"role-lambda-usage-monitor-for-{PROJECT_NAME}-{REGION}"
POLICY_GUARDRAILS = f"policy-project-user-guardrails-for-{PROJECT_NAME}-{REGION}"
LAMBDA_USAGE = f"lambda-usage-monitor-for-{PROJECT_NAME}"
LAMBDA_COST = f"lambda-cost-rollup-for-{PROJECT_NAME}"
TABLE_INVENTORY = f"{PROJECT_NAME}-resource-inventory"
TABLE_EVENTS = f"{PROJECT_NAME}-usage-events"
TABLE_COST = f"{PROJECT_NAME}-cost-by-user"
DASHBOARD_NAME = f"dashboard-{PROJECT_NAME}-usage"
SNS_NOTEBOOK_DENY = f"sns-smus-notebook-deny-for-{PROJECT_NAME}"
ALARM_NOTEBOOK_DENY = f"alarm-smus-notebook-deny-for-{PROJECT_NAME}"
SG_ENDPOINT = f"vpce-for-{PROJECT_NAME}"
DEFAULT_PROJECT_PROFILE_NAME = "default-all-capabilities"
PROVISIONING_MANAGED_POLICIES = [
    "arn:aws:iam::aws:policy/service-role/SageMakerStudioProjectProvisioningRolePolicy",
]
MANAGE_ACCESS_MANAGED_POLICIES = [
    "arn:aws:iam::aws:policy/service-role/AmazonDataZoneGlueManageAccessRolePolicy",
]

BEDROCK_ENDPOINT_SERVICES = [
    f"com.amazonaws.{REGION}.bedrock-runtime",
    f"com.amazonaws.{REGION}.bedrock-agent",
    f"com.amazonaws.{REGION}.bedrock-agent-runtime",
]
EXTRA_ENDPOINT_SERVICES = [
    f"com.amazonaws.{REGION}.sagemaker.api",
    f"com.amazonaws.{REGION}.logs",
    f"com.amazonaws.{REGION}.monitoring",
    f"com.amazonaws.{REGION}.sts",
]

MANAGED_DOMAIN_EXEC = "arn:aws:iam::aws:policy/service-role/SageMakerStudioDomainExecutionRolePolicy"
# Fallback names used across accounts/docs
MANAGED_DOMAIN_EXEC_ALT = "arn:aws:iam::aws:policy/SageMakerStudioDomainExecutionRolePolicy"
MANAGED_DOMAIN_SVC = "arn:aws:iam::aws:policy/service-role/SageMakerStudioDomainServiceRolePolicy"
MANAGED_DOMAIN_SVC_ALT = "arn:aws:iam::aws:policy/SageMakerStudioDomainServiceRolePolicy"


def _clients(region: str = REGION):
    return {
        "sts": boto3.client("sts", region_name=region),
        "ec2": boto3.client("ec2", region_name=region),
        "iam": boto3.client("iam", region_name=region),
        "s3": boto3.client("s3", region_name=region),
        "datazone": boto3.client("datazone", region_name=region),
        "sso_admin": boto3.client("sso-admin", region_name=region),
        "identitystore": boto3.client("identitystore", region_name=region),
        "dynamodb": boto3.client("dynamodb", region_name=region),
        "lambda": boto3.client("lambda", region_name=region),
        "events": boto3.client("events", region_name=region),
        "logs": boto3.client("logs", region_name=region),
        "bedrock": boto3.client("bedrock", region_name=region),
        "sagemaker": boto3.client("sagemaker", region_name=region),
    }


def load_config() -> Dict[str, Any]:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "projectName": PROJECT_NAME,
        "region": REGION,
        "domain_name": DOMAIN_NAME,
        "domain_version": DOMAIN_VERSION,
        "resources": {},
    }


def save_config(cfg: Dict[str, Any]) -> None:
    cfg["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"  ✓ config.json updated: {CONFIG_PATH}")


def set_resource(cfg: Dict[str, Any], key: str, value: Any) -> None:
    cfg.setdefault("resources", {})[key] = value
    save_config(cfg)


def tag_spec(resource_type: str, name: str) -> List[Dict[str, Any]]:
    return [
        {
            "ResourceType": resource_type,
            "Tags": [
                {"Key": "Name", "Value": name},
                {"Key": "Project", "Value": PROJECT_NAME},
                {"Key": "ManagedBy", "Value": "smus-installer"},
            ],
        }
    ]


def ensure_role(
    iam,
    role_name: str,
    trust: Dict[str, Any],
    managed_policy_arns: Sequence[str] = (),
    inline_policies: Optional[Dict[str, Dict[str, Any]]] = None,
) -> str:
    try:
        role = iam.get_role(RoleName=role_name)["Role"]
        print(f"  · role exists: {role_name}")
        iam.update_assume_role_policy(
            RoleName=role_name, PolicyDocument=json.dumps(trust)
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        role = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description=f"SMUS role for {PROJECT_NAME}",
            Tags=[
                {"Key": "Project", "Value": PROJECT_NAME},
                {"Key": "ManagedBy", "Value": "smus-installer"},
            ],
        )["Role"]
        print(f"  ✓ created role: {role_name}")
        time.sleep(8)

    for arn in managed_policy_arns:
        try:
            iam.attach_role_policy(RoleName=role_name, PolicyArn=arn)
        except ClientError as e:
            print(f"  ! attach {arn}: {e.response['Error'].get('Message', e)}")

    for pname, doc in (inline_policies or {}).items():
        iam.put_role_policy(
            RoleName=role_name, PolicyName=pname, PolicyDocument=json.dumps(doc)
        )
    return role["Arn"]


def attach_first_existing_policy(iam, role_name: str, candidates: Sequence[str]) -> Optional[str]:
    for arn in candidates:
        try:
            iam.attach_role_policy(RoleName=role_name, PolicyArn=arn)
            print(f"  ✓ attached {arn}")
            return arn
        except ClientError:
            continue
    print(f"  ! no managed policy attached for {role_name} from {list(candidates)}")
    return None


def _find_vpc_by_name(ec2, name: str) -> Optional[str]:
    resp = ec2.describe_vpcs(
        Filters=[
            {"Name": "tag:Name", "Values": [name]},
            {"Name": "tag:Project", "Values": [PROJECT_NAME]},
        ]
    )
    vpcs = resp.get("Vpcs") or []
    return vpcs[0]["VpcId"] if vpcs else None


def _collect_vpc_net(ec2, vpc_id: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuild network ids from an existing VPC (resume after partial failure)."""
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    public_subnet_ids: List[str] = []
    private_subnet_ids: List[str] = []
    for s in subnets:
        name = next((t["Value"] for t in s.get("Tags") or [] if t["Key"] == "Name"), "")
        if "public" in name:
            public_subnet_ids.append(s["SubnetId"])
        else:
            private_subnet_ids.append(s["SubnetId"])
    sgs = ec2.describe_security_groups(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "group-name", "Values": [SG_ENDPOINT]},
        ]
    ).get("SecurityGroups") or []
    sg_id = sgs[0]["GroupId"] if sgs else cfg.get("resources", {}).get("endpoint_sg_id")
    nats = ec2.describe_nat_gateways(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "state", "Values": ["available", "pending"]},
        ]
    ).get("NatGateways") or []
    out = {
        "vpc_id": vpc_id,
        "public_subnet_ids": public_subnet_ids,
        "private_subnet_ids": private_subnet_ids,
        "endpoint_sg_id": sg_id,
        "nat_gateway_id": nats[0]["NatGatewayId"] if nats else cfg.get("resources", {}).get("nat_gateway_id"),
    }
    for k, v in out.items():
        if v:
            set_resource(cfg, k, v)
    return out


def _ensure_endpoint_sg(ec2, vpc_id: str, cfg: Dict[str, Any]) -> str:
    existing = ec2.describe_security_groups(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "group-name", "Values": [SG_ENDPOINT]},
        ]
    ).get("SecurityGroups") or []
    if existing:
        sg_id = existing[0]["GroupId"]
        print(f"  · endpoint SG exists: {sg_id}")
        set_resource(cfg, "endpoint_sg_id", sg_id)
        return sg_id
    sg_id = ec2.create_security_group(
        GroupName=SG_ENDPOINT,
        Description="VPC endpoints for SMUS / Bedrock PrivateLink",
        VpcId=vpc_id,
        TagSpecifications=tag_spec("security-group", SG_ENDPOINT),
    )["GroupId"]
    try:
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 443,
                    "ToPort": 443,
                    "IpRanges": [{"CidrIp": VPC_CIDR, "Description": "HTTPS from VPC"}],
                }
            ],
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
            raise
    set_resource(cfg, "endpoint_sg_id", sg_id)
    print(f"  ✓ endpoint SG: {sg_id}")
    return sg_id


def create_vpc(ec2, cfg: Dict[str, Any]) -> Dict[str, Any]:
    print("\n==> Creating VPC")
    existing = cfg.get("resources", {}).get("vpc_id") or _find_vpc_by_name(
        ec2, f"vpc-for-{PROJECT_NAME}"
    )
    if existing:
        print(f"  · reusing vpc: {existing}")
        net = _collect_vpc_net(ec2, existing, cfg)
        if not net.get("endpoint_sg_id"):
            net["endpoint_sg_id"] = _ensure_endpoint_sg(ec2, existing, cfg)
        if net.get("private_subnet_ids") and net.get("public_subnet_ids"):
            print(f"  ✓ VPC {existing} (resumed)")
            return net
        print("  ! existing VPC missing subnets; inspect manually before recreating")
        return net

    vpc = ec2.create_vpc(
        CidrBlock=VPC_CIDR,
        TagSpecifications=tag_spec("vpc", f"vpc-for-{PROJECT_NAME}"),
    )["Vpc"]
    vpc_id = vpc["VpcId"]
    set_resource(cfg, "vpc_id", vpc_id)
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})

    igw = ec2.create_internet_gateway(
        TagSpecifications=tag_spec("internet-gateway", f"igw-for-{PROJECT_NAME}")
    )["InternetGateway"]
    igw_id = igw["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
    set_resource(cfg, "igw_id", igw_id)

    azs = [a["ZoneName"] for a in ec2.describe_availability_zones()["AvailabilityZones"]]
    public_subnet_ids: List[str] = []
    private_subnet_ids: List[str] = []

    public_rt = ec2.create_route_table(
        VpcId=vpc_id,
        TagSpecifications=tag_spec("route-table", f"rtb-public-for-{PROJECT_NAME}"),
    )["RouteTable"]["RouteTableId"]
    ec2.create_route(RouteTableId=public_rt, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)

    for i, cidr in enumerate(PUBLIC_SUBNET_CIDRS):
        az = azs[i % len(azs)]
        subnet_id = ec2.create_subnet(
            VpcId=vpc_id,
            CidrBlock=cidr,
            AvailabilityZone=az,
            TagSpecifications=tag_spec("subnet", f"subnet-public-{i+1}-for-{PROJECT_NAME}"),
        )["Subnet"]["SubnetId"]
        ec2.associate_route_table(RouteTableId=public_rt, SubnetId=subnet_id)
        public_subnet_ids.append(subnet_id)
    set_resource(cfg, "public_subnet_ids", public_subnet_ids)

    eip = ec2.allocate_address(Domain="vpc")["AllocationId"]
    nat = ec2.create_nat_gateway(
        SubnetId=public_subnet_ids[0],
        AllocationId=eip,
        TagSpecifications=tag_spec("natgateway", f"nat-for-{PROJECT_NAME}"),
    )["NatGateway"]
    nat_id = nat["NatGatewayId"]
    set_resource(cfg, "nat_gateway_id", nat_id)
    set_resource(cfg, "eip_allocation_id", eip)
    print(f"  · waiting for NAT {nat_id}...")
    waiter = ec2.get_waiter("nat_gateway_available")
    waiter.wait(NatGatewayIds=[nat_id])

    private_rt = ec2.create_route_table(
        VpcId=vpc_id,
        TagSpecifications=tag_spec("route-table", f"rtb-private-for-{PROJECT_NAME}"),
    )["RouteTable"]["RouteTableId"]
    ec2.create_route(RouteTableId=private_rt, DestinationCidrBlock="0.0.0.0/0", NatGatewayId=nat_id)

    for i, cidr in enumerate(PRIVATE_SUBNET_CIDRS):
        az = azs[i % len(azs)]
        subnet_id = ec2.create_subnet(
            VpcId=vpc_id,
            CidrBlock=cidr,
            AvailabilityZone=az,
            TagSpecifications=tag_spec("subnet", f"subnet-private-{i+1}-for-{PROJECT_NAME}"),
        )["Subnet"]["SubnetId"]
        ec2.associate_route_table(RouteTableId=private_rt, SubnetId=subnet_id)
        private_subnet_ids.append(subnet_id)
    set_resource(cfg, "private_subnet_ids", private_subnet_ids)

    sg = _ensure_endpoint_sg(ec2, vpc_id, cfg)
    out = {
        "vpc_id": vpc_id,
        "igw_id": igw_id,
        "nat_gateway_id": nat_id,
        "eip_allocation_id": eip,
        "public_subnet_ids": public_subnet_ids,
        "private_subnet_ids": private_subnet_ids,
        "endpoint_sg_id": sg,
    }
    print(f"  ✓ VPC {vpc_id}")
    return out


def create_privatelink_endpoints(ec2, cfg: Dict[str, Any], net: Dict[str, Any]) -> List[str]:
    print("\n==> Creating Bedrock / SMUS PrivateLink endpoints")
    subnet_ids = net["private_subnet_ids"][:2] or net["private_subnet_ids"]
    sg_id = net["endpoint_sg_id"]
    created = list(cfg.get("resources", {}).get("vpc_endpoint_ids") or [])

    # S3 gateway
    try:
        existing = ec2.describe_vpc_endpoints(
            Filters=[
                {"Name": "vpc-id", "Values": [net["vpc_id"]]},
                {"Name": "service-name", "Values": [f"com.amazonaws.{REGION}.s3"]},
            ]
        )["VpcEndpoints"]
        if not existing:
            rt_ids = [
                rt["RouteTableId"]
                for rt in ec2.describe_route_tables(
                    Filters=[{"Name": "vpc-id", "Values": [net["vpc_id"]]}]
                )["RouteTables"]
                if any(
                    (a.get("SubnetId") in net["private_subnet_ids"])
                    for a in rt.get("Associations") or []
                )
            ]
            ep = ec2.create_vpc_endpoint(
                VpcId=net["vpc_id"],
                ServiceName=f"com.amazonaws.{REGION}.s3",
                VpcEndpointType="Gateway",
                RouteTableIds=rt_ids or None,
                TagSpecifications=tag_spec("vpc-endpoint", f"vpce-s3-for-{PROJECT_NAME}"),
            )["VpcEndpoint"]
            created.append(ep["VpcEndpointId"])
            print(f"  ✓ S3 gateway endpoint {ep['VpcEndpointId']}")
        else:
            created.append(existing[0]["VpcEndpointId"])
            print(f"  · S3 gateway exists {existing[0]['VpcEndpointId']}")
    except ClientError as e:
        print(f"  ! S3 endpoint: {e}")

    for svc in BEDROCK_ENDPOINT_SERVICES + EXTRA_ENDPOINT_SERVICES:
        try:
            found = ec2.describe_vpc_endpoints(
                Filters=[
                    {"Name": "vpc-id", "Values": [net["vpc_id"]]},
                    {"Name": "service-name", "Values": [svc]},
                    {"Name": "vpc-endpoint-state", "Values": ["available", "pending"]},
                ]
            )["VpcEndpoints"]
            if found:
                created.append(found[0]["VpcEndpointId"])
                print(f"  · exists {svc}")
                continue
            ep = ec2.create_vpc_endpoint(
                VpcId=net["vpc_id"],
                ServiceName=svc,
                VpcEndpointType="Interface",
                SubnetIds=subnet_ids,
                SecurityGroupIds=[sg_id],
                PrivateDnsEnabled=True,
                TagSpecifications=tag_spec(
                    "vpc-endpoint", f"vpce-{svc.split('.')[-1]}-for-{PROJECT_NAME}"
                ),
            )["VpcEndpoint"]
            created.append(ep["VpcEndpointId"])
            print(f"  ✓ {svc} → {ep['VpcEndpointId']}")
        except ClientError as e:
            print(f"  ! {svc}: {e.response['Error'].get('Message', e)}")

    # unique preserve order
    uniq: List[str] = []
    for x in created:
        if x not in uniq:
            uniq.append(x)
    set_resource(cfg, "vpc_endpoint_ids", uniq)
    return uniq


def create_project_bucket(s3, account_id: str, cfg: Dict[str, Any]) -> str:
    bucket = f"storage-for-{PROJECT_NAME}-{account_id}-{REGION}"
    print(f"\n==> Ensuring S3 bucket {bucket}")
    try:
        s3.head_bucket(Bucket=bucket)
        print("  · bucket exists")
    except ClientError:
        kwargs: Dict[str, Any] = {"Bucket": bucket}
        if REGION != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": REGION}
        s3.create_bucket(**kwargs)
        s3.put_bucket_tagging(
            Bucket=bucket,
            Tagging={
                "TagSet": [
                    {"Key": "Project", "Value": PROJECT_NAME},
                    {"Key": "ManagedBy", "Value": "smus-installer"},
                ]
            },
        )
        print("  ✓ created")
    set_resource(cfg, "s3_bucket", bucket)
    return bucket


def create_domain_roles(iam, account_id: str, cfg: Dict[str, Any]) -> Dict[str, str]:
    print("\n==> Creating IAM roles")
    trust_dz = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "datazone.amazonaws.com"},
                "Action": ["sts:AssumeRole", "sts:TagSession", "sts:SetContext"],
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account_id},
                },
            }
        ],
    }
    trust_dz_simple = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "datazone.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": account_id}},
            }
        ],
    }
    trust_lambda = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }

    exec_arn = ensure_role(iam, ROLE_DOMAIN_EXEC, trust_dz)
    attach_first_existing_policy(
        iam, ROLE_DOMAIN_EXEC, [MANAGED_DOMAIN_EXEC, MANAGED_DOMAIN_EXEC_ALT]
    )
    svc_arn = ensure_role(iam, ROLE_DOMAIN_SVC, trust_dz_simple)
    attach_first_existing_policy(
        iam, ROLE_DOMAIN_SVC, [MANAGED_DOMAIN_SVC, MANAGED_DOMAIN_SVC_ALT]
    )

    # Provisioning / manage-access / model roles: service principals commonly used by SMUS
    for role_name, svc in [
        (ROLE_PROVISIONING, "datazone.amazonaws.com"),
        (ROLE_MANAGE_ACCESS, "datazone.amazonaws.com"),
        (ROLE_MODEL_PROV, "datazone.amazonaws.com"),
        (ROLE_MODEL_CONS, "datazone.amazonaws.com"),
    ]:
        arn = ensure_role(
            iam,
            role_name,
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": svc},
                        "Action": ["sts:AssumeRole", "sts:TagSession"],
                        "Condition": {"StringEquals": {"aws:SourceAccount": account_id}},
                    }
                ],
            },
            inline_policies={
                "smus-basic": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "s3:*",
                                "iam:PassRole",
                                "cloudformation:*",
                                "sagemaker:*",
                                "bedrock:*",
                                "glue:*",
                                "lakeformation:*",
                                "redshift:*",
                                "athena:*",
                                "aoss:*",
                            ],
                            "Resource": "*",
                        }
                    ],
                }
            },
        )
        set_resource(cfg, role_name.replace("-", "_"), arn)

    guardrails = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "DenyUnauthorizedInstanceTypes",
                "Effect": "Deny",
                "Action": [
                    "sagemaker:CreateApp",
                    "sagemaker:CreateSpace",
                    "sagemaker:UpdateSpace",
                ],
                "Resource": "*",
                "Condition": {
                    "ForAnyValue:StringNotEquals": {
                        "sagemaker:InstanceTypes": ALLOWED_INSTANCE_TYPES
                    }
                },
            },
            {
                "Sid": "SageMakerDataAgentDeny",
                "Effect": "Deny",
                "Action": [
                    "datazone:SendMessage",
                    "datazone:GenerateCode",
                    "datazone:StartConversation",
                    "datazone:GetConversation",
                    "datazone:ListConversations",
                ],
                "Resource": "*",
            },
        ],
    }
    try:
        iam.create_policy(
            PolicyName=POLICY_GUARDRAILS,
            PolicyDocument=json.dumps(guardrails),
            Description="SMUS instance-type + Data Agent guardrails",
        )
        print(f"  ✓ policy {POLICY_GUARDRAILS}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        print(f"  · policy exists {POLICY_GUARDRAILS}")

    policy_arn = f"arn:aws:iam::{account_id}:policy/{POLICY_GUARDRAILS}"
    set_resource(cfg, "guardrails_policy_arn", policy_arn)
    set_resource(cfg, "domain_execution_role_arn", exec_arn)
    set_resource(cfg, "domain_service_role_arn", svc_arn)

    lambda_arn = ensure_role(
        iam,
        ROLE_LAMBDA_USAGE,
        trust_lambda,
        managed_policy_arns=[
            "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
        ],
        inline_policies={
            "usage-monitor": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "sagemaker:ListSpaces",
                            "sagemaker:DescribeSpace",
                            "sagemaker:ListApps",
                            "sagemaker:DescribeApp",
                            "sagemaker:ListUserProfiles",
                            "sagemaker:DescribeUserProfile",
                            "sagemaker:ListDomains",
                            "sagemaker:DescribeDomain",
                            "sagemaker:UpdateDomain",
                            "iam:ListRoles",
                            "iam:GetRole",
                            "iam:ListAttachedRolePolicies",
                            "iam:AttachRolePolicy",
                            "iam:PutRolePolicy",
                            "iam:UpdateAssumeRolePolicy",
                            "datazone:StopCompute",
                            "datazone:GetCompute",
                            "datazone:StopNotebookCompute",
                            "datazone:GetNotebookCompute",
                            "datazone:GetEnvironmentCredentials",
                            "ec2:DescribeNetworkInterfaces",
                            "ec2:ModifyNetworkInterfaceAttribute",
                            "ec2:DescribeSecurityGroups",
                            "ec2:CreateSecurityGroup",
                            "ec2:RevokeSecurityGroupEgress",
                            "ec2:CreateTags",
                            "dynamodb:PutItem",
                            "dynamodb:UpdateItem",
                            "dynamodb:GetItem",
                            "dynamodb:Query",
                            "dynamodb:Scan",
                            "cloudwatch:PutMetricData",
                            "ce:GetCostAndUsage",
                            "tag:GetResources",
                        ],
                        "Resource": "*",
                    },
                    {
                        "Sid": "AssumeProjectUserRolesForNotebookStop",
                        "Effect": "Allow",
                        "Action": [
                            "sts:AssumeRole",
                            "sts:TagSession",
                            "sts:SetSourceIdentity",
                        ],
                        "Resource": f"arn:aws:iam::{account_id}:role/{PROJECT_USER_ROLE_PREFIX}*",
                    },
                ],
            }
        },
    )
    set_resource(cfg, "lambda_usage_role_arn", lambda_arn)
    return {
        "domain_execution_role_arn": exec_arn,
        "domain_service_role_arn": svc_arn,
        "lambda_usage_role_arn": lambda_arn,
        "guardrails_policy_arn": policy_arn,
    }


def resolve_idc_instance(sso_admin, cfg: Dict[str, Any]) -> Optional[str]:
    print("\n==> Resolving IAM Identity Center instance")
    try:
        instances = sso_admin.list_instances().get("Instances") or []
    except ClientError as e:
        print(f"  ! list_instances failed: {e}")
        return cfg.get("auth", {}).get("idc_instance_arn") or None
    if not instances:
        print("  ! No IdC instance in this account/region.")
        print("    Enable IAM Identity Center (organization or account instance), then re-run.")
        return None
    arn = instances[0]["InstanceArn"]
    print(f"  ✓ IdC instance: {arn}")
    cfg.setdefault("auth", {})["idc_instance_arn"] = arn
    cfg["auth"]["identity_store_id"] = instances[0].get("IdentityStoreId", "")
    save_config(cfg)
    return arn


def create_or_get_domain(
    datazone,
    cfg: Dict[str, Any],
    exec_role_arn: str,
    svc_role_arn: str,
    idc_arn: Optional[str],
) -> Dict[str, Any]:
    print("\n==> Creating SageMaker Unified Studio domain (DataZone V2)")
    existing_id = cfg.get("resources", {}).get("domain_id")
    if existing_id:
        try:
            d = datazone.get_domain(identifier=existing_id)
            print(f"  · domain exists: {existing_id}")
            return d
        except ClientError:
            pass

    # Find by name
    try:
        for d in (datazone.list_domains().get("items") or []):
            if d.get("name") == DOMAIN_NAME:
                full = datazone.get_domain(identifier=d["id"])
                set_resource(cfg, "domain_id", full["id"])
                set_resource(cfg, "domain_arn", full.get("arn"))
                set_resource(cfg, "portal_url", full.get("portalUrl"))
                print(f"  · found domain by name: {full['id']}")
                return full
    except ClientError as e:
        print(f"  ! list_domains: {e}")

    sso: Dict[str, Any] = {"type": "DISABLED"}
    if idc_arn:
        sso = {
            "type": "IAM_IDC",
            "userAssignment": cfg.get("auth", {}).get("user_assignment", "MANUAL"),
            "idcInstanceArn": idc_arn,
        }

    params: Dict[str, Any] = {
        "name": DOMAIN_NAME,
        "description": "SageMaker Unified Studio domain (smus-kr)",
        "domainExecutionRole": exec_role_arn,
        "serviceRole": svc_role_arn,
        "domainVersion": DOMAIN_VERSION,
        "singleSignOn": sso,
        "tags": {
            "Project": PROJECT_NAME,
            "ManagedBy": "smus-installer",
        },
    }
    resp = datazone.create_domain(**params)
    domain_id = resp["id"]
    print(f"  ✓ creating domain {domain_id} (status={resp.get('status')})")
    set_resource(cfg, "domain_id", domain_id)
    set_resource(cfg, "domain_arn", resp.get("arn"))
    set_resource(cfg, "portal_url", resp.get("portalUrl"))

    for _ in range(60):
        d = datazone.get_domain(identifier=domain_id)
        status = d.get("status")
        print(f"  · status={status}")
        if status == "AVAILABLE":
            set_resource(cfg, "portal_url", d.get("portalUrl"))
            set_resource(cfg, "root_domain_unit_id", d.get("rootDomainUnitId"))
            return d
        if status in ("CREATION_FAILED", "DELETED", "DELETION_FAILED"):
            raise RuntimeError(f"Domain creation failed: {status}")
        time.sleep(10)
    raise TimeoutError("Domain did not become AVAILABLE in time")


def note_idc_admin(cfg: Dict[str, Any], email: Optional[str]) -> None:
    print("\n==> IdC admin / Data Agent UI toggle")
    if email:
        cfg.setdefault("auth", {})["idc_admin_email"] = email
        save_config(cfg)
        print(f"  · admin email recorded: {email}")
    print("  · After domain is AVAILABLE:")
    print("    1) Console → SageMaker → Domains → smus-kr → Configure SSO user access")
    print("    2) Assign IdC users/groups (MANUAL assignment)")
    print("    3) Domain configuration → disable SageMaker Data Agent (hides UI)")
    print("    4) Amazon Bedrock models tab → refresh & enable models")
    print("    5) Guardrails attach + Studio UI hide run automatically (see attach_guardrails_*).")


def _ensure_project_role_trusts_lambda(iam, role_name: str, lambda_role_arn: str) -> None:
    """Allow usage-monitor Lambda to AssumeRole into project user roles (for StopCompute)."""
    if not lambda_role_arn:
        return
    try:
        role = iam.get_role(RoleName=role_name)["Role"]
        doc = role["AssumeRolePolicyDocument"]
        if isinstance(doc, str):
            doc = json.loads(doc)
        statements = list(doc.get("Statement") or [])
        desired_actions = [
            "sts:AssumeRole",
            "sts:TagSession",
            "sts:SetContext",
            "sts:SetSourceIdentity",
        ]
        updated = False
        found = False
        for stmt in statements:
            principal = stmt.get("Principal") or {}
            aws_p = principal.get("AWS")
            arns = aws_p if isinstance(aws_p, list) else ([aws_p] if aws_p else [])
            if lambda_role_arn not in arns:
                continue
            found = True
            actions = stmt.get("Action")
            action_list = actions if isinstance(actions, list) else [actions]
            if set(desired_actions).issubset(set(action_list)):
                return
            stmt["Action"] = desired_actions
            updated = True
            break
        if not found:
            statements.append(
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": lambda_role_arn},
                    "Action": desired_actions,
                }
            )
            updated = True
        if not updated:
            return
        doc["Statement"] = statements
        iam.update_assume_role_policy(
            RoleName=role_name, PolicyDocument=json.dumps(doc)
        )
        print(f"  ✓ trust lambda assume → {role_name}")
    except ClientError as e:
        print(f"  ! trust update {role_name}: {e.response['Error'].get('Message', e)}")


def attach_guardrails_to_project_roles(iam, cfg: Dict[str, Any]) -> List[str]:
    """Attach instance-type Deny policy to all DataZone project user roles."""
    print("\n==> Attaching guardrails to project user roles")
    policy_arn = (cfg.get("resources") or {}).get("guardrails_policy_arn")
    account_id = (cfg.get("resources") or {}).get("account_id")
    if not account_id:
        account_id = boto3.client("sts").get_caller_identity()["Account"]
    if not policy_arn:
        policy_arn = f"arn:aws:iam::{account_id}:policy/{POLICY_GUARDRAILS}"
        set_resource(cfg, "guardrails_policy_arn", policy_arn)

    lambda_role_arn = (cfg.get("resources") or {}).get("lambda_usage_role_arn") or (
        f"arn:aws:iam::{account_id}:role/{ROLE_LAMBDA_USAGE}"
    )

    attached: List[str] = []
    paginator = iam.get_paginator("list_roles")
    for page in paginator.paginate():
        for role in page.get("Roles") or []:
            name = role["RoleName"]
            if not name.startswith(PROJECT_USER_ROLE_PREFIX):
                continue
            try:
                existing = {
                    p["PolicyArn"]
                    for p in iam.list_attached_role_policies(RoleName=name).get(
                        "AttachedPolicies", []
                    )
                }
                if policy_arn in existing:
                    print(f"  · already attached: {name}")
                else:
                    iam.attach_role_policy(RoleName=name, PolicyArn=policy_arn)
                    print(f"  ✓ attached guardrails → {name}")
                # Portal Stop uses *Notebook*; REST StopCompute needs explicit allow for post-hoc guard
                iam.put_role_policy(
                    RoleName=name,
                    PolicyName="smus-notebook-compute-stop",
                    PolicyDocument=json.dumps(
                        {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Sid": "AllowStopNotebookComputeRest",
                                    "Effect": "Allow",
                                    "Action": [
                                        "datazone:StopCompute",
                                        "datazone:GetCompute",
                                        "datazone:StopNotebookCompute",
                                        "datazone:GetNotebookCompute",
                                        "datazone:GetEnvironmentCredentials",
                                    ],
                                    "Resource": "*",
                                }
                            ],
                        }
                    ),
                )
                _ensure_project_role_trusts_lambda(iam, name, lambda_role_arn)
                attached.append(name)
            except ClientError as e:
                print(f"  ! {name}: {e.response['Error'].get('Message', e)}")

    if not attached:
        print("  · no datazone_usr_role_* found yet (created when a project is provisioned)")
    set_resource(cfg, "guardrails_attached_roles", attached)
    return attached


def apply_studio_ui_instance_hide(sagemaker, cfg: Dict[str, Any]) -> None:
    """Hide non-allowed instance types in SageMaker Studio UI (domain DefaultUserSettings)."""
    print("\n==> Hiding non-allowed instance types in Studio UI")
    domains = sagemaker.list_domains().get("Domains") or []
    smus_domains = [
        d
        for d in domains
        if (d.get("DomainName") or "").startswith("SageMakerUnifiedStudio-")
        or PROJECT_NAME in (d.get("DomainName") or "")
    ]
    if not smus_domains:
        smus_domains = domains
    if not smus_domains:
        print("  · no SageMaker domain found yet")
        return

    hidden = HIDDEN_INSTANCE_TYPES
    updated: List[str] = []
    for d in smus_domains:
        domain_id = d["DomainId"]
        try:
            sagemaker.update_domain(
                DomainId=domain_id,
                DefaultUserSettings={
                    "StudioWebPortalSettings": {
                        "HiddenInstanceTypes": hidden,
                    }
                },
            )
            print(
                f"  ✓ domain {domain_id}: hidden {len(hidden)} types "
                f"(allowed remain: {', '.join(ALLOWED_INSTANCE_TYPES)})"
            )
            updated.append(domain_id)
        except ClientError as e:
            print(f"  ! UpdateDomain {domain_id}: {e.response['Error'].get('Message', e)}")
            print("    (Unified Studio Space UI may still show types; IAM Deny remains the hard gate)")

    set_resource(cfg, "studio_ui_hidden_instance_domain_ids", updated)
    set_resource(cfg, "studio_ui_hidden_instance_types_count", len(hidden))


def _list_managed_blueprints(datazone, domain_id: str) -> Dict[str, str]:
    blueprints: Dict[str, str] = {}
    next_token: Optional[str] = None
    while True:
        kwargs: Dict[str, Any] = {"domainIdentifier": domain_id, "managed": True}
        if next_token:
            kwargs["nextToken"] = next_token
        resp = datazone.list_environment_blueprints(**kwargs)
        for item in resp.get("items") or []:
            blueprints[item["name"]] = item["id"]
        next_token = resp.get("nextToken")
        if not next_token:
            break
    return blueprints


def _attach_role_policies(iam, role_name: str, policy_arns: Sequence[str]) -> None:
    for arn in policy_arns:
        try:
            iam.attach_role_policy(RoleName=role_name, PolicyArn=arn)
            print(f"  ✓ attached {arn} → {role_name}")
        except ClientError as e:
            code = e.response["Error"].get("Code")
            if code in {"NoSuchEntity", "NoSuchEntityException"}:
                print(f"  · skip missing policy {arn}")
            else:
                print(f"  ! attach {arn}: {e.response['Error'].get('Message', e)}")


def _add_policy_grant(
    datazone,
    *,
    domain_id: str,
    entity_type: str,
    entity_id: str,
    policy_type: str,
    principal: Dict[str, Any],
    detail: Dict[str, Any],
) -> None:
    try:
        datazone.add_policy_grant(
            domainIdentifier=domain_id,
            entityType=entity_type,
            entityIdentifier=entity_id,
            policyType=policy_type,
            principal=principal,
            detail=detail,
        )
        print(f"  ✓ grant {policy_type} on {entity_type}:{entity_id}")
    except ClientError as e:
        msg = e.response["Error"].get("Message", str(e))
        # Idempotent-ish: already exists / conflict
        if any(x in msg.lower() for x in ("already", "exist", "conflict", "duplicate")):
            print(f"  · grant already present: {policy_type} / {entity_id}")
        else:
            print(f"  ! grant {policy_type} / {entity_id}: {msg}")


def setup_blueprints_and_default_profile(
    datazone,
    iam,
    cfg: Dict[str, Any],
) -> None:
    """Enable Tooling/DataLake, create default project profile, add policy grants."""
    print("\n==> Enabling blueprints + default project profile")
    res = cfg.get("resources") or {}
    domain_id = res.get("domain_id")
    account_id = res.get("account_id")
    root_du = res.get("root_domain_unit_id")
    vpc_id = res.get("vpc_id")
    private_subnets = res.get("private_subnet_ids") or []
    bucket = res.get("s3_bucket")
    prov_arn: Optional[str] = None
    manage_arn: Optional[str] = None
    try:
        prov_arn = iam.get_role(RoleName=ROLE_PROVISIONING)["Role"]["Arn"]
        manage_arn = iam.get_role(RoleName=ROLE_MANAGE_ACCESS)["Role"]["Arn"]
    except ClientError as e:
        print(f"  ! lookup provisioning/manage-access roles: {e}")
        for key, val in res.items():
            if not isinstance(val, str):
                continue
            if key.startswith("role_provisioning_for_"):
                prov_arn = val
            if key.startswith("role_manage_access_for_"):
                manage_arn = val

    if not domain_id or not account_id or not vpc_id or not private_subnets or not bucket:
        print("  ! missing domain/vpc/subnet/bucket; skip blueprint/profile setup")
        return
    if not root_du:
        try:
            d = datazone.get_domain(identifier=domain_id)
            root_du = d.get("rootDomainUnitId")
            if root_du:
                set_resource(cfg, "root_domain_unit_id", root_du)
        except ClientError as e:
            print(f"  ! get_domain for root unit: {e}")
            return

    _attach_role_policies(iam, ROLE_PROVISIONING, PROVISIONING_MANAGED_POLICIES)
    _attach_role_policies(iam, ROLE_MANAGE_ACCESS, MANAGE_ACCESS_MANAGED_POLICIES)

    blueprints = _list_managed_blueprints(datazone, domain_id)
    tooling_id = blueprints.get("Tooling")
    datalake_id = blueprints.get("DataLake")
    if not tooling_id:
        print("  ! Tooling managed blueprint not found; skip")
        return

    if not prov_arn or not manage_arn:
        print("  ! provisioning/manage-access role ARN missing; skip")
        return

    s3_location = f"s3://{bucket}/"
    subnets = ",".join(private_subnets)
    regional = {
        REGION: {
            "S3Location": s3_location,
            "Subnets": subnets,
            "VpcId": vpc_id,
        }
    }

    print(f"  · enabling Tooling ({tooling_id})")
    datazone.put_environment_blueprint_configuration(
        domainIdentifier=domain_id,
        environmentBlueprintIdentifier=tooling_id,
        enabledRegions=[REGION],
        provisioningRoleArn=prov_arn,
        manageAccessRoleArn=manage_arn,
        regionalParameters=regional,
    )
    set_resource(cfg, "tooling_blueprint_id", tooling_id)

    if datalake_id:
        print(f"  · enabling DataLake ({datalake_id})")
        datazone.put_environment_blueprint_configuration(
            domainIdentifier=domain_id,
            environmentBlueprintIdentifier=datalake_id,
            enabledRegions=[REGION],
            provisioningRoleArn=prov_arn,
            manageAccessRoleArn=manage_arn,
            regionalParameters={REGION: {"S3Location": s3_location}},
        )
        set_resource(cfg, "datalake_blueprint_id", datalake_id)

    env_cfgs: List[Dict[str, Any]] = [
        {
            "name": "Tooling",
            "environmentBlueprintId": tooling_id,
            "deploymentMode": "ON_CREATE",
            "deploymentOrder": 0,
            "awsAccount": {"awsAccountId": account_id},
            "awsRegion": {"regionName": REGION},
            "configurationParameters": {
                "parameterOverrides": [
                    {
                        "name": "ec2InstanceTypes",
                        "value": ",".join(ALLOWED_INSTANCE_TYPES),
                        "isEditable": False,
                    },
                    {
                        "name": "sagemakerDomainNetworkType",
                        "value": "VpcOnly",
                        "isEditable": False,
                    },
                    {
                        "name": "enableAmazonBedrockPermissions",
                        "value": "true",
                        "isEditable": False,
                    },
                    {
                        "name": "enableSageMakerMLWorkloadsPermissions",
                        "value": "true",
                        "isEditable": False,
                    },
                    {
                        "name": "enableGlueWorkloadsPermissions",
                        "value": "true",
                        "isEditable": False,
                    },
                ]
            },
        }
    ]
    if datalake_id:
        env_cfgs.append(
            {
                "name": "DataLake",
                "environmentBlueprintId": datalake_id,
                "deploymentMode": "ON_CREATE",
                "deploymentOrder": 1,
                "awsAccount": {"awsAccountId": account_id},
                "awsRegion": {"regionName": REGION},
            }
        )

    existing = datazone.list_project_profiles(domainIdentifier=domain_id).get("items") or []
    profile = next(
        (p for p in existing if p.get("name") == DEFAULT_PROJECT_PROFILE_NAME), None
    )
    if profile:
        print(f"  · updating profile {profile['id']}")
        out = datazone.update_project_profile(
            domainIdentifier=domain_id,
            identifier=profile["id"],
            name=DEFAULT_PROJECT_PROFILE_NAME,
            description="Default SMUS project profile (Tooling + DataLake)",
            status="ENABLED",
            environmentConfigurations=env_cfgs,
        )
        profile_id = out.get("id") or profile["id"]
    else:
        print(f"  · creating profile {DEFAULT_PROJECT_PROFILE_NAME}")
        out = datazone.create_project_profile(
            domainIdentifier=domain_id,
            name=DEFAULT_PROJECT_PROFILE_NAME,
            description="Default SMUS project profile (Tooling + DataLake) for smus-kr",
            status="ENABLED",
            environmentConfigurations=env_cfgs,
        )
        profile_id = out["id"]

    set_resource(cfg, "default_project_profile_id", profile_id)
    set_resource(cfg, "default_project_profile_name", DEFAULT_PROJECT_PROFILE_NAME)
    print(f"  ✓ project profile ENABLED: {profile_id}")

    # Domain-unit grants so users can create projects from the profile
    _add_policy_grant(
        datazone,
        domain_id=domain_id,
        entity_type="DOMAIN_UNIT",
        entity_id=root_du,
        policy_type="CREATE_PROJECT",
        principal={"user": {"allUsersGrantFilter": {}}},
        detail={"createProject": {"includeChildDomainUnits": True}},
    )
    _add_policy_grant(
        datazone,
        domain_id=domain_id,
        entity_type="DOMAIN_UNIT",
        entity_id=root_du,
        policy_type="CREATE_PROJECT_FROM_PROJECT_PROFILE",
        principal={"user": {"allUsersGrantFilter": {}}},
        detail={
            "createProjectFromProjectProfile": {
                "includeChildDomainUnits": True,
                "projectProfiles": [profile_id],
            }
        },
    )

    # Blueprint env create grants (entity id = ACCOUNT:BLUEPRINT_ID; principal = project filter)
    blueprint_grants: Dict[str, str] = {}
    for label, bp_id in (("tooling", tooling_id), ("datalake", datalake_id)):
        if not bp_id:
            continue
        entity_id = f"{account_id}:{bp_id}"
        blueprint_grants[label] = entity_id
        for designation in ("OWNER", "CONTRIBUTOR"):
            _add_policy_grant(
                datazone,
                domain_id=domain_id,
                entity_type="ENVIRONMENT_BLUEPRINT_CONFIGURATION",
                entity_id=entity_id,
                policy_type="CREATE_ENVIRONMENT_FROM_BLUEPRINT",
                principal={
                    "project": {
                        "projectDesignation": designation,
                        "projectGrantFilter": {
                            "domainUnitFilter": {
                                "domainUnit": root_du,
                                "includeChildDomainUnits": True,
                            }
                        },
                    }
                },
                detail={"createEnvironmentFromBlueprint": {}},
            )

    set_resource(
        cfg,
        "blueprint_env_grants",
        {
            **blueprint_grants,
            "note": (
                "CREATE_ENVIRONMENT_FROM_BLUEPRINT granted to project "
                "OWNER/CONTRIBUTOR via domain unit filter"
            ),
        },
    )
    print("  ✓ blueprints + default profile + grants ready")


def ensure_dynamodb_tables(dynamodb, cfg: Dict[str, Any]) -> None:
    print("\n==> DynamoDB tables for monitoring")

    def _create(name: str):
        try:
            dynamodb.describe_table(TableName=name)
            print(f"  · {name} exists")
            return
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
        dynamodb.create_table(
            TableName=name,
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
            Tags=[
                {"Key": "Project", "Value": PROJECT_NAME},
                {"Key": "ManagedBy", "Value": "smus-installer"},
            ],
        )
        print(f"  ✓ creating {name}")
        waiter = dynamodb.get_waiter("table_exists")
        waiter.wait(TableName=name)

    _create(TABLE_INVENTORY)
    _create(TABLE_EVENTS)
    _create(TABLE_COST)
    set_resource(cfg, "inventory_table", TABLE_INVENTORY)
    set_resource(cfg, "events_table", TABLE_EVENTS)
    set_resource(cfg, "cost_table", TABLE_COST)


def _zip_lambda() -> bytes:
    buf = BytesIO()
    src = os.path.join(ROOT, "lambda_usage_monitor", "lambda_function.py")
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(src, arcname="lambda_function.py")
    return buf.getvalue()


def deploy_monitoring_lambdas(lam, events, cfg: Dict[str, Any], role_arn: str) -> None:
    print("\n==> Deploying usage / cost Lambdas + EventBridge")
    zipped = _zip_lambda()
    guardrails_arn = (cfg.get("resources") or {}).get("guardrails_policy_arn") or ""
    env = {
        "Variables": {
            "PROJECT_NAME": PROJECT_NAME,
            "REGION": REGION,
            "INVENTORY_TABLE": TABLE_INVENTORY,
            "EVENTS_TABLE": TABLE_EVENTS,
            "COST_TABLE": TABLE_COST,
            "METRIC_NAMESPACE": f"SMUS/{PROJECT_NAME}",
            "GUARDRAILS_POLICY_ARN": guardrails_arn,
            "ALLOWED_NOTEBOOK_INSTANCE_TYPES": ",".join(ALLOWED_NOTEBOOK_INSTANCE_TYPES),
        }
    }

    def _wait_lambda_ready(fn_name: str, timeout_s: int = 120) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            conf = lam.get_function(FunctionName=fn_name)["Configuration"]
            status = conf.get("LastUpdateStatus") or conf.get("State")
            if status in (None, "Successful", "Active"):
                # LastUpdateStatus Successful / State Active
                if conf.get("State") in (None, "Active") and conf.get(
                    "LastUpdateStatus", "Successful"
                ) in ("Successful", None):
                    return
            if status in ("Failed",):
                raise RuntimeError(
                    f"Lambda {fn_name} update failed: {conf.get('LastUpdateStatusReason')}"
                )
            time.sleep(3)
        raise TimeoutError(f"Lambda {fn_name} not ready after {timeout_s}s")

    def _upsert(fn_name: str) -> str:
        try:
            lam.get_function(FunctionName=fn_name)
            _wait_lambda_ready(fn_name)
            lam.update_function_code(FunctionName=fn_name, ZipFile=zipped)
            _wait_lambda_ready(fn_name)
            lam.update_function_configuration(
                FunctionName=fn_name,
                Role=role_arn,
                Timeout=120,
                MemorySize=256,
                Environment=env,
            )
            _wait_lambda_ready(fn_name)
            print(f"  · updated {fn_name}")
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
            lam.create_function(
                FunctionName=fn_name,
                Runtime="python3.12",
                Role=role_arn,
                Handler="lambda_function.lambda_handler",
                Code={"ZipFile": zipped},
                Timeout=120,
                MemorySize=256,
                Environment=env,
                Tags={"Project": PROJECT_NAME, "ManagedBy": "smus-installer"},
            )
            print(f"  ✓ created {fn_name}")
        return lam.get_function(FunctionName=fn_name)["Configuration"]["FunctionArn"]

    usage_arn = _upsert(LAMBDA_USAGE)
    # same code, invoked with mode=cost
    cost_arn = _upsert(LAMBDA_COST)
    set_resource(cfg, "lambda_usage_arn", usage_arn)
    set_resource(cfg, "lambda_cost_arn", cost_arn)

    # Schedule reconcile every 15 minutes
    rule_reconcile = f"rule-smus-reconcile-for-{PROJECT_NAME}"
    events.put_rule(
        Name=rule_reconcile,
        ScheduleExpression="rate(15 minutes)",
        State="ENABLED",
        Description="SMUS space inventory reconcile",
    )
    events.put_targets(
        Rule=rule_reconcile,
        Targets=[
            {
                "Id": "usageMonitor",
                "Arn": usage_arn,
                "Input": json.dumps({"mode": "reconcile"}),
            }
        ],
    )
    try:
        lam.add_permission(
            FunctionName=LAMBDA_USAGE,
            StatementId="AllowEventBridgeReconcile",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=f"arn:aws:events:{REGION}:{cfg['resources'].get('account_id')}:rule/{rule_reconcile}",
        )
    except ClientError:
        pass

    # Daily cost rollup 06:00 UTC
    rule_cost = f"rule-smus-cost-for-{PROJECT_NAME}"
    events.put_rule(
        Name=rule_cost,
        ScheduleExpression="cron(0 6 * * ? *)",
        State="ENABLED",
        Description="SMUS daily cost rollup",
    )
    events.put_targets(
        Rule=rule_cost,
        Targets=[
            {
                "Id": "costRollup",
                "Arn": cost_arn,
                "Input": json.dumps({"mode": "cost", "days": 7}),
            }
        ],
    )
    try:
        lam.add_permission(
            FunctionName=LAMBDA_COST,
            StatementId="AllowEventBridgeCost",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=f"arn:aws:events:{REGION}:{cfg['resources'].get('account_id')}:rule/{rule_cost}",
        )
    except ClientError:
        pass

    # SageMaker API events (management events via CloudTrail default bus if available)
    rule_api = f"rule-smus-api-for-{PROJECT_NAME}"
    events.put_rule(
        Name=rule_api,
        EventPattern=json.dumps(
            {
                "source": ["aws.sagemaker"],
                "detail-type": ["AWS API Call via CloudTrail"],
                "detail": {
                    "eventSource": ["sagemaker.amazonaws.com"],
                    "eventName": [
                        "CreateSpace",
                        "UpdateSpace",
                        "DeleteSpace",
                        "CreateApp",
                        "DeleteApp",
                        "CreateUserProfile",
                        "DeleteUserProfile",
                    ],
                },
            }
        ),
        State="ENABLED",
        Description="SMUS Space/App API audit → usage monitor",
    )
    events.put_targets(
        Rule=rule_api,
        Targets=[{"Id": "usageEvents", "Arn": usage_arn}],
    )
    try:
        lam.add_permission(
            FunctionName=LAMBDA_USAGE,
            StatementId="AllowEventBridgeApi",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=f"arn:aws:events:{REGION}:{cfg['resources'].get('account_id')}:rule/{rule_api}",
        )
    except ClientError:
        pass

    # Notebooks sc.* post-hoc guard (CloudTrail StartNotebookCompute → StopCompute)
    rule_notebook = f"rule-smus-notebook-guard-for-{PROJECT_NAME}"
    events.put_rule(
        Name=rule_notebook,
        EventPattern=json.dumps(
            {
                "source": ["aws.datazone"],
                "detail-type": ["AWS API Call via CloudTrail"],
                "detail": {
                    "eventSource": ["datazone.amazonaws.com"],
                    "eventName": ["StartNotebookCompute"],
                },
            }
        ),
        State="ENABLED",
        Description="SMUS Notebooks StartNotebookCompute → post-hoc instance guard",
    )
    events.put_targets(
        Rule=rule_notebook,
        Targets=[{"Id": "notebookGuard", "Arn": usage_arn}],
    )
    try:
        lam.add_permission(
            FunctionName=LAMBDA_USAGE,
            StatementId="AllowEventBridgeNotebookGuard",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=(
                f"arn:aws:events:{REGION}:{cfg['resources'].get('account_id')}"
                f":rule/{rule_notebook}"
            ),
        )
    except ClientError:
        pass

    set_resource(
        cfg,
        "eventbridge_rules",
        [rule_reconcile, rule_cost, rule_api, rule_notebook],
    )
    set_resource(
        cfg,
        "allowed_notebook_instance_types",
        ALLOWED_NOTEBOOK_INSTANCE_TYPES,
    )
    print("  ✓ monitoring schedules + API + notebook-guard rules configured")
    print(
        "  · Notebooks allowlist (post-hoc StopCompute): "
        + ", ".join(ALLOWED_NOTEBOOK_INSTANCE_TYPES)
    )
    print("  ! Cost Explorer: Billing → Cost allocation tags 에서")
    print("    sagemaker:user-profile-arn / sagemaker:space-arn / Project 활성화 필요 (24~48h)")

    deploy_notebook_deny_alarm(cfg)


def deploy_notebook_deny_alarm(cfg: Dict[str, Any]) -> None:
    """
    Alarm when unauthorized Notebooks sc.* is detected.
    Stop is not reliable (DataZone portal authz); detection → SNS is the ops path.
    """
    print("\n==> Notebook deny SNS + CloudWatch alarm")
    account_id = (cfg.get("resources") or {}).get("account_id")
    if not account_id:
        account_id = boto3.client("sts").get_caller_identity()["Account"]
        set_resource(cfg, "account_id", account_id)

    sns = boto3.client("sns", region_name=REGION)
    cw = boto3.client("cloudwatch", region_name=REGION)
    namespace = f"SMUS/{PROJECT_NAME}"

    topic = sns.create_topic(
        Name=SNS_NOTEBOOK_DENY,
        Tags=[
            {"Key": "Project", "Value": PROJECT_NAME},
            {"Key": "ManagedBy", "Value": "smus-installer"},
        ],
    )
    topic_arn = topic["TopicArn"]
    set_resource(cfg, "notebook_deny_sns_arn", topic_arn)
    print(f"  ✓ SNS topic {SNS_NOTEBOOK_DENY}")

    email = (cfg.get("auth") or {}).get("idc_admin_email") or ""
    if email and "@" in email:
        try:
            sns.subscribe(TopicArn=topic_arn, Protocol="email", Endpoint=email)
            print(f"  · email subscription requested → {email} (confirm in inbox)")
            set_resource(cfg, "notebook_deny_sns_email", email)
        except ClientError as e:
            print(f"  ! sns subscribe: {e}")
    else:
        print("  ! no auth.idc_admin_email — subscribe manually to the SNS topic")

    # Allow CloudWatch to publish to the topic
    sns.set_topic_attributes(
        TopicArn=topic_arn,
        AttributeName="Policy",
        AttributeValue=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "AllowCWPublish",
                        "Effect": "Allow",
                        "Principal": {"Service": "cloudwatch.amazonaws.com"},
                        "Action": "sns:Publish",
                        "Resource": topic_arn,
                        "Condition": {
                            "ArnLike": {
                                "aws:SourceArn": (
                                    f"arn:aws:cloudwatch:{REGION}:{account_id}:alarm:{ALARM_NOTEBOOK_DENY}"
                                )
                            }
                        },
                    },
                    {
                        "Sid": "AllowAccountOwner",
                        "Effect": "Allow",
                        "Principal": {"AWS": f"arn:aws:iam::{account_id}:root"},
                        "Action": [
                            "sns:Subscribe",
                            "sns:Receive",
                            "sns:Publish",
                            "sns:GetTopicAttributes",
                            "sns:SetTopicAttributes",
                        ],
                        "Resource": topic_arn,
                    },
                ],
            }
        ),
    )

    allowed = ", ".join(ALLOWED_NOTEBOOK_INSTANCE_TYPES)
    cw.put_metric_alarm(
        AlarmName=ALARM_NOTEBOOK_DENY,
        AlarmDescription=(
            f"SMUS Notebooks unauthorized instance detected for {PROJECT_NAME}. "
            f"Allowed sc.* only: {allowed}. "
            "Automatic StopCompute is not reliable (DataZone portal authz); "
            "investigate portal / DDB inventory NOTEBOOK#* and stop manually if needed."
        ),
        Namespace=namespace,
        MetricName="NotebookComputeDeniedTotal",
        Statistic="Sum",
        Period=60,
        EvaluationPeriods=1,
        DatapointsToAlarm=1,
        Threshold=1.0,
        ComparisonOperator="GreaterThanOrEqualToThreshold",
        TreatMissingData="notBreaching",
        ActionsEnabled=True,
        AlarmActions=[topic_arn],
        OKActions=[],
        Tags=[
            {"Key": "Project", "Value": PROJECT_NAME},
            {"Key": "ManagedBy", "Value": "smus-installer"},
        ],
    )
    set_resource(cfg, "notebook_deny_alarm_name", ALARM_NOTEBOOK_DENY)
    alarm_url = (
        f"https://{REGION}.console.aws.amazon.com/cloudwatch/home"
        f"?region={REGION}#alarmsV2:alarm/{ALARM_NOTEBOOK_DENY}"
    )
    set_resource(cfg, "notebook_deny_alarm_url", alarm_url)
    print(f"  ✓ alarm {ALARM_NOTEBOOK_DENY} (metric NotebookComputeDeniedTotal >= 1 / 1m)")
    print(f"  · {alarm_url}")


def run_dashboard() -> None:
    print("\n==> Running dashboard.py")
    script = os.path.join(ROOT, "dashboard.py")
    subprocess.run([sys.executable, script], check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install SageMaker Unified Studio (smus-kr)")
    parser.add_argument("--idc-admin-email", default="", help="IdC admin user email to record")
    parser.add_argument("--skip-domain", action="store_true", help="Skip DataZone domain create")
    parser.add_argument(
        "--skip-project-profile",
        action="store_true",
        help="Skip Tooling/DataLake enable + default project profile/grants",
    )
    parser.add_argument("--skip-dashboard", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    cfg["projectName"] = PROJECT_NAME
    cfg["region"] = REGION
    cfg["domain_name"] = DOMAIN_NAME
    cfg["domain_version"] = DOMAIN_VERSION
    cfg["allowed_instance_types"] = ALLOWED_INSTANCE_TYPES
    cfg["allowed_notebook_instance_types"] = ALLOWED_NOTEBOOK_INSTANCE_TYPES
    cfg.setdefault("features", {})
    cfg["features"].update(
        {
            "bedrock_privatelink": True,
            "direct_connect": False,
            "data_agent_disabled": True,
            "monitoring_enabled": True,
            "notebook_compute_posthoc_guard": True,
        }
    )
    cfg.setdefault("auth", {})
    cfg["auth"]["type"] = "IAM_IDC"
    cfg["auth"]["user_assignment"] = cfg["auth"].get("user_assignment") or "MANUAL"
    if args.idc_admin_email:
        cfg["auth"]["idc_admin_email"] = args.idc_admin_email
    save_config(cfg)

    clients = _clients(REGION)
    account_id = clients["sts"].get_caller_identity()["Account"]
    set_resource(cfg, "account_id", account_id)
    print(f"Account={account_id} Region={REGION} Project={PROJECT_NAME}")

    net = create_vpc(clients["ec2"], cfg)
    create_privatelink_endpoints(clients["ec2"], cfg, net)
    create_project_bucket(clients["s3"], account_id, cfg)
    roles = create_domain_roles(clients["iam"], account_id, cfg)
    idc_arn = resolve_idc_instance(clients["sso_admin"], cfg)

    if not args.skip_domain:
        create_or_get_domain(
            clients["datazone"],
            cfg,
            roles["domain_execution_role_arn"],
            roles["domain_service_role_arn"],
            idc_arn,
        )
    note_idc_admin(cfg, args.idc_admin_email or cfg.get("auth", {}).get("idc_admin_email"))

    if not args.skip_project_profile and cfg.get("resources", {}).get("domain_id"):
        setup_blueprints_and_default_profile(clients["datazone"], clients["iam"], cfg)

    attach_guardrails_to_project_roles(clients["iam"], cfg)
    apply_studio_ui_instance_hide(clients["sagemaker"], cfg)

    ensure_dynamodb_tables(clients["dynamodb"], cfg)
    deploy_monitoring_lambdas(
        clients["lambda"], clients["events"], cfg, roles["lambda_usage_role_arn"]
    )

    if not args.skip_dashboard:
        run_dashboard()

    print("\nDone. Review config.json and CloudWatch dashboard.")
    portal = cfg.get("resources", {}).get("portal_url")
    if portal:
        print(f"Unified Studio URL: {portal}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except NoCredentialsError:
        print("AWS credentials not found.", file=sys.stderr)
        raise SystemExit(2)
