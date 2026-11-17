#!/usr/bin/env python3
"""Tear down resources recorded in config.json (best-effort, thorough VPC cleanup)."""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any, Dict, List

import boto3
from botocore.exceptions import ClientError

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")


def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def wait_nat_deleted(ec2, nat_id: str, timeout: int = 600) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        nats = ec2.describe_nat_gateways(NatGatewayIds=[nat_id]).get("NatGateways") or []
        state = nats[0]["State"] if nats else "deleted"
        print(f"  · NAT {nat_id} state={state}")
        if state == "deleted":
            return
        time.sleep(15)
    print(f"  ! NAT {nat_id} still not deleted after wait")


def delete_vpc_stack(ec2, res: Dict[str, Any]) -> None:
    vpc_id = res.get("vpc_id")
    if not vpc_id:
        print("· no vpc_id")
        return

    # Endpoints
    for ep in res.get("vpc_endpoint_ids") or []:
        try:
            ec2.delete_vpc_endpoints(VpcEndpointIds=[ep])
            print(f"✓ endpoint {ep}")
        except ClientError as e:
            print(f"· endpoint {ep}: {e}")

    # Also discover leftover endpoints in VPC
    try:
        eps = ec2.describe_vpc_endpoints(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("VpcEndpoints") or []
        ids = [e["VpcEndpointId"] for e in eps]
        if ids:
            ec2.delete_vpc_endpoints(VpcEndpointIds=ids)
            print(f"✓ remaining endpoints: {ids}")
            time.sleep(10)
    except ClientError as e:
        print(f"· list endpoints: {e}")

    # NAT
    nat = res.get("nat_gateway_id")
    if nat:
        try:
            ec2.delete_nat_gateway(NatGatewayId=nat)
            print(f"✓ delete NAT requested {nat}")
            wait_nat_deleted(ec2, nat)
        except ClientError as e:
            print(f"· nat: {e}")

    # Release EIP
    eip = res.get("eip_allocation_id")
    if eip:
        try:
            ec2.release_address(AllocationId=eip)
            print(f"✓ released EIP {eip}")
        except ClientError as e:
            print(f"· eip: {e}")

    # Disassociate & delete subnets
    subnet_ids: List[str] = list(res.get("private_subnet_ids") or []) + list(
        res.get("public_subnet_ids") or []
    )
    try:
        discovered = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])[
            "Subnets"
        ]
        for s in discovered:
            if s["SubnetId"] not in subnet_ids:
                subnet_ids.append(s["SubnetId"])
    except ClientError:
        pass

    for sid in subnet_ids:
        try:
            ec2.delete_subnet(SubnetId=sid)
            print(f"✓ subnet {sid}")
        except ClientError as e:
            print(f"· subnet {sid}: {e}")

    # Route tables (non-main)
    try:
        rts = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])[
            "RouteTables"
        ]
        for rt in rts:
            is_main = any(a.get("Main") for a in rt.get("Associations") or [])
            if is_main:
                continue
            for assoc in rt.get("Associations") or []:
                if assoc.get("RouteTableAssociationId") and not assoc.get("Main"):
                    try:
                        ec2.disassociate_route_table(
                            AssociationId=assoc["RouteTableAssociationId"]
                        )
                    except ClientError:
                        pass
            try:
                ec2.delete_route_table(RouteTableId=rt["RouteTableId"])
                print(f"✓ route table {rt['RouteTableId']}")
            except ClientError as e:
                print(f"· rtb {rt['RouteTableId']}: {e}")
    except ClientError as e:
        print(f"· route tables: {e}")

    # IGW
    igw = res.get("igw_id")
    if not igw:
        try:
            igws = ec2.describe_internet_gateways(
                Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]
            ).get("InternetGateways") or []
            if igws:
                igw = igws[0]["InternetGatewayId"]
        except ClientError:
            pass
    if igw:
        try:
            ec2.detach_internet_gateway(InternetGatewayId=igw, VpcId=vpc_id)
        except ClientError:
            pass
        try:
            ec2.delete_internet_gateway(InternetGatewayId=igw)
            print(f"✓ igw {igw}")
        except ClientError as e:
            print(f"· igw: {e}")

    # Security groups (non-default)
    try:
        sgs = ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])[
            "SecurityGroups"
        ]
        for sg in sgs:
            if sg.get("GroupName") == "default":
                continue
            try:
                ec2.delete_security_group(GroupId=sg["GroupId"])
                print(f"✓ sg {sg['GroupId']}")
            except ClientError as e:
                print(f"· sg {sg['GroupId']}: {e}")
    except ClientError as e:
        print(f"· sgs: {e}")

    # VPC
    try:
        ec2.delete_vpc(VpcId=vpc_id)
        print(f"✓ vpc {vpc_id}")
    except ClientError as e:
        print(f"· vpc {vpc_id}: {e}")


