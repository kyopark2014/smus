#!/usr/bin/env python3
"""
Create / update CloudWatch dashboard for SMUS usage monitoring.

Reads config.json produced by installer.py.
Widgets cover:
  - Active spaces
  - Spaces by instance type / status / user
  - Usage API events
  - Estimated cost by user (when Cost Explorer rollup has data)
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

import boto3
from botocore.exceptions import ClientError

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")


def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: Dict[str, Any]) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _metric_widget(
    title: str,
    metrics: List[List[Any]],
    region: str,
    view: str = "timeSeries",
    stat: str = "Maximum",
    period: int = 900,
) -> Dict[str, Any]:
    return {
        "type": "metric",
        "properties": {
            "title": title,
            "region": region,
            "view": view,
            "stacked": False,
            "stat": stat,
            "period": period,
            "metrics": metrics,
        },
    }


def _text_widget(markdown: str) -> Dict[str, Any]:
    return {"type": "text", "properties": {"markdown": markdown}}


def build_body(cfg: Dict[str, Any]) -> Dict[str, Any]:
    project = cfg.get("projectName", "smus-kr")
    region = cfg.get("region", "ap-northeast-1")
    ns = f"SMUS/{project}"
    resources = cfg.get("resources") or {}
    domain_id = resources.get("domain_id", "(pending)")
    portal = resources.get("portal_url", "")
    inventory = resources.get("inventory_table", f"{project}-resource-inventory")
    cost_table = resources.get("cost_table", f"{project}-cost-by-user")

    allowed = ", ".join(cfg.get("allowed_instance_types") or [])
    widgets: List[Dict[str, Any]] = [
        _text_widget(
            f"""# SMUS usage — `{project}`
- Region: `{region}` · Domain: `{domain_id}`
- Portal: {portal or '_not yet available_'}
- Allowed instance types: `{allowed}`
- Inventory table: `{inventory}` · Cost table: `{cost_table}`
- Data Agent: **disabled** · Bedrock: **PrivateLink** · Direct Connect: **off**

> Cost-by-user metrics appear after Cost allocation tags are activated and `lambda-cost-rollup` runs.
"""
        ),
        _metric_widget(
            "Active Spaces (reconcile)",
            [[ns, "ActiveSpaces"]],
            region,
            view="singleValue",
            period=900,
        ),
        _metric_widget(
            "Spaces by instance type",
            [
                [ns, "SpacesByInstanceType", "InstanceType", "ml.t3.medium"],
                [ns, "SpacesByInstanceType", "InstanceType", "ml.t3.large"],
                [ns, "SpacesByInstanceType", "InstanceType", "ml.t3.xlarge"],
                [ns, "SpacesByInstanceType", "InstanceType", "unknown"],
            ],
            region,
            period=900,
        ),
        _metric_widget(
            "Spaces by status",
            [
                [ns, "SpacesByStatus", "Status", "InService"],
                [ns, "SpacesByStatus", "Status", "Pending"],
                [ns, "SpacesByStatus", "Status", "Failed"],
                [ns, "SpacesByStatus", "Status", "Deleting"],
                [ns, "SpacesByStatus", "Status", "Deleted"],
            ],
            region,
            period=900,
        ),
        _metric_widget(
            "Usage API events",
            [[ns, "UsageEvents", "EventName", "CreateSpace"],
             [ns, "UsageEvents", "EventName", "UpdateSpace"],
             [ns, "UsageEvents", "EventName", "DeleteSpace"],
             [ns, "UsageEvents", "EventName", "CreateApp"],
             [ns, "UsageEvents", "EventName", "DeleteApp"]],
            region,
            period=300,
            stat="Sum",
        ),
        _metric_widget(
            "Estimated cost by user (Cost Explorer rollup)",
            [[ns, "EstimatedCostByUser"]],
            region,
            period=86400,
            stat="Sum",
        ),
        _metric_widget(
            "Notebooks unauthorized sc.* (detect / alarm)",
            [
                [ns, "NotebookComputeDeniedTotal"],
                [ns, "NotebookComputeDenied", "InstanceType", "sc.g5.xlarge"],
                [ns, "NotebookComputeDenied", "InstanceType", "sc.g5.2xlarge"],
                [ns, "NotebookComputeDenied", "InstanceType", "sc.g4dn.2xlarge"],
            ],
            region,
            period=60,
            stat="Sum",
        ),
        _text_widget(
            f"""## How to drill down
