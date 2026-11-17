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
# Notebooks managed compute (sc.*) — mirror JupyterLab ml.t3.* allowlist
ALLOWED_NOTEBOOK_INSTANCE_TYPES = [
    t.strip()
    for t in (
        os.environ.get("ALLOWED_NOTEBOOK_INSTANCE_TYPES")
        or "sc.t3.medium,sc.t3.large,sc.t3.xlarge"
    ).split(",")
    if t.strip()
]
QUARANTINE_SG_NAME = os.environ.get(
    "NOTEBOOK_QUARANTINE_SG_NAME", f"smus-notebook-quarantine-for-{PROJECT}"
)

sm = boto3.client("sagemaker", region_name=REGION)
iam = boto3.client("iam")
ec2 = boto3.client("ec2", region_name=REGION)
ddb = boto3.resource("dynamodb", region_name=REGION)
cw = boto3.client("cloudwatch", region_name=REGION)
ce = boto3.client("ce", region_name="us-east-1")  # Cost Explorer is global (IAD)
_session = boto3.Session(region_name=REGION)


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


def _ensure_project_role_trusts_lambda(role_name: str, lambda_role_arn: str) -> None:
    """Allow usage-monitor Lambda to AssumeRole into project user roles."""
    if not lambda_role_arn:
        return
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


