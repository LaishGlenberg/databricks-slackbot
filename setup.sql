-- Smoke-test objects. Safe to re-run.

CREATE SCHEMA IF NOT EXISTS workspace.slackbot_smoke
  COMMENT 'Scratch space for the Databricks Slack smoke bot';

-- Delta: a normal managed table (Delta is the default format)
CREATE TABLE IF NOT EXISTS workspace.slackbot_smoke.delta_check (id INT, note STRING);

-- Iceberg: explicit format. Fails loudly if Iceberg isn't enabled in the workspace.
CREATE TABLE IF NOT EXISTS workspace.slackbot_smoke.iceberg_check (id INT) USING ICEBERG;
