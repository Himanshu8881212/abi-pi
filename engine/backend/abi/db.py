"""Postgres metadata store: users, roles, sessions, audit log, agent memory, notifications, LLM usage, secrets, sources (PRD §4).

Connection comes from the standard libpq env vars (PGHOST, PGUSER, PGPASSWORD, PGDATABASE).
"""

import importlib.metadata
import re
import time
import tomllib
from pathlib import Path

import psycopg
from procrastinate import schema as queue_schema
from psycopg.rows import dict_row

VERSION = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())["project"]["version"]
# H-03.2: bump with every change to SCHEMA (test_H03_schema_number_bumped pins SCHEMA's sha256 to it), so older code
# refuses a database newer code has upgraded instead of running on it.
SCHEMA_NUMBER = 3  # 3: ABI-PI 1.2 (flow_conditions.since)
QUEUE = importlib.metadata.version("procrastinate")
# The Procrastinate of v0.5.0-alpha, the oldest release we upgrade from (D-133); installs before H-03.2 recorded none.
QUEUE_BASELINE = "3.10.0"
STATE = """
CREATE TABLE IF NOT EXISTS schema_state (
  one boolean PRIMARY KEY DEFAULT true CHECK (one),
  app_version text NOT NULL,
  app_schema int NOT NULL,
  queue text,  -- Procrastinate version whose schema and migrations are applied; NULL = never applied by init
  updated timestamptz NOT NULL DEFAULT now()
);
"""

