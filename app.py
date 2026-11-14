"""
Bare-minimum Slack bot that smoke-tests the Databricks stack.

One Slack command:  /smoke
It runs one tiny check per technology and posts pass / fail / skip.

Design rules:
  - every check is 1-3 meaningful lines
  - anything not configured reports SKIP, not FAIL
  - errors are sanitised before going to Slack (no storage paths, no ARNs)
"""

import os
import re
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from random import randint

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

# ---------------------------------------------------------------------------
# Config: everything comes from env vars. Missing optional vars = SKIP.
# ---------------------------------------------------------------------------
w = WorkspaceClient()

WAREHOUSE_ID = os.environ["DATABRICKS_WAREHOUSE_ID"]
CATALOG = os.environ.get("SMOKE_CATALOG", "workspace")
SCHEMA = os.environ.get("SMOKE_SCHEMA", "slackbot_smoke")
TABLE_DIR = f"{CATALOG}.{SCHEMA}"

ENDPOINT = os.environ.get("SERVING_ENDPOINT")             # optional

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"}


class Skip(Exception):
    """Raised when a check has nothing to test yet."""


def first_line(e: BaseException) -> str:
    """First non-empty line of an exception, never raises.

    Databricks errors often start with a blank line, and an empty str(e) would
    make a bare splitlines()[0] throw IndexError inside the handler - which
    kills the thread and means the user gets no reply at all.
    """
    cleaned = str(e).strip()
    return cleaned.splitlines()[0] if cleaned else type(e).__name__


def safe(detail: str) -> str:
    """Strip infrastructure detail before it reaches a chat channel."""
    detail = re.sub(r"\b(?:s3|gs|abfss|wasbs)://\S+", "<storage-path>", detail)
    detail = re.sub(r"\barn:[^\s,]+", "<arn>", detail)
    detail = re.sub(r"\b[0-9a-f]{32}\b", "<uuid>", detail)
    return detail[:180]


