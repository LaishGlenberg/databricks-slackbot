#!/usr/bin/env python
"""
Run a .sql file against a Databricks SQL warehouse.

Exists so you never have to paste SQL into a web editor. Placeholders that
need real values are filled in automatically:

    {{APP_SP}}        -> the service principal name of --app
    {{WAREHOUSE_ID}}  -> the value of --warehouse

Usage:
    python run_sql.py setup.sql  --warehouse <wh-id>
    python run_sql.py grants.sql --warehouse <wh-id> --app coffee-bot

Continues past failures and exits non-zero at the end, so one unsupported
statement (e.g. Iceberg disabled) doesn't block the rest.
"""

import argparse
import re
import sys
import time

from databricks.sdk import WorkspaceClient

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"}


def run_statement(w, warehouse_id: str, statement: str, deadline_s: int = 240):
    """Submit, poll to a terminal state, return the final response.

    A cold warehouse returns PENDING/RUNNING rather than SUCCEEDED, and
    on_wait_timeout defaults to CONTINUE - so we must poll rather than assume
    a non-SUCCEEDED state means failure.
    """
    resp = w.statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=statement,
        wait_timeout="30s",
    )
    statement_id = resp.statement_id
    started = time.time()

    while resp.status.state.value not in TERMINAL:
        if time.time() - started > deadline_s:
            try:
                w.statement_execution.cancel_execution(statement_id)
            except Exception:
                pass
            raise RuntimeError(f"timed out after {deadline_s}s")
        time.sleep(1.5)
        resp = w.statement_execution.get_statement(statement_id)

    if resp.status.state.value != "SUCCEEDED":
        err = resp.status.error
        raise RuntimeError((err.message if err else resp.status.state.value) or "failed")
    return resp


def split_statements(text: str) -> list[str]:
    """Naive ; split. Fine for simple DDL, breaks on ; inside string literals."""
    out = []
    for chunk in text.split(";"):
        body = "\n".join(
            line for line in chunk.splitlines() if not line.strip().startswith("--")
        ).strip()
        if body:
            out.append(chunk.strip())
    return out


def first_line(stmt: str) -> str:
    for line in stmt.splitlines():
        if line.strip() and not line.strip().startswith("--"):
            return line.strip()[:70]
    return stmt[:70]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", help="path to .sql file")
    ap.add_argument("--warehouse", required=True, help="SQL warehouse id")
    ap.add_argument("--app", help="fill {{APP_SP}} from this app's service principal")
    ap.add_argument("--profile", help="databricks CLI profile name (default: SDK default)")
    args = ap.parse_args()

    w = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()

    subs = {"{{WAREHOUSE_ID}}": args.warehouse}
    if args.app:
        app = w.apps.get(args.app)
        print(f"app '{args.app}' runs as: {app.service_principal_name}")
        print("  (if this is empty, the app has no service principal yet)\n")
        subs["{{APP_SP}}"] = app.service_principal_name

    with open(args.file, encoding="utf-8") as fh:
        statements = split_statements(fh.read())

    failures = 0
    for stmt in statements:
        for key, value in subs.items():
            stmt = stmt.replace(key, value)

        try:
            resp = run_statement(w, args.warehouse, stmt)

            rows = resp.result.data_array if resp.result else None
            if rows:
                cols = [c.name for c in resp.manifest.schema.columns]
                print("OK   ", first_line(stmt), f"-> {cols} {rows[0]}")
            else:
                print("OK   ", first_line(stmt))
        except Exception as e:  # noqa: BLE001 - report and keep going
            failures += 1
            # Multi-line errors often start with a blank line, so don't just
            # take splitlines()[0] or the message looks empty.
            cleaned = str(e).strip()
            first = cleaned.splitlines()[0] if cleaned else type(e).__name__
            print("FAIL ", first_line(stmt), f"-> {first[:200]}")

    print(f"\n{len(statements) - failures}/{len(statements)} statements succeeded")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