# ponytail: CREATE/ALTER ... IF NOT EXISTS at app start, no migration tool; add one when a change can't be written
# as an idempotent statement (a rename, a type change, a data move).
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name text NOT NULL UNIQUE,
  password_hash text NOT NULL,
  workspace_owner boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS project_roles (
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  project text NOT NULL,
  role text NOT NULL CHECK (role IN ('viewer', 'editor', 'owner')),
  PRIMARY KEY (user_id, project)
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash bytea PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  created timestamptz NOT NULL DEFAULT now()
);
-- No FK to users: the audit log outlives deleted accounts (SC-6).
CREATE TABLE IF NOT EXISTS audit_log (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at timestamptz NOT NULL DEFAULT now(),
  user_id bigint,
  user_name text,
  via text NOT NULL,
  action text NOT NULL,
  project text,
  input jsonb,
  status int NOT NULL,
  detail text
);
CREATE TABLE IF NOT EXISTS memory (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  project text NOT NULL,
  text text NOT NULL,
  created_by text NOT NULL,
  created timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE memory ADD COLUMN IF NOT EXISTS terms text[] NOT NULL DEFAULT '{}';  -- glossary links (§5 #6)
ALTER TABLE memory ADD COLUMN IF NOT EXISTS by_agent boolean NOT NULL DEFAULT true;  -- written by an LLM (H-01.4-fix-2); older rows unknown: fail closed
-- project NULL = workspace-wide
CREATE TABLE IF NOT EXISTS notifications (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at timestamptz NOT NULL DEFAULT now(),
  project text,
  kind text NOT NULL,
  message text NOT NULL
);
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS ref text;  -- the object's ID within `kind` (table, tool, flow, agent), for UI links (F-10)
-- SE-6/D-47: where data-derived text starts (restricted Viewers see the text before it); NULL = unknown, all hidden
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS detail int;
CREATE TABLE IF NOT EXISTS notification_reads (
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  notification_id bigint NOT NULL REFERENCES notifications ON DELETE CASCADE,
  PRIMARY KEY (user_id, notification_id)
);
CREATE TABLE IF NOT EXISTS llm_usage (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at timestamptz NOT NULL DEFAULT now(),
  user_id bigint,
  agent text NOT NULL,
  model text NOT NULL,
  prompt_tokens int,
  completion_tokens int
);
ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS provider text NOT NULL DEFAULT 'env';  -- AG-6: provider ID, 'env' = OPENAI_* env
ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS cost numeric;  -- AG-6: estimated USD from configured prices, NULL without one
ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS effort text;  -- D-71: the reasoning effort sent, NULL = none sent
ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS reasoning_tokens int;  -- D-71: part of completion_tokens, NULL = not reported
ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS cached_tokens int;  -- D-117: part of prompt_tokens served from the provider's cache, NULL = not reported
-- AG-6 (E-12.1): workspace LLM providers, tried in `position` order; api_key is a ${secret:NAME} reference (D-2)
CREATE TABLE IF NOT EXISTS llm_providers (
  id text PRIMARY KEY,
  position int NOT NULL,
  base_url text NOT NULL,
  api_key text,
  models jsonb NOT NULL,
  prices jsonb NOT NULL
);
ALTER TABLE llm_providers ADD COLUMN IF NOT EXISTS efforts jsonb NOT NULL DEFAULT '{}';  -- D-71: alias → {levels, default}
-- D-117: embedding vectors of definition texts (never data rows) by sha256(text) and '<provider>/<model>'; no text kept
CREATE TABLE IF NOT EXISTS embeddings (
  sha text NOT NULL,
  model text NOT NULL,
  vector real[] NOT NULL,
  PRIMARY KEY (sha, model)
);
CREATE TABLE IF NOT EXISTS llm_settings (
  one boolean PRIMARY KEY DEFAULT true CHECK (one),
  main_model text NOT NULL
);
-- OB-1: every read the semantic engine runs (S-04). user_id NULL = the system (readiness measure tests).
CREATE TABLE IF NOT EXISTS query_log (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at timestamptz NOT NULL DEFAULT now(),
  user_id bigint,
  project text NOT NULL,
  query jsonb NOT NULL,
  sql text NOT NULL,
  snapshot bigint NOT NULL,
  cached boolean NOT NULL,
  rows int,
  ms real NOT NULL,
  error text
);
-- SC-4: workspace credentials, Fernet-encrypted; referenced as ${secret:NAME}.
CREATE TABLE IF NOT EXISTS secrets (
  name text PRIMARY KEY,
  value bytea NOT NULL
);
-- D-2 (F-09): hosts a secret may be sent to; NULL = stored before F-09, bound on the first replay of its source.
ALTER TABLE secrets ADD COLUMN IF NOT EXISTS allowed_hosts text[];
-- Runtime state per loaded table (D-9): its files' fingerprint. Definitions live in the repo (sources/<id>.json).
-- spec: pre-M-10 definitions only; the worker's first refresh writes them to sources/<id>.json, then NULLs them.
CREATE TABLE IF NOT EXISTS sources (
  project text NOT NULL,
  table_name text NOT NULL,
  spec jsonb,
  fingerprint text,
  PRIMARY KEY (project, table_name)
);
ALTER TABLE sources ALTER COLUMN spec DROP NOT NULL;
-- TL-4 runtime state per published tool: the model + tool hash its tests last ran on; error NOT NULL = disabled.
CREATE TABLE IF NOT EXISTS tool_state (
  project text NOT NULL,
  tool text NOT NULL,
  stamp text NOT NULL,
  error text,
  PRIMARY KEY (project, tool)
);
-- FL-4/FL-5 (T-03.1): flow runs and step traces; run history isn't in Git (§6.6). The ID is <project>/<flow>/<trigger key>,
-- so a trigger can't start the same run twice.
CREATE TABLE IF NOT EXISTS flow_runs (
  id text PRIMARY KEY,
  project text NOT NULL,
  flow text NOT NULL,
  status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'ok', 'failed')),
  error text,
  created timestamptz NOT NULL DEFAULT now(),
  finished timestamptz
);
CREATE TABLE IF NOT EXISTS flow_steps (
  run_id text NOT NULL REFERENCES flow_runs ON DELETE CASCADE,
  pos int NOT NULL,
  step text NOT NULL,
  status text NOT NULL CHECK (status IN ('ok', 'failed')),
  input jsonb,  -- the action's validated input: SecretStr fields are masked
  output jsonb,
  error text,
  started timestamptz NOT NULL,
  finished timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (run_id, step)
);
-- FL-2/FL-4 (T-03.2): the last schedule tick per enabled scheduled flow; a later tick runs the latest fire since then, once.
CREATE TABLE IF NOT EXISTS flow_schedule (
  project text NOT NULL,
  flow text NOT NULL,
  checked timestamptz NOT NULL,
  PRIMARY KEY (project, flow)
);
-- FL-6: email domains ('email:x.com') and channels ('channel:#ops') confirmed for sending, per project (who and when: audit_log).
-- AG-3 (T-02.3): one row per custom agent run (also evaluation runs, via 'eval'); history isn't in Git (§6.6).
CREATE TABLE IF NOT EXISTS agent_runs (
  id uuid PRIMARY KEY,
  at timestamptz NOT NULL DEFAULT now(),
  project text NOT NULL,
  agent text NOT NULL,
  version int NOT NULL,
  user_id bigint,
  via text NOT NULL,
  question text NOT NULL,
  trace jsonb NOT NULL,  -- [{tool, input, output}]
  answer text NOT NULL,
  grounded boolean,  -- NULL: grounding off (in-app only) and not checked
  retried boolean NOT NULL,
  template boolean NOT NULL,
  prompt_tokens int NOT NULL,
  completion_tokens int NOT NULL
);
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS reasoning_tokens int NOT NULL DEFAULT 0;  -- D-71
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS effort text;  -- D-71: the last answer's effort, NULL = none sent
-- AG-4 runtime state per published agent: the model + tools + agent hash its evaluation set last ran on; error NOT NULL = disabled.
CREATE TABLE IF NOT EXISTS agent_state (
  project text NOT NULL,
  agent text NOT NULL,
  stamp text NOT NULL,
  error text,
  PRIMARY KEY (project, agent)
);
CREATE TABLE IF NOT EXISTS send_targets (
  project text NOT NULL,
  target text NOT NULL,
  PRIMARY KEY (project, target)
);
-- AT-1/IB-1 (T-05.1): the in-app inbox, messages posted to channel 'inbox'; runtime state, not in Git (§6.6).
CREATE TABLE IF NOT EXISTS inbox (
  id bigserial PRIMARY KEY,
  project text NOT NULL,
  at timestamptz NOT NULL DEFAULT now(),
  sender text NOT NULL,
  text text NOT NULL,
  data jsonb,  -- the attached result {columns, rows}
  snapshot text
);
ALTER TABLE inbox ADD COLUMN IF NOT EXISTS recipient bigint;  -- AT-4 (T-04.1): only this user sees it (a task test run's owner)
-- FL-1/AT-2 (D-03.1): a false condition step ends a run as 'stopped'.
-- FL-1 (D-03.3): an approval step pauses a run ('waiting') until approve_flow_run or reject_flow_run ('rejected');
-- started_by: the user whose action started the run (NULL: a trigger no one clicked, i.e. the flow owner), D-31.
ALTER TABLE flow_runs DROP CONSTRAINT IF EXISTS flow_runs_status_check;
ALTER TABLE flow_runs ADD CONSTRAINT flow_runs_status_check CHECK (status IN ('queued', 'running', 'ok', 'failed', 'stopped', 'waiting', 'rejected'));
ALTER TABLE flow_steps DROP CONSTRAINT IF EXISTS flow_steps_status_check;
ALTER TABLE flow_steps ADD CONSTRAINT flow_steps_status_check CHECK (status IN ('ok', 'failed', 'waiting', 'rejected', 'skipped'));  -- skipped: FLOW-DAG when/after
ALTER TABLE flow_runs ADD COLUMN IF NOT EXISTS started_by bigint;
-- FL-4 (D-03-fix-2): when the run was last queued (insert or approval); a run queued over a minute ago with no live
-- Procrastinate job lost its defer to a crash, and the minute tick queues it again (flows.requeue).
ALTER TABLE flow_runs ADD COLUMN IF NOT EXISTS queued timestamptz NOT NULL DEFAULT now();
-- FL-4 (D-03-fix-3): a step that began sending (email, message, also from an agent step's tools), committed before the
-- send leaves; a run resumed after a crash never runs such a step again unless it finished ok (flows.sending).
CREATE TABLE IF NOT EXISTS flow_sends (
  run_id text NOT NULL REFERENCES flow_runs ON DELETE CASCADE,
  step text NOT NULL,
  PRIMARY KEY (run_id, step)
);
-- FL-2 (D-03.2): a data-condition trigger's last value and last fire; a run starts only on false→true outside the cooldown.
CREATE TABLE IF NOT EXISTS flow_conditions (
  project text NOT NULL,
  flow text NOT NULL,
  holds boolean NOT NULL,
  fired timestamptz,
  PRIMARY KEY (project, flow)
);
-- ABI-PI 1.2: a new trigger under the flow's ID (edited, or the flow deleted and saved again) starts over at `since`
ALTER TABLE flow_conditions ADD COLUMN IF NOT EXISTS since timestamptz;
-- SE-8 (D-03.1): a user's semantic query re-checked every minute; fingerprint = sha256 of the last result, NULL = no baseline yet.
CREATE TABLE IF NOT EXISTS subscriptions (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  project text NOT NULL,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  name text NOT NULL,
  query jsonb NOT NULL,
  fingerprint text,
  error text,
  checked timestamptz
);
-- ML-4 (X-01.1): the model registry, one row per training run; `spec` is ml/<id>.json as trained, `fitted` the model
-- as text (LightGBM model_to_string or linear coefficients as JSON, never a pickle). Not in Git (§6.6).
CREATE TABLE IF NOT EXISTS ml_runs (
  id uuid PRIMARY KEY,
  project text NOT NULL,
  model text NOT NULL,
  status text NOT NULL CHECK (status IN ('queued', 'running', 'ok', 'failed')),
  user_id bigint,
  spec jsonb NOT NULL,
  created timestamptz NOT NULL DEFAULT clock_timestamp(),
  finished timestamptz,
  commit text,
  snapshot bigint,
  method text,
  metrics jsonb,
  baseline jsonb,
  better boolean,
  importance jsonb,
  excluded jsonb,
  summary text,
  fitted text,
  error text
);
ALTER TABLE ml_runs ADD COLUMN IF NOT EXISTS query jsonb;  -- ML-5 traceability (X-01.2): the exact dataset query trained on
-- ML-5 (X-01.3): a scoring run ('score') names the training run it predicted with
ALTER TABLE ml_runs ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'train' CHECK (kind IN ('train', 'score'));
ALTER TABLE ml_runs ADD COLUMN IF NOT EXISTS trained uuid;
-- ML-6 (X-01.4): `key` names the scoring after a refresh (snapshot-<n>), once per model; `profile` a training run's
-- feature and prediction distributions; `drift` a scoring run's check against them
ALTER TABLE ml_runs ADD COLUMN IF NOT EXISTS key text;
ALTER TABLE ml_runs ADD COLUMN IF NOT EXISTS profile jsonb;
ALTER TABLE ml_runs ADD COLUMN IF NOT EXISTS drift jsonb;
CREATE UNIQUE INDEX IF NOT EXISTS ml_runs_key ON ml_runs (project, model, key);
ALTER TABLE ml_runs ADD COLUMN IF NOT EXISTS groups jsonb;  -- ML-4 (E-04.1): a clustering run's per-cluster profiles
ALTER TABLE ml_runs ADD COLUMN IF NOT EXISTS candidates jsonb;  -- ML-4 (D-99): {method: held-out metrics} of each candidate trained
-- AG-2 (UI-DS.8-fix-1): the held-out sizes a summary states ({"holdout share": 0.2} or periods), so a relayed summary grounds
ALTER TABLE ml_runs ADD COLUMN IF NOT EXISTS holdout jsonb;
-- ML-4 (D-99, ML-CANDIDATES.3): the training run scoring uses until unpinned (pin_ml_run), at most one per model
ALTER TABLE ml_runs ADD COLUMN IF NOT EXISTS pinned timestamptz;
CREATE UNIQUE INDEX IF NOT EXISTS ml_runs_pinned ON ml_runs (project, model) WHERE pinned IS NOT NULL;
-- ML-4 (ML-ZOO.1, D-135): a training run's hyperparameter search, one row per trial (`params` {} = the defaults);
-- score is the mean of the candidates' metric over folds of the training part; `chosen` = the setting its candidate kept
CREATE TABLE IF NOT EXISTS ml_trials (
  run uuid NOT NULL REFERENCES ml_runs ON DELETE CASCADE,
  position integer NOT NULL,
  method text NOT NULL,
  params jsonb NOT NULL,
  metric text NOT NULL,
  score double precision,
  score_std double precision,
  seconds double precision,
  status text NOT NULL CHECK (status IN ('ok', 'failed', 'skipped')),
  chosen boolean NOT NULL,
  PRIMARY KEY (run, position)
);
-- SC-7 (X-02.1): inbound channel senders → user accounts; kind slack 'team_id:user_id', teams aadObjectId, email address.
CREATE TABLE IF NOT EXISTS channel_senders (
  kind text NOT NULL CHECK (kind IN ('slack', 'teams', 'email')),
  external_id text NOT NULL,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  PRIMARY KEY (kind, external_id)
);
-- D-32: inbound replay store (sha256 of channel + signature or message ID) and fixed-window rate-limit counters;
-- the worker's minute tick purges both after a day (flows.inbound.purge).
CREATE TABLE IF NOT EXISTS inbound_seen (
  key text PRIMARY KEY,
  at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS inbound_hits (
  key text NOT NULL,
  win timestamptz NOT NULL,
  n int NOT NULL,
  PRIMARY KEY (key, win)
);
-- AG-5 (X-02.2): a D-3 action tool call from an inbound agent run, waiting for the mapped user's in-app approval;
-- approved = the approval was confirmed and the action waits for its own confirmation (FL-6); running = claimed.
CREATE TABLE IF NOT EXISTS inbound_approvals (
  id bigserial PRIMARY KEY,
  at timestamptz NOT NULL DEFAULT now(),
  project text NOT NULL,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  run_id uuid NOT NULL,
  agent text NOT NULL,
  action text NOT NULL,
  input jsonb NOT NULL,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'running', 'done', 'rejected'))
);
-- SC-8 (X-02.3): a webhook-trigger flow's signing token, Fernet-encrypted (never in Git); rotating replaces it at once.
CREATE TABLE IF NOT EXISTS webhook_tokens (
  project text NOT NULL,
  flow text NOT NULL,
  token bytea NOT NULL,
  PRIMARY KEY (project, flow)
);
-- AG-5 (X-02.4/5): an accepted Slack or Teams message waiting for its worker job, which gets only the ID (Procrastinate
-- logs job arguments; the text and the Slack response_url stay here) and deletes the row as it starts.
CREATE TABLE IF NOT EXISTS inbound_messages (
  id bigserial PRIMARY KEY,
  project text NOT NULL,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  channel text NOT NULL,
  text text NOT NULL,
  reply_to text
);
-- RP-6 (X-05.6): view-only share links; the token is kept only as its SHA-256 (D-35), shown once at creation.
CREATE TABLE IF NOT EXISTS share_links (
  id bigserial PRIMARY KEY,
  project text NOT NULL,
  report text NOT NULL,
  page text,
  creator bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  token_hash bytea NOT NULL UNIQUE,
  created timestamptz NOT NULL DEFAULT now(),
  expires timestamptz NOT NULL,
  revoked timestamptz
);
ALTER TABLE inbound_messages ALTER COLUMN reply_to DROP NOT NULL;  -- X-02.5: Teams answers go to the agent's channel
ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS thread jsonb;  -- X-02.6: an email's {message_id, references, subject}
ALTER TABLE flow_runs ADD COLUMN IF NOT EXISTS trigger jsonb;  -- FL-2 (X-02.3): what the trigger passed, read as {{ data.trigger.<path> }}
-- SC-1 (E-01.1): OIDC providers (client_secret is a ${secret:NAME} reference), single-use sign-in flows, linked identities.
CREATE TABLE IF NOT EXISTS oidc_providers (
  id text PRIMARY KEY,
  name text NOT NULL,
  issuer text NOT NULL,
  client_id text NOT NULL,
  client_secret text NOT NULL
);
CREATE TABLE IF NOT EXISTS oidc_flows (
  state_hash bytea PRIMARY KEY,
  browser_hash bytea NOT NULL UNIQUE,
  provider text NOT NULL REFERENCES oidc_providers ON DELETE CASCADE,
  verifier text NOT NULL,
  nonce text NOT NULL,
  link_user bigint REFERENCES users ON DELETE CASCADE,
  user_id bigint REFERENCES users ON DELETE CASCADE,
  used boolean NOT NULL DEFAULT false,
  created timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS oidc_identities (
  issuer text,
  sub text,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  PRIMARY KEY (issuer, sub)
);
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS oidc_provider text;
-- IN-8 (E-10.1): one stats row per successful load of a copied table: its row count and NULL count per column
CREATE TABLE IF NOT EXISTS quality_loads (
  id bigserial PRIMARY KEY,
  project text NOT NULL,
  table_name text NOT NULL,
  loaded_at timestamptz NOT NULL DEFAULT now(),
  row_count bigint NOT NULL,
  nulls jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS quality_loads_table ON quality_loads (project, table_name, id);
-- SE-6 (E-02.3): who made each artifact (snapshot, export); while a project has RLS rules only they can open it
CREATE TABLE IF NOT EXISTS artifacts (
  project text NOT NULL,
  name text PRIMARY KEY,
  creator bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  created timestamptz NOT NULL DEFAULT now()
);
-- SC-9 (E-05.1, D-50): tenants group users and projects; NULL tenant / no project_tenants row = platform
CREATE TABLE IF NOT EXISTS tenants (id text PRIMARY KEY, name text NOT NULL);
-- SC-9 branding (E-05.4): logo checked by tenants.check_logo, logo_type from that check, color '#rrggbb'
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS logo bytea, ADD COLUMN IF NOT EXISTS logo_type text,
  ADD COLUMN IF NOT EXISTS color text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS tenant text REFERENCES tenants;
ALTER TABLE users ADD COLUMN IF NOT EXISTS tenant_admin boolean NOT NULL DEFAULT false;
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_owner_platform;
ALTER TABLE users ADD CONSTRAINT users_owner_platform CHECK (NOT workspace_owner OR tenant IS NULL);
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_admin_tenant;
ALTER TABLE users ADD CONSTRAINT users_admin_tenant CHECK (NOT tenant_admin OR tenant IS NOT NULL);
CREATE TABLE IF NOT EXISTS project_tenants (project text PRIMARY KEY, tenant text NOT NULL REFERENCES tenants);
-- A tenant account holds roles only in its own tenant's projects, whoever writes the rows. Deferred to commit (or the
-- SET CONSTRAINTS in actions.run), so writers that move several of these rows (rename, delete) may go in any order.
CREATE OR REPLACE FUNCTION abi_tenant_roles() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE uid bigint; proj text;
BEGIN
  -- ponytail: one global lock for every membership change (rare); per-user/per-project locks if they ever contend
  PERFORM pg_advisory_xact_lock(4205901);  -- the check below then reads the state racing writers committed
  IF TG_TABLE_NAME = 'users' THEN uid := NEW.id;
  ELSIF TG_TABLE_NAME = 'project_roles' THEN uid := NEW.user_id;
  ELSIF TG_OP = 'DELETE' THEN proj := OLD.project;
  ELSE proj := NEW.project;
  END IF;
  IF EXISTS (SELECT 1 FROM project_roles r JOIN users u ON u.id = r.user_id LEFT JOIN project_tenants t ON t.project = r.project
             WHERE (r.user_id = uid OR r.project = proj) AND u.tenant IS NOT NULL AND u.tenant IS DISTINCT FROM t.tenant) THEN
    RAISE EXCEPTION 'A tenant account can hold roles only in its own tenant''s projects.' USING ERRCODE = 'AB901';
  END IF;
  RETURN NULL;
END $$;
DO $$ BEGIN  -- CREATE OR REPLACE is not supported for constraint triggers
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'tenant_roles' AND tgrelid = 'project_roles'::regclass) THEN
    CREATE CONSTRAINT TRIGGER tenant_roles AFTER INSERT OR UPDATE ON project_roles
      DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION abi_tenant_roles();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'tenant_roles' AND tgrelid = 'users'::regclass) THEN
    CREATE CONSTRAINT TRIGGER tenant_roles AFTER UPDATE OF tenant ON users
      DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION abi_tenant_roles();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'tenant_roles' AND tgrelid = 'project_tenants'::regclass) THEN
    CREATE CONSTRAINT TRIGGER tenant_roles AFTER INSERT OR UPDATE OR DELETE ON project_tenants
      DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION abi_tenant_roles();
  END IF;