# ---------------------------------------------------------------------------
# One helper: run SQL, handle the async half properly.
#
# A cold warehouse routinely returns PENDING/RUNNING instead of SUCCEEDED.
# Treating that as failure is wrong - on_wait_timeout defaults to CONTINUE, so
# the statement keeps running and may commit *after* we already reported an
# error. So: submit, poll to a terminal state, only then judge it.
# ---------------------------------------------------------------------------
def q(sql_text: str, wait: str = "30s", deadline_s: int = 180) -> list[dict]:
    resp = w.statement_execution.execute_statement(
        warehouse_id=WAREHOUSE_ID,
        statement=sql_text,
        wait_timeout=wait,
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

    # DDL returns SUCCEEDED with no manifest at all.
    if not resp.result or not resp.result.data_array:
        return []

    cols = [c.name for c in resp.manifest.schema.columns]
    return [dict(zip(cols, row)) for row in resp.result.data_array]


# ---------------------------------------------------------------------------
# The checks.
# ---------------------------------------------------------------------------
def check_apps() -> str:
    me = w.current_user.me()
    return f"SP: {me.user_name}"


def check_unity_catalog() -> str:
    rows = q("SELECT current_user() AS who, current_catalog() AS cat")
    return f"{rows[0]['who']} @ {rows[0]['cat']}"


def _table_roundtrip(name: str, ddl: str, expected_format: str) -> str:
    """Create, write, read back, and confirm the on-disk format."""
    table = f"{TABLE_DIR}.{name}"
    q(ddl)
    # Column list is required: delta_check has (id, note) and a bare VALUES
    # with one value would fail on column count.
    q(f"INSERT INTO {table} (id) VALUES ({randint(1, 10**6)})")
    rows = q(f"SELECT count(*) AS n FROM {table}")
    detail = q(f"DESCRIBE DETAIL {table}")
    fmt = detail[0]["format"]
    if fmt != expected_format:
        raise RuntimeError(f"table is {fmt}, expected {expected_format}")
    return f"{rows[0]['n']} rows, format={fmt}"


def check_delta() -> str:
    return _table_roundtrip(
        "delta_check",
        f"CREATE TABLE IF NOT EXISTS {TABLE_DIR}.delta_check (id INT, note STRING)",
        "delta",
    )


def check_iceberg() -> str:
    return _table_roundtrip(
        "iceberg_check",
        f"CREATE TABLE IF NOT EXISTS {TABLE_DIR}.iceberg_check (id INT) USING ICEBERG",
        "iceberg",
    )


def _chat(prompt: str, max_tokens: int = 400,
          prior: list[ChatMessage] | None = None) -> str:
    if not ENDPOINT:
        raise Skip("set SERVING_ENDPOINT to test")
    # messages must be ChatMessage objects, not dicts
    messages = list(prior or [])
    messages.append(ChatMessage(role=ChatMessageRole.USER, content=prompt))
    resp = w.serving_endpoints.query(
        name=ENDPOINT,
        messages=messages,
        max_tokens=max_tokens,
    )
    choices = getattr(resp, "choices", None)
    return (choices[0].message.content if choices else "").strip()


# ---------------------------------------------------------------------------
# Memory: opt in, never automatic.
#
# Nothing is remembered unless the message carries a standalone !mem / !memory
# / !save token. Then the user's message and the bot's reply are both kept, and
# later asks in the same channel get those turns as context. !forget / !clear
# drops the channel's memory. In-process only - it is lost on restart, which is
# the point of keeping this basic.
# ---------------------------------------------------------------------------
MEM_FLAGS = {"!mem", "!memory", "!save"}
FORGET_FLAGS = {"!forget", "!clear"}
MEMORY: dict[str, list[dict]] = {}
MEM_TURNS = 10


def take_flag(text: str) -> tuple[str, bool, bool]:
    """Split the flags off a question -> (question, save, forget).

    Only a whole token counts, so '!memorable' inside prose does not silently
    turn memory on.
    """
    words = text.split()
    low = [wd.lower() for wd in words]
    kept = [wd for wd, lw in zip(words, low) if lw not in MEM_FLAGS | FORGET_FLAGS]
    return " ".join(kept), any(wd in MEM_FLAGS for wd in low), any(
        wd in FORGET_FLAGS for wd in low
    )


def history(channel: str) -> list[ChatMessage]:
    """Remembered exchanges for a channel, oldest first."""
    messages: list[ChatMessage] = []
    for turn in MEMORY.get(channel, []):
        messages.append(ChatMessage(role=ChatMessageRole.USER, content=turn["user"]))
        messages.append(
            ChatMessage(role=ChatMessageRole.ASSISTANT, content=turn["assistant"])
        )
    return messages


def remember(channel: str, question: str, reply: str) -> None:
    turns = MEMORY.setdefault(channel, [])
    turns.append({"user": question, "assistant": reply})
    del turns[:-MEM_TURNS]
    print(f"memory: {channel} holds {len(turns)} turn(s)", flush=True)


def reply_for(channel: str, question: str) -> str:
    """Answer one question, saving the exchange only if the flag was present.

    !forget wins over everything else: it clears and returns without asking the
    model or storing anything.
    """
    question, save, forget = take_flag(question)
    if forget:
        MEMORY.pop(channel, None)
        print(f"memory: {channel} cleared", flush=True)
        return "memory cleared"
    if not question:
        return "ask me something"
    text = answer(question, prior=history(channel))
    if save:
        remember(channel, question, text)
    return text


def check_serving() -> str:
    return f"{ENDPOINT} -> {_chat('Reply with: pong', max_tokens=10)[:40]!r}"


# Out of scope for now (agreed): MLflow needs an experiment + CAN_EDIT on the
# app SP; Lakebase on Free Edition needs a per-connection OAuth token rather
# than a DSN. Both report SKIP so they are never mistaken for passes.
MLFLOW_DEFERRED = "deferred: needs experiment + CAN_EDIT for the app SP"
LAKEBASE_DEFERRED = "deferred: Free Edition needs a per-connection token"


def check_mlflow() -> str:
    raise Skip(MLFLOW_DEFERRED)


def check_lakebase() -> str:
    raise Skip(LAKEBASE_DEFERRED)


CHECKS = [
    ("Databricks Apps", check_apps),
    ("Unity Catalog", check_unity_catalog),
    ("Delta", check_delta),
    ("Iceberg", check_iceberg),
    ("Model Serving", check_serving),
    ("MLflow", check_mlflow),
    ("Lakebase", check_lakebase),
]


def run_all() -> str:
    lines = ["*Stack check*"]
    passed = 0

    for name, fn in CHECKS:
        try:
            detail = fn()
            passed += 1
            lines.append(f":white_check_mark: *{name}* — {safe(str(detail))}")
        except Skip as e:
            lines.append(f":heavy_minus_sign: *{name}* — skipped ({safe(str(e))})")
        except Exception as e:
            lines.append(f":x: *{name}* — {type(e).__name__}: {safe(first_line(e))}")
            traceback.print_exc()

    lines.append(f"\n{passed}/{len(CHECKS)} passed")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Slack wiring
# ---------------------------------------------------------------------------
bolt = App(
    token=os.environ["SLACK_BOT_TOKEN"],
    signing_secret=os.environ.get("SLACK_SIGNING_SECRET", "unused-in-socket-mode"),
)


def answer(question: str, prior: list[ChatMessage] | None = None) -> str:
    """Ask the serving endpoint, optionally with remembered turns in front."""
    return _chat(question, prior=prior)[:3000]


def _react(client, channel: str, ts: str, name: str, *, add: bool) -> None:
    """Reactions are best-effort - never let one fail a reply."""
    try:
        if add:
            client.reactions_add(channel=channel, timestamp=ts, name=name)
        else:
            client.reactions_remove(channel=channel, timestamp=ts, name=name)
    except Exception:
        print(f"reaction {name} {'add' if add else 'remove'} failed", flush=True)


def _answer(client, say, event, question: str) -> None:
    """React to the user's message, answer in-thread, then settle the reaction."""
    channel, ts = event["channel"], event["ts"]
    _react(client, channel, ts, "eyes", add=True)

    ok = True
    try:
        reply = reply_for(channel, question)
    except Skip as e:
        reply, ok = f":warning: {e}", False
    except Exception as e:
        reply, ok = f":x: {type(e).__name__}: {safe(first_line(e))}", False
        traceback.print_exc()

    say(text=reply, thread_ts=ts)
    _react(client, channel, ts, "eyes", add=False)
    _react(client, channel, ts, "white_check_mark" if ok else "x", add=True)


@bolt.command("/dbrick")
def handle_dbrick(ack, respond, command):
    ack()  # Slack needs this within 3s
    # respond() stays valid ~30 min, so a slow warehouse query or LLM call
    # can answer after this immediate acknowledgement.
    respond(":eyes: on it…")
    question = command["text"].strip()
    channel = command["channel_id"]

    def target() -> None:
        try:
            respond(reply_for(channel, question) if question else run_all())
        except Skip as e:
            respond(f":warning: {e}")
        except Exception as e:
            respond(f":x: {type(e).__name__}: {safe(first_line(e))}")
            traceback.print_exc()

    threading.Thread(target=target, daemon=True).start()


@bolt.event("app_mention")
def handle_mention(event, say, client):
    # "<@U123> how many orders" -> strip the leading mention
    question = event["text"].split(">", 1)[-1].strip()
    threading.Thread(
        target=_answer,
        args=(client, say, event, question),
        daemon=True,
    ).start()


@bolt.event("message")
def handle_dm(event, say, client):
    # Bolt hands us every message event; only DMs from humans are ours
    if event.get("channel_type") != "im":
        return
    if event.get("subtype") or event.get("bot_id"):
        return
    threading.Thread(
        target=_answer,
        args=(client, say, event, event["text"].strip()),
        daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# Databricks Apps expects something listening on its port, or it may mark the
# app unhealthy even when the bot itself is fine.
# ---------------------------------------------------------------------------
def serve_health() -> None:
    port = int(os.environ.get("DATABRICKS_APP_PORT", "8000"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    threading.Thread(target=serve_health, daemon=True).start()
    print("starting Socket Mode...", flush=True)
    SocketModeHandler(bolt, os.environ["SLACK_APP_TOKEN"]).start()
