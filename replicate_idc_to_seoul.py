#!/usr/bin/env python3
"""
Replicate organization IAM Identity Center (primary: ap-northeast-1)
to ap-northeast-2 (Seoul), then wire SMUS domain SSO to IdC.

MUST run with credentials for the Organizations management account that
owns IdC (OwnerAccountId=677146750822). Member-account credentials cannot
create the required multi-Region CMK in that account.

Steps:
  1) Create multi-Region CMK in ap-northeast-1 (if missing)
  2) Create replica key in ap-northeast-2
  3) update-instance EncryptionConfiguration → CUSTOMER_MANAGED_KEY
  4) add-region ap-northeast-2 and wait until ACTIVE
  5) Optionally update smus-kr DataZone domain singleSignOn to IAM_IDC
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import ClientError

PRIMARY_REGION = "ap-northeast-1"
TARGET_REGION = "ap-northeast-2"
INSTANCE_ARN = "arn:aws:sso:::instance/ssoins-77587d4394df269b"
IDENTITY_STORE_ID = "d-95676b6bd2"
MGMT_ACCOUNT = "677146750822"
ORG_ID = "o-khxpvjh3iw"
ALIAS = "alias/idc-mrk-for-smus-kr"
ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")


def baseline_key_policy(mgmt_account: str) -> Dict[str, Any]:
    # From AWS docs: Baseline KMS key policy for IAM Identity Center
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowIdentityCenterAdminAccounts",
                "Effect": "Allow",
                "Principal": {"AWS": [f"arn:aws:iam::{mgmt_account}:root"]},
                "Action": "kms:*",
                "Resource": "*",
            },
            {
                "Sid": "AllowIdentityCenterAndIdentityStoreToDescribeKey",
                "Effect": "Allow",
                "Principal": {
                    "Service": ["identitystore.amazonaws.com", "sso.amazonaws.com"]
                },
                "Action": "kms:DescribeKey",
                "Resource": "*",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": "${aws:ResourceAccount}"}
                },
            },
            {
                "Sid": "AllowIdentityCenterAndIdentityStoreToUseKey",
                "Effect": "Allow",
                "Principal": {
                    "Service": ["identitystore.amazonaws.com", "sso.amazonaws.com"]
                },
                "Action": [
                    "kms:Decrypt",
                    "kms:ReEncryptTo",
                    "kms:ReEncryptFrom",
                    "kms:GenerateDataKeyWithoutPlaintext",
                ],
                "Resource": "*",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": "${aws:ResourceAccount}"},
                    "ForAnyValue:StringEquals": {
                        "kms:EncryptionContextKeys": [
                            "aws:sso:instance-arn",
                            "aws:identitystore:identitystore-arn",
                        ]
                    },
                },
            },
            {
                "Sid": "AllowOrgPrincipalsViaIdentityCenterAndIdentityStore",
                "Effect": "Allow",
                "Principal": "*",
                "Action": "kms:Decrypt",
                "Resource": "*",
                "Condition": {
                    "StringEquals": {"aws:PrincipalOrgID": "${aws:ResourceOrgID}"},
                    "StringLike": {
                        "kms:ViaService": [
                            "sso.*.amazonaws.com",
                            "identitystore.*.amazonaws.com",
                        ]
                    },
                    "ForAnyValue:StringEquals": {
                        "kms:EncryptionContextKeys": [
                            "aws:sso:instance-arn",
                            "aws:identitystore:identitystore-arn",
                        ]
                    },
                },
            },
            {
                "Sid": "AllowManagedApps",
                "Effect": "Allow",
                "Principal": "*",
                "Action": "kms:Decrypt",
                "Resource": "*",
                "Condition": {
                    "Bool": {"aws:PrincipalIsAWSService": "true"},
                    "StringEquals": {"aws:SourceOrgID": "${aws:ResourceOrgID}"},
                    "StringLike": {
                        "kms:ViaService": [
                            "identitystore.*.amazonaws.com",
                            "sso.*.amazonaws.com",
                        ]
                    },
                    "ForAnyValue:StringEquals": {
                        "kms:EncryptionContextKeys": [
                            "aws:sso:instance-arn",
                            "aws:identitystore:identitystore-arn",
                        ]
                    },
                },
            },
        ],
    }


def require_mgmt_account(sts) -> str:
    ident = sts.get_caller_identity()
    account = ident["Account"]
    print(f"Caller: {ident['Arn']} (account {account})")
    if account != MGMT_ACCOUNT:
        raise SystemExit(
            f"ERROR: IdC owner is {MGMT_ACCOUNT}, but credentials are for {account}.\n"
            f"Re-run with management-account credentials, e.g.:\n"
            f"  AWS_PROFILE=<mgmt-profile> python replicate_idc_to_seoul.py\n"
            f"Or: aws sso login --profile <mgmt-profile>"
        )
    return account


def find_alias(kms, alias_name: str) -> Optional[str]:
    paginator = kms.get_paginator("list_aliases")
    for page in paginator.paginate():
        for a in page.get("Aliases") or []:
            if a.get("AliasName") == alias_name and a.get("TargetKeyId"):
                return a["TargetKeyId"]
    return None


def ensure_primary_mrk(kms_primary, account: str) -> str:
    key_id = find_alias(kms_primary, ALIAS)
    if key_id:
        desc = kms_primary.describe_key(KeyId=key_id)["KeyMetadata"]
        arn = desc["Arn"]
        print(f"  · primary MRK exists: {arn}")
        if not desc.get("MultiRegion"):
            raise SystemExit(f"Alias {ALIAS} points to a non multi-Region key: {arn}")
        return arn

    print("  · creating multi-Region CMK in ap-northeast-1...")
    resp = kms_primary.create_key(
        Description="Multi-Region CMK for IAM Identity Center (smus-kr Seoul replication)",
        KeyUsage="ENCRYPT_DECRYPT",
        KeySpec="SYMMETRIC_DEFAULT",
        MultiRegion=True,
        Policy=json.dumps(baseline_key_policy(account)),
        Tags=[
            {"TagKey": "Project", "TagValue": "smus-kr"},
            {"TagKey": "ManagedBy", "TagValue": "replicate_idc_to_seoul"},
            {"TagKey": "Purpose", "TagValue": "IAM-Identity-Center"},
        ],
    )
    key_id = resp["KeyMetadata"]["KeyId"]
    arn = resp["KeyMetadata"]["Arn"]
    try:
        kms_primary.create_alias(AliasName=ALIAS, TargetKeyId=key_id)
    except ClientError as e:
        if e.response["Error"]["Code"] != "AlreadyExistsException":
            raise
    print(f"  ✓ created primary MRK: {arn}")
    return arn


def ensure_replica(kms_primary, kms_target, primary_arn: str) -> str:
    primary_key_id = primary_arn.split("/")[-1]
    # Check if replica already exists in target
    try:
        # MultiRegionConfiguration available on describe of primary
        meta = kms_primary.describe_key(KeyId=primary_key_id)["KeyMetadata"]
        replicas = (meta.get("MultiRegionConfiguration") or {}).get("ReplicaKeys") or []
        for r in replicas:
            if r.get("Region") == TARGET_REGION:
                print(f"  · replica already in {TARGET_REGION}: {r.get('Arn')}")
                return r["Arn"]
    except ClientError as e:
        print(f"  ! describe primary for replicas: {e}")

    print(f"  · creating replica key in {TARGET_REGION}...")
    resp = kms_primary.replicate_key(
        KeyId=primary_key_id,
        ReplicaRegion=TARGET_REGION,
        Description="Replica CMK for IAM Identity Center in Seoul",
        Tags=[
            {"TagKey": "Project", "TagValue": "smus-kr"},
            {"TagKey": "ManagedBy", "TagValue": "replicate_idc_to_seoul"},
        ],
    )
    replica_arn = resp["ReplicaKeyMetadata"]["Arn"]
    # Apply same key policy on replica (policies are NOT auto-synced)
    kms_target.put_key_policy(
        KeyId=replica_arn,
        PolicyName="default",
        Policy=json.dumps(baseline_key_policy(MGMT_ACCOUNT)),
    )
    try:
        kms_target.create_alias(AliasName=ALIAS, TargetKeyId=replica_arn)
    except ClientError:
        pass
    print(f"  ✓ replica: {replica_arn}")
    return replica_arn


def configure_idc_encryption(sso, primary_key_arn: str) -> None:
    print("  · update-instance encryption → CUSTOMER_MANAGED_KEY")
    sso.update_instance(
        InstanceArn=INSTANCE_ARN,
        EncryptionConfiguration={
            "KeyType": "CUSTOMER_MANAGED_KEY",
            "KmsKeyArn": primary_key_arn,
        },
    )
    print("  ✓ encryption configured")


def add_seoul_region(sso) -> None:
    print(f"  · add-region {TARGET_REGION}")
    try:
        resp = sso.add_region(InstanceArn=INSTANCE_ARN, RegionName=TARGET_REGION)
        print(f"  · status={resp.get('Status')}")
    except ClientError as e:
        msg = e.response["Error"].get("Message", str(e))
        if "already" in msg.lower() or "exists" in msg.lower():
            print(f"  · region already added: {msg}")
        else:
            raise

    for i in range(90):
        d = sso.describe_region(InstanceArn=INSTANCE_ARN, RegionName=TARGET_REGION)
        status = d.get("Status") or (d.get("Region") or {}).get("Status")
        # API shape may vary
        if isinstance(d.get("Region"), dict):
            status = d["Region"].get("Status", status)
        print(f"  · describe-region status={status} ({i})")
        if status == "ACTIVE":
            print("  ✓ Seoul IdC replica ACTIVE")
            return
        if status in ("FAILED", "CREATE_FAILED", "DELETION_FAILED"):
            raise RuntimeError(f"Region replication failed: {d}")
        time.sleep(20)
    raise TimeoutError("Seoul IdC replication did not become ACTIVE in time")


def update_smus_domain_sso(user_assignment: str = "MANUAL") -> None:
    if not os.path.exists(CONFIG_PATH):
        print("  · smus config.json missing; skip domain SSO update")
        return
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    domain_id = (cfg.get("resources") or {}).get("domain_id")
    if not domain_id:
        print("  · no domain_id in config; skip")
        return

    # Domain lives in member account — this call needs member-account or shared admin creds.
    # Try with current (mgmt) credentials first; if AccessDenied, record instructions.
    dz = boto3.client("datazone", region_name=TARGET_REGION)
    try:
        dz.update_domain(
            identifier=domain_id,
            singleSignOn={
                "type": "IAM_IDC",
                "userAssignment": user_assignment,
                "idcInstanceArn": INSTANCE_ARN,
            },
        )
        print(f"  ✓ domain {domain_id} SSO → IAM_IDC")
        cfg.setdefault("auth", {})
        cfg["auth"]["type"] = "IAM_IDC"
        cfg["auth"]["idc_instance_arn"] = INSTANCE_ARN
        cfg["auth"]["user_assignment"] = user_assignment
        cfg["auth"]["idc_primary_region"] = PRIMARY_REGION
        cfg["auth"]["idc_replicated_regions"] = [PRIMARY_REGION, TARGET_REGION]
        cfg["updated_at"] = datetime.now(timezone.utc).isoformat()
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except ClientError as e:
        print(f"  ! domain SSO update from mgmt account failed: {e}")
        print("    From member account (262976740991) run:")
        print(
            "    aws datazone update-domain --region ap-northeast-2 "
            f"--identifier {domain_id} "
            "--single-sign-on "
            f"type=IAM_IDC,userAssignment={user_assignment},idcInstanceArn={INSTANCE_ARN}"
        )


def verify_seoul_list_instances() -> None:
    sso = boto3.client("sso-admin", region_name=TARGET_REGION)
    inst = sso.list_instances().get("Instances") or []
    print(f"  · list-instances in {TARGET_REGION}: {json.dumps(inst, default=str)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-domain-sso", action="store_true")
    parser.add_argument(
        "--user-assignment",
        choices=["MANUAL", "AUTOMATIC"],
        default="MANUAL",
    )
    args = parser.parse_args()

    sts = boto3.client("sts")
    account = require_mgmt_account(sts)

    kms_p = boto3.client("kms", region_name=PRIMARY_REGION)
    kms_t = boto3.client("kms", region_name=TARGET_REGION)
    sso = boto3.client("sso-admin", region_name=PRIMARY_REGION)

    print("\n==> 1) Multi-Region CMK")
    primary_arn = ensure_primary_mrk(kms_p, account)
    ensure_replica(kms_p, kms_t, primary_arn)

    print("\n==> 2) Configure IdC encryption")
    configure_idc_encryption(sso, primary_arn)

    print("\n==> 3) Replicate IdC to Seoul")
    add_seoul_region(sso)
    verify_seoul_list_instances()

    if not args.skip_domain_sso:
        print("\n==> 4) Attach IdC to SMUS domain")
        update_smus_domain_sso(args.user_assignment)

    print("\nDone.")
    print("Next (console, Seoul domain):")
    print("  - Configure SSO user access / assign users")
    print("  - Disable SageMaker Data Agent toggle")
    print("  - Enable Bedrock models")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