END $$;
-- SC-9 (E-05.2): a secret belongs to a tenant (NULL = platform); names are unique per tenant, the platform included
ALTER TABLE secrets ADD COLUMN IF NOT EXISTS tenant text REFERENCES tenants;
ALTER TABLE secrets DROP CONSTRAINT IF EXISTS secrets_pkey;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'secrets_tenant_name') THEN
    ALTER TABLE secrets ADD CONSTRAINT secrets_tenant_name UNIQUE NULLS NOT DISTINCT (tenant, name);
  END IF;
END $$;
-- SC-11 (E-07.1): the metering ledger, counts only, tenant stamped at write time (NULL = platform). Like audit_log,
-- never rewritten by rename/delete_project (git._keyed). ponytail: kept forever; roll up daily and prune if it grows.
CREATE TABLE IF NOT EXISTS usage (
  at timestamptz NOT NULL DEFAULT now(),
  tenant text,
  project text,  -- NULL: workspace scope (a Main Agent chat with no project)
  user_id bigint,
  kind text NOT NULL CHECK (kind IN ('query', 'llm', 'refresh', 'send', 'export')),
  rows_scanned bigint,
  prompt_tokens int,
  completion_tokens int,
  cost numeric
);
ALTER TABLE usage ADD COLUMN IF NOT EXISTS effort text;  -- D-71: an llm row's reasoning effort (NULL = none sent)
ALTER TABLE usage ADD COLUMN IF NOT EXISTS reasoning_tokens int;
ALTER TABLE usage ADD COLUMN IF NOT EXISTS cached_tokens int;  -- D-117
CREATE TABLE IF NOT EXISTS usage_storage (
  day date NOT NULL,
  tenant text,
  project text NOT NULL,
  lake_bytes bigint NOT NULL,
  file_bytes bigint NOT NULL,
  PRIMARY KEY (day, project)
);
-- SC-10 (E-06.2): account email, the workspace's account mail server, single-use invite/reset tokens (sha256 only)
ALTER TABLE users ADD COLUMN IF NOT EXISTS email text UNIQUE CHECK (email = lower(email));
CREATE TABLE IF NOT EXISTS account_mail (one boolean PRIMARY KEY DEFAULT true CHECK (one), smtp jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS account_tokens (
  hash bytea PRIMARY KEY,
  kind text NOT NULL CHECK (kind IN ('invite', 'reset')),
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  expires timestamptz NOT NULL
);
-- SC-10 sign-up (E-06.3): a domain belongs to at most one tenant; a sign-up token has the email, no account yet
CREATE TABLE IF NOT EXISTS tenant_domains (
  domain text PRIMARY KEY CHECK (domain = lower(domain)),
  tenant text NOT NULL REFERENCES tenants ON DELETE CASCADE
);
ALTER TABLE account_tokens ADD COLUMN IF NOT EXISTS email text, ALTER COLUMN user_id DROP NOT NULL;
ALTER TABLE account_tokens DROP CONSTRAINT IF EXISTS account_tokens_kind_check;
ALTER TABLE account_tokens ADD CONSTRAINT account_tokens_kind_check CHECK (kind IN ('invite', 'reset', 'signup')
  AND (kind = 'signup') = (user_id IS NULL) AND (kind = 'signup') = (email IS NOT NULL));
-- SC-10 TOTP (E-06.4): secrets as Fernet tokens (on / being enrolled), the last accepted step (replay), recovery codes sha256
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_secret bytea, ADD COLUMN IF NOT EXISTS totp_pending bytea,
  ADD COLUMN IF NOT EXISTS totp_step bigint;
CREATE TABLE IF NOT EXISTS recovery_codes (
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  hash bytea NOT NULL,
  PRIMARY KEY (user_id, hash)
);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS mfa_required boolean NOT NULL DEFAULT false;
-- SE-13 (E-11.1): the tenant's time zone (IANA) and display currency (ISO 4217); NULL = unset (project.json overrides)
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS time_zone text, ADD COLUMN IF NOT EXISTS currency text;
-- RP-9 (E-08.1): per-project embed signing keys (Fernet-encrypted HS256 secret), the origins that may frame them
CREATE TABLE IF NOT EXISTS embed_keys (
  kid text PRIMARY KEY,
  project text NOT NULL,
  name text NOT NULL,
  origins text[] NOT NULL,
  creator bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  key bytea NOT NULL,
  created timestamptz NOT NULL DEFAULT now(),
  revoked timestamptz
);
-- AG-6 Sign in with ChatGPT (E-12.3.1): link flows (hashed one-time state, like oidc_flows) and one link per user;
-- tokens are Fernet({access, refresh}) (SC-4), NULL once a refresh was refused (relink with the kept client_id)
CREATE TABLE IF NOT EXISTS chatgpt_flows (
  state_hash bytea PRIMARY KEY,
  browser_hash bytea NOT NULL UNIQUE,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  client_id text NOT NULL,
  verifier text NOT NULL,
  nonce text NOT NULL,
  used boolean NOT NULL DEFAULT false,
  created timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS chatgpt_links (
  user_id bigint PRIMARY KEY REFERENCES users ON DELETE CASCADE,
  client_id text NOT NULL,
  sub text NOT NULL,
  email text,
  tokens bytea,
  expires timestamptz,
  model text
);
ALTER TABLE chatgpt_links ADD COLUMN IF NOT EXISTS backup text;  -- AG-6/D-80: the plan model tried after `model`
ALTER TABLE chatgpt_links ADD COLUMN IF NOT EXISTS effort text;  -- D-71: reasoning effort, NULL = the model's default
-- API-SRC.3: OAuth connect flows of API sources (hashed one-time state, `auth` holds only ${secret:NAME} refs) and
-- each source's refresh token, Fernet-encrypted (SC-4), bound to the token endpoint's host and the API's host (D-2);
-- `id` is new per connect (the in-memory access token cache keys on it); `used` once bound to a saved source
CREATE TABLE IF NOT EXISTS api_oauth_flows (
  state_hash bytea PRIMARY KEY,
  browser_hash bytea NOT NULL UNIQUE,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  project text NOT NULL,
  source text NOT NULL,
  api_host text NOT NULL,
  auth jsonb NOT NULL,
  verifier text NOT NULL,
  used boolean NOT NULL DEFAULT false,
  created timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS api_tokens (
  project text NOT NULL,
  source text NOT NULL,
  host text NOT NULL,
  api_host text NOT NULL,
  token bytea NOT NULL,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  used boolean NOT NULL DEFAULT false,
  connected timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (project, source)
);
-- CHAT-PERSIST (D-90): a person's own Main Agent conversations (never shared); items = the UI transcript, no tokens
CREATE TABLE IF NOT EXISTS chats (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  project text,
  title text NOT NULL,  -- '' until the chat has a question
  items jsonb NOT NULL,
  created timestamptz NOT NULL DEFAULT now(),
  updated timestamptz NOT NULL DEFAULT now()
);
-- H-01.4-fix-5 (SE-6, D-47): glossary term texts a person wrote (sha256 of [term, definition]); any other text is masked
-- for restricted Viewers (an agent's may quote rows; imported or older texts: writer unknown, fail closed)
CREATE TABLE IF NOT EXISTS glossary_people (
  project text NOT NULL,
  sha text NOT NULL,
  PRIMARY KEY (project, sha)
);
-- H-03.1: one row per finished backup, written by ops/backup.sh (not in the dump it describes), read by H-03.ui
CREATE TABLE IF NOT EXISTS backups (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at timestamptz NOT NULL DEFAULT now(),
  bytes bigint NOT NULL,
  manifest_sha256 text NOT NULL,
  version text NOT NULL,
  commit text NOT NULL,
  key_apart boolean NOT NULL  -- the key file was copied to its own destination (false: there was no key file yet)
);
"""


def connect() -> psycopg.Connection:
    # ponytail: one connection per call; add psycopg_pool when request rate makes connects show up.
    return psycopg.connect(row_factory=dict_row)


class Downgrade(RuntimeError):
    """Older code started on a database newer code has upgraded (H-03.2)."""


def _v(text: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", text)[:3])


def init(queue: bool = False) -> None:
    """The app's startup step: refuse a downgrade, apply SCHEMA, and with `queue` (the worker's start) Procrastinate's
    schema on an empty database or its shipped migrations on an existing one; then record what is applied. One
    transaction under an advisory lock, so app and worker starting together never apply anything twice."""
    # DEMO-1.6-fix-1: ALTER TABLE takes ACCESS EXCLUSIVE even when IF NOT EXISTS makes it a no-op, held to commit. An
    # init waiting on a table a running transaction uses (the worker's refresh holds `sources`) would keep every earlier
    # table locked while that transaction waits on one of them from another connection: a hang Postgres can't see.
    # So an attempt gives up after a short wait, drops all its locks and tries again.
    while True:
        try:
            with connect() as conn:
                conn.execute("SET LOCAL lock_timeout = '1s'")
                conn.execute("SELECT pg_advisory_xact_lock(hashtext('abi-schema'))")
                conn.execute(STATE)
                _upgrade(conn, queue)
            return
        except psycopg.errors.LockNotAvailable:
            time.sleep(1)


def _upgrade(conn: psycopg.Connection, queue: bool) -> None:
    was = conn.execute("SELECT app_version, app_schema, queue FROM schema_state").fetchone()
    if was and (was["app_schema"] > SCHEMA_NUMBER or _v(was["queue"] or "0") > _v(QUEUE)):
        raise Downgrade(f"This database was upgraded by ABI {was['app_version']} (schema {was['app_schema']}, "
                        f"Procrastinate {was['queue'] or 'not recorded'}); this is ABI {VERSION} (schema {SCHEMA_NUMBER}, Procrastinate "
                        f"{QUEUE}). Older code can't run on newer data: start that version or a newer one, or restore "
                        "a backup taken before the upgrade.")
    conn.execute(SCHEMA)
    done = was and was["queue"]
    if queue:
        if conn.execute("SELECT to_regclass('procrastinate_jobs') AS t").fetchone()["t"] is None:
            conn.execute(queue_schema.SchemaManager.get_schema())
        else:  # procrastinate docs "Migrations": a file `<x.y.z>_<n>_<pre|post>_*.sql` ships with release x.y.z
            # ponytail: pre and post together, so an upgrade stops app and worker first (admin guide); a migration
            # that can't run in a transaction (CREATE INDEX CONCURRENTLY) needs its own autocommit step then.
            at = _v(done or QUEUE_BASELINE)
            for f in sorted(Path(queue_schema.migrations_path).glob("*.sql")):
                if at < _v(f.name.split("_")[0]) <= _v(QUEUE):
                    conn.execute(f.read_text())
        done = QUEUE
    conn.execute("INSERT INTO schema_state (app_version, app_schema, queue) VALUES (%s, %s, %s) ON CONFLICT (one) "
                 "DO UPDATE SET app_version = excluded.app_version, app_schema = excluded.app_schema, "
                 "queue = excluded.queue, updated = now() WHERE (schema_state.app_version, schema_state.app_schema, "
                 "schema_state.queue) IS DISTINCT FROM (excluded.app_version, excluded.app_schema, excluded.queue)",
                 (VERSION, SCHEMA_NUMBER, done))
