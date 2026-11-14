#!/usr/bin/env python
"""
Create (or update) the Databricks App with the resources /smoke needs.

Why this exists: `resources:` inside app.yaml is ignored. Resources are a
property of the app object, and `env.valueFrom` in app.yaml only resolves
against resources attached here. Getting this wrong fails at build time with:

    error resolving resource warehouse for env DATABRICKS_WAREHOUSE_ID:
    resource warehouse not found

Usage:
    python setup_app.py --app coffee-bot --warehouse fda8a728bc5bd1c1
"""

import argparse
import sys
import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.apps import App

DEFAULT_ENDPOINT = "databricks-meta-llama-3-3-70b-instruct"
SECRET_SCOPE = "slack"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="coffee-bot")
    ap.add_argument("--warehouse", required=True)
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--secret-scope", default=SECRET_SCOPE)
    ap.add_argument("--profile")
    args = ap.parse_args()

    w = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()

    resources = [
        {
            "name": "warehouse",
            "sql_warehouse": {"id": args.warehouse, "permission": "CAN_USE"},
        },
        {
            "name": "endpoint",
            "serving_endpoint": {"name": args.endpoint, "permission": "CAN_QUERY"},
        },
        {
            "name": "slack-bot-token",
            "secret": {
                "scope": args.secret_scope,
                "key": "slack-bot-token",
                "permission": "READ",
            },
        },
        {
            "name": "slack-app-token",
            "secret": {
                "scope": args.secret_scope,
                "key": "slack-app-token",
                "permission": "READ",
            },
        },
    ]

    try:
        w.apps.get(args.app)
        exists = True
    except Exception:
        exists = False

    if not exists:
        # create_and_wait() blocks for up to 20 minutes with no output, which is
        # indistinguishable from a hang. Create without waiting, then poll and
        # print so progress is visible.
        print(f"creating app {args.app} ...", flush=True)
        w.apps.create(App(name=args.app))
        for _ in range(120):
            app = w.apps.get(args.app)
            state = app.compute_status.state.value if app.compute_status else "?"
            print(f"  compute: {state}", flush=True)
            if state == "ACTIVE":
                break
            time.sleep(5)
    else:
        print(f"app {args.app} already exists", flush=True)

    body = {"name": args.app, "resources": resources}
    print(f"attaching {len(resources)} resources ...", flush=True)
    try:
        w.api_client.do("PATCH", f"/api/2.0/apps/{args.app}", body=body)
    except Exception as e:
        print(f"  ERR attaching resources: {str(e).strip().splitlines()[0][:200]}")
        return 1

    app = w.apps.get(args.app)
    print(f"\napp: {args.app}")
    print(f"  url           : {app.url}")
    print(f"  service principal : {app.service_principal_name}")
    print(f"  application id    : {app.service_principal_client_id}")
    print("  resources:")
    for r in app.resources or []:
        kind = next(
            (k for k in ("sql_warehouse", "serving_endpoint", "secret", "postgres") if getattr(r, k, None)),
            "?",
        )
        print(f"    - {r.name} ({kind})")
    print("\nnext: deploy, then run grants.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
