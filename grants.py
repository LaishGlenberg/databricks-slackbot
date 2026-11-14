#!/usr/bin/env python
"""
Grant an app's service principal what /smoke needs.

Two different mechanisms, which is why this isn't a .sql file:

  * Unity Catalog privileges (catalog, schema, tables) -> real SQL GRANT,
    and the principal must be the SP's **application ID**, not its display name.
  * SQL warehouse access -> an ACL, not a UC privilege. There is no SQL syntax
    for it (`GRANT USE WAREHOUSE ON WAREHOUSE x` is a parse error), so it goes
    through the permissions API.
  * Serving endpoints -> system-provided `databricks-*` endpoints are
    account-wide and need no grant. Only endpoints you create yourself do.

Usage:
    python grants.py --app coffee-bot --warehouse fda8a728bc5bd1c1
"""

import argparse
import sys

from databricks.sdk import WorkspaceClient

from run_sql import run_statement


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True, help="app name, e.g. coffee-bot")
    ap.add_argument("--warehouse", required=True, help="SQL warehouse id")
    ap.add_argument("--catalog", default="workspace")
    ap.add_argument("--schema", default="slackbot_smoke")
    ap.add_argument("--endpoint", help="only for endpoints you created yourself")
    ap.add_argument("--profile")
    args = ap.parse_args()

    w = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()

    app = w.apps.get(args.app)
    sp = app.service_principal_client_id
    print(f"{args.app}")
    print(f"  display name : {app.service_principal_name}")
    print(f"  application id: {sp}\n")

    failures = 0

    print("== Unity Catalog (SQL GRANT, principal = application id) ==")
    for stmt in (
        f"GRANT USE CATALOG ON CATALOG `{args.catalog}` TO `{sp}`",
        f"GRANT ALL PRIVILEGES ON SCHEMA `{args.catalog}`.`{args.schema}` TO `{sp}`",
    ):
        try:
            run_statement(w, args.warehouse, stmt)
            print(f"  OK  {stmt[:80]}")
        except Exception as e:
            failures += 1
            print(f"  ERR {stmt[:80]}\n      {str(e).strip().splitlines()[0][:160] if str(e).strip() else type(e).__name__}")

    print("\n== SQL warehouse (permissions API) ==")
    try:
        w.api_client.do(
            "PATCH",
            f"/api/2.0/permissions/sql/warehouses/{args.warehouse}",
            body={
                "access_control_list": [
                    {"service_principal_name": sp, "permission_level": "CAN_USE"}
                ]
            },
        )
        print(f"  OK  CAN_USE on warehouse {args.warehouse}")
    except Exception as e:
        failures += 1
        print(f"  ERR {str(e).strip().splitlines()[0][:160]}")

    if args.endpoint:
        print("\n== Serving endpoint (permissions API) ==")
        try:
            w.api_client.do(
                "PATCH",
                f"/api/2.0/permissions/serving-endpoints/{args.endpoint}",
                body={
                    "access_control_list": [
                        {"service_principal_name": sp, "permission_level": "CAN_QUERY"}
                    ]
                },
            )
            print(f"  OK  CAN_QUERY on {args.endpoint}")
        except Exception as e:
            # System-provided databricks-* endpoints need no grant, so this is
            # informational unless it's an endpoint you created.
            print(f"  --  not settable: {str(e).strip().splitlines()[0][:140]}")
    else:
        print("\n== Serving endpoint: skipped (system endpoints need no grant) ==")

    print(f"\n{'all grants applied' if not failures else f'{failures} failure(s)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