def delete_iam(res: Dict[str, Any], region: str, project: str) -> None:
    iam = boto3.client("iam")
    role_names = [
        f"role-domain-execution-for-{project}-{region}",
        f"role-domain-service-for-{project}-{region}",
        f"role-provisioning-for-{project}-{region}",
        f"role-manage-access-for-{project}-{region}",
        f"role-model-provisioning-for-{project}-{region}",
        f"role-model-consumption-for-{project}-{region}",
        f"role-lambda-usage-monitor-for-{project}-{region}",
    ]
    for name in role_names:
        try:
            for p in iam.list_attached_role_policies(RoleName=name).get("AttachedPolicies") or []:
                iam.detach_role_policy(RoleName=name, PolicyArn=p["PolicyArn"])
            for p in iam.list_role_policies(RoleName=name).get("PolicyNames") or []:
                iam.delete_role_policy(RoleName=name, PolicyName=p)
            iam.delete_role(RoleName=name)
            print(f"✓ role {name}")
        except ClientError as e:
            print(f"· role {name}: {e}")

    policy_name = f"policy-project-user-guardrails-for-{project}-{region}"
    account = res.get("account_id")
    if account:
        arn = f"arn:aws:iam::{account}:policy/{policy_name}"
        try:
            for v in iam.list_policy_versions(PolicyArn=arn).get("Versions") or []:
                if not v.get("IsDefaultVersion"):
                    iam.delete_policy_version(PolicyArn=arn, VersionId=v["VersionId"])
            iam.delete_policy(PolicyArn=arn)
            print(f"✓ policy {policy_name}")
        except ClientError as e:
            print(f"· policy {policy_name}: {e}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="Skip confirmation")
    parser.add_argument("--delete-domain", action="store_true")
    parser.add_argument("--delete-vpc", action="store_true", help="Also delete VPC/NAT/endpoints")
    parser.add_argument("--delete-iam", action="store_true", help="Delete SMUS IAM roles/policies")
    parser.add_argument("--delete-s3", action="store_true", help="Empty and delete project bucket")
    args = parser.parse_args()

    cfg = load_config()
    region = cfg.get("region", "ap-northeast-2")
    res = cfg.get("resources") or {}
    project = cfg.get("projectName", "smus-kr")

    if not args.yes:
        print("This will delete monitoring Lambdas/rules/tables and optionally domain/VPC/IAM.")
        print("Re-run with --yes [--delete-domain] [--delete-vpc] [--delete-iam] [--delete-s3]")
        return 1

    print(f"Uninstalling project={project} region={region}")

    lam = boto3.client("lambda", region_name=region)
    events = boto3.client("events", region_name=region)
    dynamodb = boto3.client("dynamodb", region_name=region)
    cw = boto3.client("cloudwatch", region_name=region)
    datazone = boto3.client("datazone", region_name=region)
    ec2 = boto3.client("ec2", region_name=region)
    s3 = boto3.client("s3", region_name=region)

    for rule in res.get("eventbridge_rules") or []:
        try:
            targets = events.list_targets_by_rule(Rule=rule).get("Targets") or []
            if targets:
                events.remove_targets(Rule=rule, Ids=[t["Id"] for t in targets])
            events.delete_rule(Name=rule)
            print(f"✓ deleted rule {rule}")
        except ClientError as e:
            print(f"· rule {rule}: {e}")

    for fn in (
        (res.get("lambda_usage_arn") or "").split(":function:")[-1],
        (res.get("lambda_cost_arn") or "").split(":function:")[-1],
        f"lambda-usage-monitor-for-{project}",
        f"lambda-cost-rollup-for-{project}",
    ):
        if not fn:
            continue
        try:
            lam.delete_function(FunctionName=fn)
            print(f"✓ deleted lambda {fn}")
        except ClientError as e:
            print(f"· lambda {fn}: {e}")

    for table in (res.get("inventory_table"), res.get("events_table"), res.get("cost_table")):
        if not table:
            continue
        try:
            dynamodb.delete_table(TableName=table)
            print(f"✓ deleting table {table}")
        except ClientError as e:
            print(f"· table {table}: {e}")

    dash = res.get("dashboard_name")
    if dash:
        try:
            cw.delete_dashboards(DashboardNames=[dash])
            print(f"✓ deleted dashboard {dash}")
        except ClientError as e:
            print(f"· dashboard: {e}")

    if args.delete_domain and res.get("domain_id"):
        try:
            datazone.delete_domain(identifier=res["domain_id"], skipDeletionCheck=True)
            print(f"✓ delete_domain requested: {res['domain_id']}")
            for _ in range(60):
                try:
                    d = datazone.get_domain(identifier=res["domain_id"])
                    print(f"  · domain status={d.get('status')}")
                    if d.get("status") in ("DELETED", "DELETION_FAILED"):
                        break
                except ClientError as e:
                    if e.response["Error"]["Code"] in (
                        "ResourceNotFoundException",
                        "AccessDeniedException",
                        "ValidationException",
                    ):
                        print("  · domain gone")
                        break
                    print(f"  · get_domain: {e}")
                    break
                time.sleep(10)
        except ClientError as e:
            # try without skipDeletionCheck
            try:
                datazone.delete_domain(identifier=res["domain_id"])
                print(f"✓ delete_domain requested (no skip): {res['domain_id']}")
            except ClientError as e2:
                print(f"· domain: {e2}")

    if args.delete_vpc:
        delete_vpc_stack(ec2, res)

    if args.delete_iam:
        delete_iam(res, region, project)

    if args.delete_s3 and res.get("s3_bucket"):
        bucket = res["s3_bucket"]
        try:
            # empty bucket
            paginator = s3.get_paginator("list_object_versions")
            for page in paginator.paginate(Bucket=bucket):
                to_delete = []
                for obj in page.get("Versions") or []:
                    to_delete.append({"Key": obj["Key"], "VersionId": obj["VersionId"]})
                for obj in page.get("DeleteMarkers") or []:
                    to_delete.append({"Key": obj["Key"], "VersionId": obj["VersionId"]})
                if to_delete:
                    s3.delete_objects(Bucket=bucket, Delete={"Objects": to_delete})
            s3.delete_bucket(Bucket=bucket)
            print(f"✓ bucket {bucket}")
        except ClientError as e:
            # non-versioned fallback
            try:
                objs = s3.list_objects_v2(Bucket=bucket).get("Contents") or []
                if objs:
                    s3.delete_objects(
                        Bucket=bucket,
                        Delete={"Objects": [{"Key": o["Key"]} for o in objs]},
                    )
                s3.delete_bucket(Bucket=bucket)
                print(f"✓ bucket {bucket}")
            except ClientError as e2:
                print(f"· s3 {bucket}: {e2}")

    # Clear resources in config but keep project metadata for next install
    cfg["resources"] = {"account_id": res.get("account_id")}
    cfg["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print("✓ config.json resources cleared")
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
