# Databricks notebook source
# Paste this whole file into a notebook cell and run it on serverless compute.
# It prints a capability report. Nothing is created, nothing is billed beyond
# the few seconds the notebook runs.

import socket
import ssl

print("=== 1. Can this account reach Slack? ===")

# Plain HTTPS to Slack's API
try:
    import requests

    r = requests.get("https://slack.com/api/api.test", timeout=10)
    print(f"  https slack.com/api   : {r.status_code} {r.text[:60]}")
except Exception as e:
    print(f"  https slack.com/api   : BLOCKED -> {type(e).__name__}: {e}")

# Raw TLS to the Socket Mode endpoint. This is the one that actually matters:
# Socket Mode needs an outbound websocket to this host.
try:
    ctx = ssl.create_default_context()
    with socket.create_connection(("wss-primary.slack.com", 443), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname="wss-primary.slack.com") as tls:
            print(f"  tls  wss-primary.slack: OK ({tls.version()})")
except Exception as e:
    print(f"  tls  wss-primary.slack: BLOCKED -> {type(e).__name__}: {e}")

print("\n=== 2. What does this workspace actually give me? ===")

from databricks.sdk import WorkspaceClient

w = WorkspaceClient()

probes = [
    ("warehouses", lambda: [x.name for x in w.warehouses.list()]),
    ("catalogs", lambda: [x.name for x in w.catalogs.list()][:6]),
    ("serving endpoints", lambda: [x.name for x in w.serving_endpoints.list()]),
    ("apps", lambda: [x.name for x in w.apps.list()]),
    ("lakebase instances", lambda: [x.name for x in w.database.list_database_instances()]),
]

for label, fn in probes:
    try:
        print(f"  {label:20}: {fn()}")
    except Exception as e:
        first = str(e).splitlines()[0][:140]
        print(f"  {label:20}: ERROR -> {first}")
