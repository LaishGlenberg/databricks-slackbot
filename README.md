# databricks-slackbot

A deliberately tiny Slack bot that lives inside Databricks and reports which parts of the stack are working. One slash command, `/smoke`, runs a single minimal check per technology and posts pass / fail / skip.

Nothing here is a template for production. Every check is 1–3 lines so you can read it and know exactly what it proves.

## What `/smoke` tests

| Check | What it proves |
|---|---|
| Databricks Apps | the app process is running and can call the Databricks API |
| Unity Catalog | the app's identity works and it can query through a warehouse |
| Delta | it can create and write a managed Delta table |
| Iceberg | it can create and write an Iceberg table |
| Model Serving | an endpoint answers a chat completion |
| MLflow | **deferred** — needs an experiment plus `CAN_EDIT` for the app's SP |
| Lakebase | **deferred** — Free Edition needs a per-connection OAuth token, not a DSN |

The last two report **skip** with a reason, never a pass. If neither `SERVING_ENDPOINT` nor the warehouse is set, that check skips too — so the bot is usable on day one and lights up as you add pieces.

## Repository map

```
app.py           Bolt handlers, the 7 checks, health endpoint
app.yaml         command + env only (resources are NOT read from here, see below)
setup_app.py     creates the app and attaches its resources
grants.py        grants the app's service principal what it needs
requirements.txt
run_sql.py       executes .sql files against a warehouse
setup.sql        creates the smoke schema + Delta and Iceberg tables
slack/           manifest.yaml - the whole Slack app in one paste
probe/           the throwaway egress/capability probes, already run
```

**Gotcha that cost an afternoon:** a `resources:` block inside `app.yaml` is silently ignored. Resources belong to the app object, which is why `setup_app.py` exists. `env.valueFrom` in `app.yaml` then resolves against the resource *names* set there.

---

# Setup

Order matters: the app has to exist before you can grant it permissions, and the schema has to exist before the app can use it.

## 1. Install the CLI and authenticate

```bash
winget install Databricks.DatabricksCLI

databricks auth login --host https://<your-workspace>.cloud.databricks.com
databricks current-user me
```

`auth login` opens a browser and stores a profile in `~/.databrickscfg`. Verify with the last command.

## 2. Create the Slack app

At [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From an app manifest**, pick your workspace, and paste [`slack/manifest.yaml`](slack/manifest.yaml). That defines the scopes and the `/smoke` command in one go — no clicking through scope checkboxes.

**Socket Mode** is used instead of a public URL, so nothing connects *into* your app: no public endpoint, no signature secret, no workspace SSO problem.

Then two clicks inside the app:

1. **Socket Mode** → generate an app-level token with scope `connections:write`. This is `xapp-...`
2. **Install App** → copy the Bot User OAuth Token, `xoxb-...`

This creates a *separate* Slack app from Vaultnel. Same workspace, its own identity, so nothing you do here can break the existing bot.

## 3. Store both tokens

```bash
databricks secrets create-scope slack
databricks secrets put-secret slack slack-bot-token
databricks secrets put-secret slack slack-app-token
```

Each `put-secret` opens an editor — paste the `xoxb-` and `xapp-` values and save.

## 4. Create the smoke tables

```bash
python -m pip install databricks-sdk
python run_sql.py setup.sql --warehouse <warehouse-id>
```

Expect the Delta table to succeed. **The Iceberg statement may fail** — that's information, not a mistake: Iceberg isn't enabled on every workspace. Note it and move on.

These files target the **`workspace`** catalog, which is what Free Edition gives you. There is no `main` there.

Find your warehouse id with:

```bash
databricks warehouses list
```

## 5. Create the app and push the code

```bash
databricks apps create coffee-bot

databricks sync . /Workspace/Users/<you>@<company>.com/coffee-bot --watch
```

`sync` copies files from this folder into the workspace and `--watch` keeps them in sync as you edit. This is the answer to "how do I get code in there" — you never paste code into a web editor. Press `Ctrl-C` when the first sync finishes.

Now deploy:

```bash
databricks apps deploy coffee-bot \
  --source-code-path /Workspace/Users/<you>@<company>.com/coffee-bot
```

Before this step succeeds, edit `app.yaml` and replace `<warehouse-id>` and `<endpoint-name>` with real values.

## 6. Attach the secrets

Apps UI → your app → **Environment** → add:

| Variable | Source |
|---|---|
| `SLACK_BOT_TOKEN` | secret scope `slack`, key `slack-bot-token` |
| `SLACK_APP_TOKEN` | secret scope `slack`, key `slack-app-token` |

The app will fail to start until these exist — expected. Saving them restarts it.

Then check the logs for `starting Socket Mode...`:

```bash
databricks apps logs coffee-bot
```

## 7. Grant the app access

The app runs as its own service principal and starts with **zero** permissions.

```bash
python run_sql.py grants.sql --warehouse <warehouse-id> --app coffee-bot
```

The script resolves `{{APP_SP}}` to the app's actual service principal name, so there's nothing to look up by hand.

## 8. Try it

In Slack, add the bot to a channel and run `/smoke`:

```
/invite @coffee-bot
/smoke
```

## 9. Optional checks

Each of these turns a **skip** into a **pass**. Add them as env vars in the Apps UI.

Only Model Serving is wired up. It is already pointed at `databricks-meta-llama-3-3-70b-instruct`, a system endpoint the workspace exposes. Databricks does not host `deepseek-flash`, so this check uses a Llama — it proves serving works, not that it hosts your model of choice. System `databricks-*` endpoints are account-wide and need no grant.

MLflow and Lakebase are deliberately out of scope: both need provisioning and extra identity work and add no proof the other five don't already give.

---

# Notes

**Verified on Free Edition.** A notebook probe returned: Slack reachable over HTTPS and TLS to `wss-primary.slack.com`; one warehouse (`Serverless Starter Warehouse`); 11 foundation-model endpoints; `apps` and `lakebase` APIs both responding with empty lists (i.e. enabled, nothing created). Catalog is `workspace`.

**Iterating.** `databricks sync` then `databricks apps deploy` again. Roughly 30 seconds per cycle.

**Alternative delivery paths.** `sync` is the quickest. For anything long-lived, commit this repo to Git and point the app at the repository in the Apps UI, or wrap it in a Declarative Automation Bundle so the app, its permissions, and its config are all declared in one place.

**Why Socket Mode.** Databricks Apps URLs sit behind workspace authentication. Slack cannot log into your workspace, so the Events API route can bounce Slack to a login page. Socket Mode dials outward and avoids the question entirely.

**The 3-second rule.** Slack waits 3 seconds, then shows the user an error and retries. `app.py` calls `ack()` first and posts the result later via `respond()`, which stays valid for ~30 minutes. Keep that structure when adding handlers.
