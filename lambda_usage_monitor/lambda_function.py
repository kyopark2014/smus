"""
SMUS usage monitor / cost rollup Lambda.

Modes (event["mode"]):
  - reconcile (default / schedule): List SageMaker Spaces & Apps → inventory + metrics
  - event: EventBridge SageMaker/CloudTrail event → usage_events + inventory upsert
  - cost: Cost Explorer daily rollup by user-profile / project tag
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION") or os.environ.get("REGION") or "ap-northeast-1"
PROJECT = os.environ.get("PROJECT_NAME", "smus-kr")
INVENTORY_TABLE = os.environ.get("INVENTORY_TABLE", f"{PROJECT}-resource-inventory")
EVENTS_TABLE = os.environ.get("EVENTS_TABLE", f"{PROJECT}-usage-events")
COST_TABLE = os.environ.get("COST_TABLE", f"{PROJECT}-cost-by-user")
NAMESPACE = os.environ.get("METRIC_NAMESPACE", f"SMUS/{PROJECT}")
GUARDRAILS_POLICY_ARN = os.environ.get(
    "GUARDRAILS_POLICY_ARN",
    "",
)
PROJECT_USER_ROLE_PREFIX = "datazone_usr_role_"

sm = boto3.client("sagemaker", region_name=REGION)
iam = boto3.client("iam")
ddb = boto3.resource("dynamodb", region_name=REGION)
cw = boto3.client("cloudwatch", region_name=REGION)
ce = boto3.client("ce", region_name="us-east-1")  # Cost Explorer is global (IAD)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_ddb(obj: Any) -> Any:
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _to_ddb(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_ddb(v) for v in obj]
    return obj


def _put_metric(name: str, value: float, dims: Optional[List[Dict[str, str]]] = None) -> None:
    metric: Dict[str, Any] = {
        "MetricName": name,
        "Timestamp": datetime.now(timezone.utc),
        "Value": value,
        "Unit": "Count",
    }
    if dims:
        metric["Dimensions"] = dims
    cw.put_metric_data(Namespace=NAMESPACE, MetricData=[metric])


def _space_instance_type(space: Dict[str, Any]) -> str:
    settings = space.get("SpaceSettings") or {}
    for key in ("JupyterLabAppSettings", "CodeEditorAppSettings", "JupyterServerAppSettings", "KernelGatewayAppSettings"):
        app = settings.get(key) or {}
        spec = app.get("DefaultResourceSpec") or {}
        if spec.get("InstanceType"):
            return spec["InstanceType"]
    return "unknown"


def _space_app_type(space: Dict[str, Any]) -> str:
    settings = space.get("SpaceSettings") or {}
    return settings.get("AppType") or "unknown"


def _owner(space: Dict[str, Any]) -> str:
    ownership = space.get("OwnershipSettings") or {}
    return ownership.get("OwnerUserProfileName") or "unknown"


def attach_guardrails_to_project_roles() -> Dict[str, Any]:
    """Keep Deny policy attached as new SMUS projects create datazone_usr_role_*."""
    policy_arn = GUARDRAILS_POLICY_ARN
    if not policy_arn:
        account = boto3.client("sts").get_caller_identity()["Account"]
        policy_arn = (
            f"arn:aws:iam::{account}:policy/"
            f"policy-project-user-guardrails-for-{PROJECT}-{REGION}"
        )

    attached: List[str] = []
    skipped: List[str] = []
    errors: List[str] = []
    try:
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
                        skipped.append(name)
                    else:
                        iam.attach_role_policy(RoleName=name, PolicyArn=policy_arn)
                        attached.append(name)
                except ClientError as e:
                    errors.append(f"{name}: {e.response['Error'].get('Message', str(e))}")
    except ClientError as e:
        errors.append(str(e))

    return {
        "policy_arn": policy_arn,
        "attached": attached,
        "already": skipped,
        "errors": errors,
    }


def reconcile_inventory() -> Dict[str, Any]:
    inventory = ddb.Table(INVENTORY_TABLE)
    spaces: List[Dict[str, Any]] = []
    next_token = None
    while True:
        kwargs: Dict[str, Any] = {}
        if next_token:
            kwargs["NextToken"] = next_token
        resp = sm.list_spaces(**kwargs)
        spaces.extend(resp.get("Spaces") or [])
        next_token = resp.get("NextToken")
        if not next_token:
            break

    by_type: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    by_user: Dict[str, int] = {}
    detailed = 0

    for summary in spaces:
        domain_id = summary["DomainId"]
        space_name = summary["SpaceName"]
        try:
            detail = sm.describe_space(DomainId=domain_id, SpaceName=space_name)
        except ClientError as e:
            print(f"describe_space failed: {space_name}: {e}")
            continue

        instance_type = _space_instance_type(detail)
        app_type = _space_app_type(detail)
        owner = _owner(detail)
        status = detail.get("Status") or summary.get("Status") or "Unknown"

        item = {
            "pk": f"SPACE#{domain_id}#{space_name}",
            "sk": "INVENTORY",
            "resource_type": "Space",
            "domain_id": domain_id,
            "space_name": space_name,
            "app_type": app_type,
            "instance_type": instance_type,
            "owner_user_profile": owner,
            "status": status,
            "last_modified": detail.get("LastModifiedTime").isoformat()
            if detail.get("LastModifiedTime")
            else None,
            "updated_at": _now_iso(),
            "project": PROJECT,
        }
        inventory.put_item(Item=_to_ddb(item))
        detailed += 1
        by_type[instance_type] = by_type.get(instance_type, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
        by_user[owner] = by_user.get(owner, 0) + 1

    _put_metric("ActiveSpaces", float(detailed))
    for itype, count in by_type.items():
        _put_metric("SpacesByInstanceType", float(count), [{"Name": "InstanceType", "Value": itype}])
    for status, count in by_status.items():
        _put_metric("SpacesByStatus", float(count), [{"Name": "Status", "Value": status}])
    for user, count in by_user.items():
        safe_user = user[:200] or "unknown"
        _put_metric("SpacesByUser", float(count), [{"Name": "UserProfile", "Value": safe_user}])

    return {
        "spaces_listed": len(spaces),
        "spaces_detailed": detailed,
        "by_instance_type": by_type,
        "by_status": by_status,
        "by_user": by_user,
    }


def handle_api_event(event: Dict[str, Any]) -> Dict[str, Any]:
    events_table = ddb.Table(EVENTS_TABLE)
    inventory = ddb.Table(INVENTORY_TABLE)

    detail = event.get("detail") or {}
    event_name = detail.get("eventName") or event.get("detail-type") or "Unknown"
    request = detail.get("requestParameters") or {}
    response = detail.get("responseElements") or {}
    user_identity = detail.get("userIdentity") or {}

    domain_id = request.get("domainId") or response.get("domainId") or "unknown"
    space_name = request.get("spaceName") or response.get("spaceName") or "unknown"
    instance_type = (
        (((request.get("spaceSettings") or {}).get("jupyterLabAppSettings") or {})
         .get("defaultResourceSpec") or {})
        .get("instanceType")
    ) or "unknown"
    owner = (
        ((request.get("ownershipSettings") or {}).get("ownerUserProfileName"))
        or user_identity.get("userName")
        or "unknown"
    )

    event_id = detail.get("eventID") or f"{event_name}-{int(time.time() * 1000)}"
    events_table.put_item(
        Item=_to_ddb(
            {
                "pk": f"EVENT#{event_id}",
                "sk": detail.get("eventTime") or _now_iso(),
                "event_name": event_name,
                "domain_id": domain_id,
                "space_name": space_name,
                "instance_type": instance_type,
                "owner_user_profile": owner,
                "raw_source": event.get("source"),
                "project": PROJECT,
                "ingested_at": _now_iso(),
            }
        )
    )

    if space_name != "unknown" and domain_id != "unknown":
        inventory.update_item(
            Key={"pk": f"SPACE#{domain_id}#{space_name}", "sk": "INVENTORY"},
            UpdateExpression=(
                "SET resource_type=:rt, domain_id=:d, space_name=:s, "
                "instance_type=:it, owner_user_profile=:o, updated_at=:u, "
                "last_event=:e, project=:p"
            ),
            ExpressionAttributeValues={
                ":rt": "Space",
                ":d": domain_id,
                ":s": space_name,
                ":it": instance_type,
                ":o": owner,
                ":u": _now_iso(),
                ":e": event_name,
                ":p": PROJECT,
            },
        )

    _put_metric("UsageEvents", 1.0, [{"Name": "EventName", "Value": str(event_name)[:100]}])
    return {"stored_event": event_id, "event_name": event_name}


def rollup_cost(days: int = 7) -> Dict[str, Any]:
    """Roll up Cost Explorer by tag Project and sagemaker:user-profile-arn if activated."""
    cost_table = ddb.Table(COST_TABLE)
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)

    results: List[Dict[str, Any]] = []

    def _query(group_by_tag: str) -> List[Dict[str, Any]]:
        try:
            resp = ce.get_cost_and_usage(
                TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
                Granularity="DAILY",
                Metrics=["UnblendedCost"],
                Filter={
                    "Tags": {
                        "Key": "Project",
                        "Values": [PROJECT],
                    }
                },
                GroupBy=[{"Type": "TAG", "Key": group_by_tag}],
            )
            return resp.get("ResultsByTime") or []
        except ClientError as e:
            print(f"Cost Explorer query failed for tag {group_by_tag}: {e}")
            return []

    # Prefer user-profile ARN tag; also store Project totals.
    for tag_key in ("sagemaker:user-profile-arn", "sagemaker:space-arn", "Project"):
        for day in _query(tag_key):
            day_start = day.get("TimePeriod", {}).get("Start")
            for group in day.get("Groups") or []:
                keys = group.get("Keys") or ["unknown"]
                amount = float((group.get("Metrics") or {}).get("UnblendedCost", {}).get("Amount") or 0)
                label = keys[0].split("$")[-1] if keys else "unknown"
                item = {
                    "pk": f"COST#{tag_key}#{label}",
                    "sk": day_start or _now_iso()[:10],
                    "tag_key": tag_key,
                    "tag_value": label,
                    "amount": Decimal(str(round(amount, 6))),
                    "currency": (group.get("Metrics") or {})
                    .get("UnblendedCost", {})
                    .get("Unit", "USD"),
                    "project": PROJECT,
                    "updated_at": _now_iso(),
                }
                cost_table.put_item(Item=item)
                results.append({"tag": tag_key, "value": label, "day": day_start, "amount": amount})
                if tag_key == "sagemaker:user-profile-arn":
                    _put_metric(
                        "EstimatedCostByUser",
                        amount,
                        [{"Name": "UserProfile", "Value": (label or "unknown")[:200]}],
                    )

    return {"rows": len(results), "start": start.isoformat(), "end": end.isoformat()}


def lambda_handler(event, context):
    mode = (event or {}).get("mode")
    if not mode:
        if (event or {}).get("source") in ("aws.sagemaker", "aws.cloudtrail") or (
            event or {}
        ).get("detail-type"):
            mode = "event"
        else:
            mode = "reconcile"

    print(json.dumps({"mode": mode, "project": PROJECT}, default=str))

    if mode == "event":
        result = handle_api_event(event)
    elif mode == "cost":
        result = rollup_cost(int((event or {}).get("days") or 7))
    else:
        inv = reconcile_inventory()
        guard = attach_guardrails_to_project_roles()
        result = {"inventory": inv, "guardrails": guard}

    return {"ok": True, "mode": mode, "result": result}
