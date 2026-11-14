# databricks-slackbot

A deliberately tiny Slack bot that runs on Databricks. It answers questions through a Databricks serving endpoint, and can report which parts of the stack are actually working.

All commands live in **[RUNBOOK.md](RUNBOOK.md)** — deploy, grants, logs, restart, troubleshooting.

## What it does

| Trigger | Behaviour |
|---|---|
| `/dbrick <question>` | replies `:eyes: on it…` immediately, then the model's answer |
| `/dbrick` (no text) | runs the stack checks below |
| `@dbrick-bot <question>` | 👀 reaction on your message → answer in thread → 👀 replaced with ✅ / ❌ |
| DM | same as a mention |

Slash commands cannot get a reaction — a slash command is not a message, so there is no `ts` to react to. That is why the 👀 lives on mentions.

## What the checks prove

| Check | What it proves |
|---|---|
| Databricks Apps | the app process is running and can call the Databricks API |
| Unity Catalog | the app's own identity works and it can query through a warehouse |
| Delta | it can write a managed Delta table and the format is really `delta` |
| Iceberg | same, with `format=iceberg` |
| Model Serving | an endpoint answers a chat completion |
| MLflow | **deferred** — needs an experiment plus `CAN_EDIT` for the app's SP |
| Lakebase | **deferred** — Free Edition needs a per-connection OAuth token, not a DSN |

The last two report **skip** with a reason, never a pass.

## Repository map

```
app.py           Bolt handlers, the 7 checks, health endpoint
app.yaml         command + env only (resources are NOT read from here)
setup_app.py     creates the app and attaches its resources
grants.py        UC grants by SP id + warehouse ACL via the permissions API
run_sql.py       executes .sql files against a warehouse
setup.sql        creates the smoke schema + Delta and Iceberg tables
slack/           manifest.yaml - the whole Slack app in one paste
probe/           the throwaway egress/capability probes, already run
RUNBOOK.md       every command you need
```

## How it fits together

```
Slack ──socket mode──▶ Databricks App (coffee-bot, own service principal)
                            ├──▶ SQL warehouse ──▶ Delta / Iceberg tables
                            └──▶ Model Serving ──▶ databricks-meta-llama-3-3-70b-instruct
```

Credentials come from two places, both resolved by the platform: the app's own service principal for Databricks APIs, and two secrets in scope `slack` for Slack.

---

# Notes

**Free Edition.** Verified against the live workspace: Slack reachable over HTTPS and TLS from both a notebook and the app; one SQL warehouse; 11 foundation-model endpoints; `apps` and `lakebase` APIs enabled. **No `main` catalog** — this workspace uses `workspace`.

**Apps auto-stop 24h** after start or redeploy. The bot goes quiet with no error; `databricks apps start coffee-bot --profile free` revives it. Not an always-on service.

**Cost is $0.** Everything here fits inside Free Edition quotas.

**Traps.** `resources:` in `app.yaml` is silently ignored, `databricks sync` reports success on directories that do not exist, and warehouse grants are ACLs rather than SQL. Each one is documented with its verification step in [RUNBOOK.md](RUNBOOK.md#11-traps-that-wasted-time).