1. DynamoDB → inventory: filter `resource_type = Space` → `owner_user_profile`, `instance_type`, `app_type`, `status`
2. DynamoDB → usage-events: Create/Update/Delete timeline
3. DynamoDB → cost-by-user: daily UnblendedCost by `sagemaker:user-profile-arn`
4. Enable Billing → Cost allocation tags: `Project`, `sagemaker:user-profile-arn`, `sagemaker:space-arn`
5. Notebooks deny alarm: `{resources.get("notebook_deny_alarm_name") or "alarm-smus-notebook-deny-for-" + project}`  
   SNS: `{resources.get("notebook_deny_sns_arn") or "(pending)"}` — **detect only** (force stop not reliable)
"""
        ),
    ]

    # layout: simple stacked rows
    y = 0
    laid: List[Dict[str, Any]] = []
    for i, w in enumerate(widgets):
        height = 3 if w["type"] == "text" else 6
        width = 24 if w["type"] == "text" else (6 if i == 1 else 12)
        if i == 1:
            width = 6
        laid.append({**w, "x": 0 if width == 24 or i <= 1 else (0 if i % 2 == 0 else 12), "y": y, "width": width, "height": height})
        if width == 24 or i == 1 or i % 2 == 1:
            y += height
    # Fix first metric singleValue next to text awkwardly — keep simple vertical stack
    laid = []
    y = 0
    for w in widgets:
        height = 4 if w["type"] == "text" else 6
        laid.append({**w, "x": 0, "y": y, "width": 24, "height": height})
        y += height

    return {"widgets": laid}


def create_dashboard(cfg: Dict[str, Any]) -> str:
    region = cfg.get("region", "ap-northeast-1")
    name = (cfg.get("resources") or {}).get("dashboard_name") or f"dashboard-{cfg.get('projectName', 'smus-kr')}-usage"
    cw = boto3.client("cloudwatch", region_name=region)
    body = build_body(cfg)
    cw.put_dashboard(DashboardName=name, DashboardBody=json.dumps(body))
    url = (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home"
        f"?region={region}#dashboards/dashboard/{name}"
    )
    cfg.setdefault("resources", {})["dashboard_name"] = name
    cfg["resources"]["dashboard_url"] = url
    save_config(cfg)
    print(f"✓ CloudWatch dashboard: {name}")
    print(f"  {url}")
    return name


def print_inventory_snapshot(cfg: Dict[str, Any]) -> None:
    region = cfg.get("region", "ap-northeast-1")
    table = (cfg.get("resources") or {}).get("inventory_table")
    if not table:
        return
    ddb = boto3.resource("dynamodb", region_name=region)
    try:
        items = ddb.Table(table).scan(Limit=50).get("Items") or []
    except ClientError as e:
        print(f"  · inventory scan skipped: {e}")
        return
    if not items:
        print("  · inventory empty (reconcile Lambda not run yet)")
        return
    print(f"\nInventory snapshot ({len(items)} items, max 50):")
    for it in items:
        print(
            f"  - {it.get('space_name')} | {it.get('app_type')} | "
            f"{it.get('instance_type')} | {it.get('status')} | user={it.get('owner_user_profile')}"
        )


def main() -> int:
    if not os.path.exists(CONFIG_PATH):
        print(f"config.json not found: {CONFIG_PATH}")
        return 1
    cfg = load_config()
    create_dashboard(cfg)
    print_inventory_snapshot(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