def attach_guardrails_to_project_roles() -> Dict[str, Any]:
    """Keep Deny + notebook-stop allow/trust on new datazone_usr_role_*."""
    account = boto3.client("sts").get_caller_identity()["Account"]
    policy_arn = GUARDRAILS_POLICY_ARN
    if not policy_arn:
        policy_arn = (
            f"arn:aws:iam::{account}:policy/"
            f"policy-project-user-guardrails-for-{PROJECT}-{REGION}"
        )
    lambda_role_arn = (
        os.environ.get("LAMBDA_USAGE_ROLE_ARN")
        or f"arn:aws:iam::{account}:role/role-lambda-usage-monitor-for-{PROJECT}-{REGION}"
    )
    stop_doc = {
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

    attached: List[str] = []
    skipped: List[str] = []
    notebook_stop: List[str] = []
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
                    iam.put_role_policy(
                        RoleName=name,
                        PolicyName="smus-notebook-compute-stop",
                        PolicyDocument=json.dumps(stop_doc),
                    )
                    _ensure_project_role_trusts_lambda(name, lambda_role_arn)
                    notebook_stop.append(name)
                except ClientError as e:
                    errors.append(f"{name}: {e.response['Error'].get('Message', str(e))}")
    except ClientError as e:
        errors.append(str(e))

    return {
        "policy_arn": policy_arn,
        "attached": attached,
        "already": skipped,
        "notebook_stop": notebook_stop,
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


def _temp_creds(access_key: str, secret_key: str, token: str):
    class _TempCreds:
        pass

    c = _TempCreds()
    c.access_key = access_key
    c.secret_key = secret_key
    c.token = token
    return c


def _env_id_from_context(
    project_id: Optional[str],
    execution_role_arn: Optional[str],
    role_session_name: Optional[str],
) -> Optional[str]:
    """Tooling env id: session '<user>@<env>' or role datazone_usr_role_<project>_<env>."""
    if role_session_name and "@" in role_session_name:
        return role_session_name.split("@", 1)[1][:64] or None
    if execution_role_arn and project_id:
        name = execution_role_arn.split("/")[-1]
        prefix = f"{PROJECT_USER_ROLE_PREFIX}{project_id}_"
        if name.startswith(prefix):
            return name[len(prefix) :] or None
    return None


def _frozen_creds_for_stop(
    domain_id: str,
    project_id: str,
    execution_role_arn: Optional[str],
    source_identity: Optional[str] = None,
    role_session_name: Optional[str] = None,
    environment_id: Optional[str] = None,
):
    """
    DataZone domain authz rejects bare AssumeRole into datazone_usr_role_*.
    Portal-equivalent path: AssumeRole (+ SourceIdentity) → GetEnvironmentCredentials
    → use those short-lived creds for StopCompute.
    """
    meta: Dict[str, Any] = {"assumed_role": None, "auth": "lambda_role"}
    if not (
        execution_role_arn
        and execution_role_arn.split("/")[-1].startswith(PROJECT_USER_ROLE_PREFIX)
    ):
        return _session.get_credentials().get_frozen_credentials(), meta

    try:
        sts = boto3.client("sts", region_name=REGION)
        kwargs: Dict[str, Any] = {
            "RoleArn": execution_role_arn,
            "RoleSessionName": (role_session_name or "smus-notebook-guard")[:64],
            "DurationSeconds": 900,
        }
        if source_identity:
            kwargs["SourceIdentity"] = source_identity[:64]
        assumed = sts.assume_role(**kwargs)["Credentials"]
        meta["assumed_role"] = execution_role_arn
        meta["auth"] = "assume_role"

        env_id = environment_id or _env_id_from_context(
            project_id, execution_role_arn, role_session_name
        )
        if not env_id:
            print("stop auth: missing environment_id; falling back to assumed role")
            return (
                _temp_creds(
                    assumed["AccessKeyId"],
                    assumed["SecretAccessKey"],
                    assumed["SessionToken"],
                ),
                meta,
            )

        dz = boto3.client(
            "datazone",
            region_name=REGION,
            aws_access_key_id=assumed["AccessKeyId"],
            aws_secret_access_key=assumed["SecretAccessKey"],
            aws_session_token=assumed["SessionToken"],
        )
        ec = dz.get_environment_credentials(
            domainIdentifier=domain_id, environmentIdentifier=env_id
        )
        meta["auth"] = "environment_credentials"
        meta["environment_id"] = env_id
        return (
            _temp_creds(ec["accessKeyId"], ec["secretAccessKey"], ec["sessionToken"]),
            meta,
        )
    except ClientError as e:
        print(f"stop auth failed: {e}")
        meta["auth_error"] = str(e)
        return _session.get_credentials().get_frozen_credentials(), meta


def _stop_notebook_compute(
    domain_id: str,
    project_id: str,
    notebook_id: str,
    compute_id: str,
    execution_role_arn: Optional[str] = None,
    source_identity: Optional[str] = None,
    role_session_name: Optional[str] = None,
    environment_id: Optional[str] = None,
    attempts: int = 4,
) -> Dict[str, Any]:
    """
    Stop SMUS Notebooks compute via DataZone REST (portal smithy: StopCompute).
    CloudTrail may log StartNotebookCompute/StopNotebookCompute for the same calls.
    POST /v2/domains/{domainId}/computes/{computeId}
    body: {projectIdentifier, notebookIdentifier}
    Auth: GetEnvironmentCredentials after AssumeRole into datazone_usr_role_*.
    """
    import urllib.error
    import urllib.request

    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    endpoint = f"https://datazone.{REGION}.api.aws"
    url = f"{endpoint}/v2/domains/{domain_id}/computes/{compute_id}"
    body = json.dumps(
        {
            "projectIdentifier": project_id,
            "notebookIdentifier": notebook_id,
        }
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "datazone-domain-id": domain_id,
        "datazone-project-id": project_id,
        "--datazone-domain-id": domain_id,
        "--datazone-project-id": project_id,
        "--datazone-domain-region": REGION,
    }
    creds, meta = _frozen_creds_for_stop(
        domain_id,
        project_id,
        execution_role_arn,
        source_identity=source_identity,
        role_session_name=role_session_name,
        environment_id=environment_id,
    )
    last: Dict[str, Any] = {"ok": False, **meta}
    for i in range(max(1, attempts)):
        request = AWSRequest(method="POST", url=url, data=body, headers=headers)
        SigV4Auth(creds, "datazone", REGION).add_auth(request)
        prepared = request.prepare()
        http_req = urllib.request.Request(
            prepared.url,
            data=body,
            headers=dict(prepared.headers),
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    payload = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    payload = {"raw": raw}
                return {
                    "ok": True,
                    "status": resp.status,
                    "body": payload,
                    "attempt": i + 1,
                    **meta,
                }
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            last = {
                "ok": False,
                "status": e.code,
                "error": err,
                "attempt": i + 1,
                **meta,
            }
            # Race: computeId exists in Start response before Stop is ready
            if e.code in (404, 409, 425) and i + 1 < attempts:
                time.sleep(2 * (i + 1))
                continue
            return last
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e), "attempt": i + 1, **meta}
    return last


def _find_compute_enis(compute_id: str) -> List[Dict[str, Any]]:
    """Customer-VPC ENIs created by DataZone for a notebook compute environment."""
    if not compute_id:
        return []
    try:
        pages = ec2.get_paginator("describe_network_interfaces").paginate(
            Filters=[
                {
                    "Name": "description",
                    "Values": [f"*ComputeEnvironment({compute_id})*"],
                }
            ]
        )
        found: List[Dict[str, Any]] = []
        for page in pages:
            found.extend(page.get("NetworkInterfaces") or [])
        if found:
            return found
        # Fallback: broader scan (description filter wildcards are limited)
        for page in ec2.get_paginator("describe_network_interfaces").paginate():
            for eni in page.get("NetworkInterfaces") or []:
                desc = eni.get("Description") or ""
                if compute_id in desc and "ComputeEnvironment" in desc:
                    found.append(eni)
        return found
    except ClientError as e:
        print(f"describe ENI failed: {e}")
        return []


def _ensure_quarantine_sg(vpc_id: str) -> str:
    existing = ec2.describe_security_groups(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "group-name", "Values": [QUARANTINE_SG_NAME]},
        ]
    ).get("SecurityGroups") or []
    if existing:
        return existing[0]["GroupId"]
    sg_id = ec2.create_security_group(
        GroupName=QUARANTINE_SG_NAME,
        Description=(
            "SMUS notebook guard deny-all quarantine for unauthorized sc.* compute ENIs"
        ),
        VpcId=vpc_id,
        TagSpecifications=[
            {
                "ResourceType": "security-group",
                "Tags": [
                    {"Key": "Name", "Value": QUARANTINE_SG_NAME},
                    {"Key": "Project", "Value": PROJECT},
                ],
            }
        ],
    )["GroupId"]
    # Default SG allows all egress — revoke so the ENI is fully isolated.
    for perm in (
        {"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
        {"IpProtocol": "-1", "Ipv6Ranges": [{"CidrIpv6": "::/0"}]},
    ):
        try:
            ec2.revoke_security_group_egress(GroupId=sg_id, IpPermissions=[perm])
        except ClientError:
            pass
    return sg_id


def _quarantine_notebook_compute(compute_id: str) -> Dict[str, Any]:
    """
    Portal StopCompute needs im.amazonaws.com session context we cannot mint.
    Fallback: move DataZone-managed compute ENI onto a deny-all security group
    in the customer VPC so the unauthorized instance cannot keep working.
    """
    enis = _find_compute_enis(compute_id)
    if not enis:
        return {"ok": False, "reason": "eni_not_found", "compute_id": compute_id}
    results = []
    for eni in enis:
        eni_id = eni["NetworkInterfaceId"]
        vpc_id = eni.get("VpcId")
        if not vpc_id:
            results.append({"eni": eni_id, "ok": False, "error": "missing_vpc"})
            continue
        try:
            qsg = _ensure_quarantine_sg(vpc_id)
            before = [g["GroupId"] for g in (eni.get("Groups") or [])]
            if before == [qsg]:
                results.append(
                    {"eni": eni_id, "ok": True, "already": True, "groups": before}
                )
                continue
            ec2.modify_network_interface_attribute(
                NetworkInterfaceId=eni_id, Groups=[qsg]
            )
            results.append(
                {
                    "eni": eni_id,
                    "ok": True,
                    "before_groups": before,
                    "quarantine_sg": qsg,
                }
            )
        except ClientError as e:
            results.append(
                {
                    "eni": eni_id,
                    "ok": False,
                    "error": e.response["Error"].get("Message", str(e)),
                }
            )
    ok = any(r.get("ok") for r in results)
    return {"ok": ok, "method": "eni_quarantine", "enis": results}


def handle_notebook_compute_guard(event: Dict[str, Any]) -> Dict[str, Any]:
    """Post-hoc guard: unauthorized Notebooks sc.* → StopCompute."""
    detail = event.get("detail") or {}
    event_name = detail.get("eventName") or ""
    if event_name != "StartNotebookCompute":
        return {"skipped": True, "reason": f"eventName={event_name}"}

    if detail.get("errorCode"):
        return {"skipped": True, "reason": "start_failed", "error": detail.get("errorCode")}

    req = detail.get("requestParameters") or {}
    resp = detail.get("responseElements") or {}
    itype = ((req.get("computeConfig") or {}).get("instanceType")) or (
        (resp.get("computeConfig") or {}).get("instanceType")
    )
    domain_id = req.get("domainId") or resp.get("domainId")
    project_id = req.get("projectId") or resp.get("projectId")
    notebook_id = req.get("notebookId") or resp.get("notebookId")
    compute_id = resp.get("computeId") or req.get("computeId")

    allowed = itype in ALLOWED_NOTEBOOK_INSTANCE_TYPES
    record = {
        "pk": f"NOTEBOOK_COMPUTE#{compute_id or notebook_id or 'unknown'}",
        "sk": detail.get("eventTime") or _now_iso(),
        "event_name": event_name,
        "instance_type": itype or "unknown",
        "domain_id": domain_id,
        "project_id": project_id,
        "notebook_id": notebook_id,
        "compute_id": compute_id,
        "allowed": allowed,
        "project": PROJECT,
        "ingested_at": _now_iso(),
    }

    events_table = ddb.Table(EVENTS_TABLE)
    inventory = ddb.Table(INVENTORY_TABLE)
    events_table.put_item(Item=_to_ddb(record))
    inventory.put_item(
        Item=_to_ddb(
            {
                "pk": f"NOTEBOOK#{domain_id}#{notebook_id}",
                "sk": "COMPUTE",
                "resource_type": "NotebookCompute",
                "domain_id": domain_id,
                "project_id": project_id,
                "notebook_id": notebook_id,
                "compute_id": compute_id,
                "instance_type": itype or "unknown",
                "allowed": allowed,
                "updated_at": _now_iso(),
                "project": PROJECT,
            }
        )
    )

    if allowed:
        _put_metric(
            "NotebookComputeAllowed",
            1.0,
            [{"Name": "InstanceType", "Value": (itype or "unknown")[:100]}],
        )
        return {"action": "allow", "instance_type": itype, "compute_id": compute_id}

    _put_metric(
        "NotebookComputeDenied",
        1.0,
        [{"Name": "InstanceType", "Value": (itype or "unknown")[:100]}],
    )
    # Undimensioned twin for CloudWatch alarm (any unauthorized sc.*)
    _put_metric("NotebookComputeDeniedTotal", 1.0)

    if not all([domain_id, project_id, notebook_id, compute_id]):
        return {
            "action": "deny_missing_ids",
            "instance_type": itype,
            "ids": {
                "domain_id": domain_id,
                "project_id": project_id,
                "notebook_id": notebook_id,
                "compute_id": compute_id,
            },
        }

    exec_role = ((req.get("executionRoleConfig") or {}).get("roleArn")) or (
        (resp.get("executionRoleConfig") or {}).get("roleArn")
    )
    ui = detail.get("userIdentity") or {}
    source_identity = (ui.get("sessionContext") or {}).get("sourceIdentity") or None
    # Portal session name looks like "<userId>@<envId>"
    principal_id = ui.get("principalId") or ""
    role_session_name = None
    if ":" in principal_id:
        role_session_name = principal_id.split(":", 1)[1][:64]
    environment_id = _env_id_from_context(project_id, exec_role, role_session_name)

    # Brief wait so compute ENI/API are ready; Stop may still 403 without portal IM session
    time.sleep(3)
    stop = _stop_notebook_compute(
        domain_id,
        project_id,
        notebook_id,
        compute_id,
        execution_role_arn=exec_role,
        source_identity=source_identity,
        role_session_name=role_session_name,
        environment_id=environment_id,
    )
    # Reliable enforcement: quarantine customer-VPC DataZone compute ENI (deny-all SG)
    quarantine = _quarantine_notebook_compute(compute_id)
    if not quarantine.get("ok"):
        # ENI can appear a few seconds after Start STARTING
        time.sleep(5)
        quarantine = _quarantine_notebook_compute(compute_id)

    enforced = bool(stop.get("ok") or quarantine.get("ok"))
    _put_metric(
        "NotebookComputeStopAttempts",
        1.0,
        [{"Name": "Result", "Value": "ok" if enforced else "error"}],
    )
    result = {
        "stop": stop,
        "quarantine": quarantine,
        "enforced": enforced,
    }
    inventory.update_item(
        Key={"pk": f"NOTEBOOK#{domain_id}#{notebook_id}", "sk": "COMPUTE"},
        UpdateExpression="SET stop_result=:s, stopped_at=:t, updated_at=:t",
        ExpressionAttributeValues={":s": _to_ddb(result), ":t": _now_iso()},
    )
    return {
        "action": "deny_stop",
        "instance_type": itype,
        "compute_id": compute_id,
        "stop": stop,
        "quarantine": quarantine,
        "enforced": enforced,
        "allowed_types": ALLOWED_NOTEBOOK_INSTANCE_TYPES,
    }

def lambda_handler(event, context):
    mode = (event or {}).get("mode")
    if not mode:
        source = (event or {}).get("source")
        detail = (event or {}).get("detail") or {}
        if source == "aws.datazone" or detail.get("eventSource") == "datazone.amazonaws.com":
            if detail.get("eventName") == "StartNotebookCompute":
                mode = "notebook_guard"
            else:
                mode = "event"
        elif source in ("aws.sagemaker", "aws.cloudtrail") or (event or {}).get(
            "detail-type"
        ):
            mode = "event"
        else:
            mode = "reconcile"

    print(json.dumps({"mode": mode, "project": PROJECT}, default=str))

    if mode == "notebook_guard":
        result = handle_notebook_compute_guard(event)
    elif mode == "event":
        result = handle_api_event(event)
    elif mode == "cost":
        result = rollup_cost(int((event or {}).get("days") or 7))
    else:
        inv = reconcile_inventory()
        guard = attach_guardrails_to_project_roles()
        result = {"inventory": inv, "guardrails": guard}

    return {"ok": True, "mode": mode, "result": result}
