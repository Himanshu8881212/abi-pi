"""LLM providers, failover, model per agent, small/large routing, usage and cost. PRD AG-6, OB-1, SC-4, D-2.

Every LLM call (Main Agent, Documenter, custom agents) goes through `complete`. Providers live in Postgres
`llm_providers` (workspace owner actions); with none for the wanted alias, the env provider (`OPENAI_BASE_URL`,
`OPENAI_API_KEY`, `ABI_LLM_MODEL`) is used exactly as before E-12.1. A provider's key is a ${secret:NAME} reference,
resolved only for the request header and only toward a host the secret allows (D-2).
"""

import contextvars
import os
import re
import typing
import urllib.parse
from typing import Annotated, Literal

import httpx2
import openai
from openai import OpenAI
from openai.types.shared import ReasoningEffort
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, StringConstraints

from abi import actions, db
from abi.actions import ActionFailed, action, require_confirmation
from abi.project import ID  # provider IDs and model aliases use the object-ID pattern
from abi.project.git import NoInput
from abi.security import Role, User, credentials, usage

OTHER = {"small": "large", "large": "small"}
LONG = 400  # characters; a longer question goes to `large`
UNATTENDED = contextvars.ContextVar("unattended", default=False)  # D-53: an agent asked by a worker job's agent (AGENT-AS-TOOL)
# ponytail: keyword routing (build/change verbs → large); a classifier call if traces show misroutes.
LARGE = re.compile(r"\b(?:add|build|change|connect|create|delete|edit|fix|forecast|import|load|predict|publish|remove|rename"
                   r"|schedule|set|train|update|write)(?:s|es|d|ed|ing)?\b", re.IGNORECASE)


LEVELS = typing.get_args(typing.get_args(ReasoningEffort)[0])  # D-71: the SDK's reasoning-effort names
Level = Literal[LEVELS]  # type: ignore[valid-type]


class Efforts(BaseModel):
    levels: list[Level] = Field(min_length=1, description="the effort levels this model accepts")
    default: Level | None = Field(None, description="sent when no effort is picked; null: none sent (the model's own default)")


class Price(BaseModel):
    prompt: float = Field(ge=0, description="USD per 1M prompt tokens")
    completion: float = Field(ge=0, description="USD per 1M completion tokens")
    cached: float | None = Field(None, ge=0, description="USD per 1M prompt tokens served from the provider's cache (D-117);"
                                 " omitted: priced as prompt tokens")


class Provider(BaseModel):
    id: str = Field(pattern=ID)
    position: int = Field(0, ge=0, description="failover order: lower is tried first")
    base_url: str = Field(pattern=r"^https?://[^\s?#@/]+(/[^\s?#]*)?$", max_length=500,
                          description="OpenAI-compatible base URL, e.g. https://api.example.com/v1; http:// only without api_key")
    api_key: str | None = Field(None, pattern=r"^\$\{secret:[A-Za-z0-9_]+\}$", description="a ${secret:NAME} reference")
    models: dict[Annotated[str, StringConstraints(pattern=ID)], Annotated[str, StringConstraints(min_length=1, max_length=200)]] = \
        Field(min_length=1, description='alias → the provider\'s model name, e.g. {"small": "x-mini", "large": "x"}')
    prices: dict[str, Price] = Field({}, description="model name → {prompt, completion} USD per 1M tokens")
    efforts: dict[str, Efforts] = Field({}, description="alias → reasoning-effort levels its model accepts (D-71); an alias"
                                        " not listed gets no effort sent (OpenAI-compatible /models reports none)")


class ProviderId(BaseModel):
    id: str = Field(pattern=ID)


class Order(BaseModel):
    ids: list[Annotated[str, StringConstraints(pattern=ID)]] = Field(description="every provider ID, first tried first")


class ProviderAlias(BaseModel):
    id: str = Field(pattern=ID)
    alias: str = Field(pattern=ID, description="one of the provider's model aliases")


class MainModel(BaseModel):
    model: str = Field(pattern=ID, description="a provider model alias (small, large, …) or auto")


def _private(url: str) -> bool:
    """ABI-PI: a model server on the same machine or LAN (an IP literal, private or loopback) may be plain http; the key
    then never crosses the internet. Host names don't count: they can point anywhere."""
    import ipaddress  # noqa: PLC0415
    try:
        ip = ipaddress.ip_address(urllib.parse.urlsplit(url).hostname or "")
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


@action(Role.owner)
def set_llm_provider(conn, user: User, args: Provider) -> dict:
    """Adds or changes a workspace LLM provider (AG-6); agent prompts go to its base URL, its key is a ${secret:NAME} reference."""
    if args.api_key:
        if args.base_url.startswith("http://") and not _private(args.base_url):
            raise ActionFailed(400, "A provider with an api_key needs an https:// base URL (http:// only on a private or loopback address).")
        name = args.api_key[len("${secret:"):-1]
        if not conn.execute("SELECT 1 FROM secrets WHERE name = %s AND tenant IS NULL", [name]).fetchone():  # SC-9: platform only
            raise ActionFailed(400, credentials.UNKNOWN)
    if bad := [a for a, e in args.efforts.items() if a not in args.models or (e.default and e.default not in e.levels)]:
        raise ActionFailed(400, f"efforts['{bad[0]}']: the alias must be in models and its default among its levels.")
    require_confirmation(f"Agent prompts will be sent to {args.base_url}.",
                         {"base_url": args.base_url, "api_key": args.api_key, "models": args.models})
    conn.execute(
        "INSERT INTO llm_providers (id, position, base_url, api_key, models, prices, efforts) VALUES (%s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (id) DO UPDATE SET position = excluded.position, base_url = excluded.base_url, api_key = excluded.api_key,"
        " models = excluded.models, prices = excluded.prices, efforts = excluded.efforts",
        [args.id, args.position, args.base_url, args.api_key, Jsonb(args.models),
         Jsonb({k: v.model_dump() for k, v in args.prices.items()}), Jsonb({k: v.model_dump() for k, v in args.efforts.items()})],
    )
    return {}


@action(Role.owner, destructive=True)
def delete_llm_provider(conn, user: User, args: ProviderId) -> dict:
    """Removes a workspace LLM provider."""
    if not conn.execute("DELETE FROM llm_providers WHERE id = %s RETURNING id", [args.id]).fetchone():
        raise ActionFailed(404, "No such provider.")
    return {}


@action(Role.viewer)
def list_llm_providers(conn, user: User, args: NoInput) -> dict:
    """The workspace LLM providers in failover order (key references only, never values) and the Main Agent's model.
    Only workspace owners see hosts, key references and prices; others get what the model pickers need (SC-9, SC-4)."""
    cols = "*" if user.workspace_owner else "id, position, models, efforts"
    return {"providers": conn.execute(f"SELECT {cols} FROM llm_providers ORDER BY position, id").fetchall(),
            "main_model": main_model(conn)}


@action(Role.owner)
def order_llm_providers(conn, user: User, args: Order) -> dict:
    """Sets the providers' failover order (AG-6): `ids` lists every provider, the first is tried first. Their hosts
    were confirmed when they were added, so no confirmation here."""
    have = [r["id"] for r in conn.execute("SELECT id FROM llm_providers FOR UPDATE")]
    if sorted(args.ids) != sorted(have):
        raise ActionFailed(400, "ids must list every provider once: " + ", ".join(sorted(have)) + ".")
    conn.execute("UPDATE llm_providers p SET position = o.n - 1 FROM unnest(%s::text[]) WITH ORDINALITY o(id, n) WHERE p.id = o.id",
                 [args.ids])
    return {}


@action(Role.owner)
def test_llm_provider(conn, user: User, args: ProviderAlias) -> dict:
    """Sends one short test prompt to a provider's model for `alias` and says whether it answered (AG-6); usage is
    recorded as agent `test` (OB-1). An error names the provider and model only (SC-4)."""
    if not (p := conn.execute("SELECT * FROM llm_providers WHERE id = %s", [args.id]).fetchone()):
        raise ActionFailed(404, "No such provider.")
    if not (name := p["models"].get(args.alias)):
        raise ActionFailed(400, f"Provider '{args.id}' has no model alias '{args.alias}'.")
    try:
        resp = _send(p, name, {"messages": [{"role": "user", "content": "Reply with the word OK."}]})
    except openai.APIError as e:
        raise explain(e, p["id"], name) from None
    resp.effort = None
    _usage(user, resp, name, "test", p["id"], p["prices"].get(name), None, [0, 0, 0, 0])
    ok, model = _answered(resp), resp.model or name
    return {"answered": ok, "model": model, "columns": ["model", "answered"], "rows": [[model, ok]]}  # D-22/D-114: quotable


@action(Role.owner)
def set_main_agent_model(conn, user: User, args: MainModel) -> dict:
    """Sets the Main Agent's model: a provider alias (small, large, …) or auto (routing by question type)."""
    conn.execute("INSERT INTO llm_settings (main_model) VALUES (%s) ON CONFLICT (one) DO UPDATE SET main_model = excluded.main_model",
                 [args.model])
    return {}


def main_model(conn) -> str:
    row = conn.execute("SELECT main_model FROM llm_settings").fetchone()
    return row["main_model"] if row else "auto"


def route(model: str, question: str) -> str:
    """AG-6/§5: the user never picks; `auto` sends build/change requests and long questions to `large`, the rest to `small`."""
    if model != "auto":
        return model
    return "large" if len(question) > LONG or LARGE.search(question) else "small"


EMPTY = "The model returned an empty answer (no text and no tool calls). Try again or pick another model."
NO_MODEL = "No model is available: an admin connects one in Govern › Settings (a proxy or an API provider)."
NO_FLOW_MODEL = "No model is available to flows and scheduled tasks: an admin connects one in Govern › Settings."
LIMITED = ("Your ChatGPT plan's usage limit is reached (see ChatGPT Settings → Usage). Try again after it resets,"
           " or ask a workspace Owner to add an LLM provider in Settings.")
LIMITED_ALL = ("Your ChatGPT plan's usage limit is reached (see ChatGPT Settings → Usage), and the backup LLM providers failed"
               " too. Try again after it resets, or ask a workspace Owner to check the LLM providers in Settings.")


class Shown(ActionFailed):
    """An LLM failure whose text is safe to show as is (no URL, key or provider body, SC-4); a 4xx is never retried (FL-4)."""


class Declined(Shown):
    """ABI-PI: the provider's safety filter refused this request (its content policy), not a broken model or setup."""


POLICY = {"cyber_policy", "content_policy_violation", "content_filter", "safety", "moderation_blocked"}
DECLINED = "I can't help with that: the request was declined under the model provider's content policy."


class NoModel(Shown):
    """Nothing to try (LLM-2-fix-13): a 4xx with what to do, shown as is and never retried."""


def explain(e: Exception, provider: str, model: str) -> Exception:
    """LLM-2.7-fix-1: a provider's API error as what went wrong, naming the provider ID and model only; a missing or
    refused model is a 4xx (retrying won't help), anything else a 502 that FL-4 retries. Other errors stay as they are."""
    if isinstance(e, openai.APIConnectionError):  # incl. timeouts
        return Shown(502, f"Could not reach LLM provider '{provider}'" + (" (timed out)." if isinstance(e, openai.APITimeoutError) else "."))
    if not isinstance(e, openai.APIStatusError):
        return e
    code = e.status_code
    if code == 404:
        return Shown(400, f"LLM provider '{provider}' has no model '{model}' (HTTP 404).")
    if code in (400, 422) and str((e.body or {}).get("code") if isinstance(e.body, dict) else "") in POLICY:
        return Declined(400, DECLINED)  # the request, not the model: an agent run answers it as a refusal
    if code in (400, 422):
        return Shown(400, f"LLM provider '{provider}' refused model '{model}' (HTTP {code}): it may not support chat or tool calls.")
    return Shown(400 if code < 500 and code != 429 else 502, f"LLM provider '{provider}' answered HTTP {code} for model '{model}'.")


PLAN_STREAM = {"response.failed": "the response failed", "response.incomplete": "the answer was cut off",
               "error": "the stream reported an error", "stream ended without response.completed": "the stream ended early"}


def explain_plan(e: Exception, slug: str) -> Shown:
    """LLM-2-fix-22: a ChatGPT plan failure as ABI's own words, naming the plan model only (never a host, token, email
    or the provider's body, SC-4); a 4xx status other than 429 is not retried (FL-4), anything else is a 502."""
    code = e.status_code if isinstance(e, openai.APIStatusError) else 502
    if isinstance(e, openai.APIConnectionError | httpx2.TransportError):  # incl. timeouts
        what = "ChatGPT could not be reached" + (" (timed out)" if isinstance(e, openai.APITimeoutError | httpx2.TimeoutException) else "")
    elif isinstance(e, openai.APIStatusError):
        what = f"ChatGPT answered HTTP {code}"
    else:
        what = PLAN_STREAM.get(str(e), "the call failed")  # only the key is matched: the shown text is always ours
    return Shown(400 if code < 500 and code != 429 else 502, f"ChatGPT ({slug}) could not answer: {what}. Try again; if it keeps"
                 " failing, pick another plan model or ask a workspace Owner to add an LLM provider in Settings.")


def complete(user: User, agent: str, model: str, question: str, *, project: str | None = None, effort: str | None = None,
             plan: str | None = None, workspace: bool = False, **kw):
    """One chat completion for `agent` ('main' or a custom agent ID) with `model` (alias or auto), failing over across
    providers in order on any API error, D-2 refusal or empty completion (no text, no tool calls; LLM-2-fix-6). Raises
    the last error when every provider fails; returns the last empty completion when none answered and one was empty
    (callers then report EMPTY). The response's `.spent` is (prompt, completion, reasoning, cached) tokens summed over every
    attempt of this call, empty ones included (OB-1). `project`: the project the agent works in, for SC-11 metering (None: workspace scope).
    A user who linked ChatGPT and picked a model gets it (then their backup plan model, D-80) first, outside flows
    (D-53) or worker jobs; any failure there falls over to the providers below. `plan`: this chat's plan model instead of the picked one;
    `workspace`: this chat picked a workspace alias, so no plan model (MODEL-PICKER).
    `effort` (an agent's or this chat's, D-71) is sent where the model lists it, else the user's ChatGPT effort (plan
    models) or the alias's default (providers); a model that lists none gets none. The response carries `.effort`."""
    from abi import flows  # not at top: both import the agent package
    from abi.agent import chatgpt

    empty = err = None
    limited = False  # any plan attempt hit its usage limit: the person hears that, not a backup's error (LLM-2-fix-20)
    spent = [0, 0, 0, 0]  # OB-1 (LLM-2-fix-14): prompt, completion, reasoning, cached tokens of every attempt of this call

    def done(resp):
        resp.spent = tuple(spent)
        return resp

    def failed(provider: str, status: int, detail: str, **more) -> None:
        with db.connect() as conn:  # SC-6
            actions.audit(conn, user, actions.VIA.get(), "llm", None, {"provider": provider, **more}, status, detail)

    unattended = flows.IN_FLOW.get() or actions.VIA.get() == "worker" or UNATTENDED.get()  # D-53: flows and worker jobs (AG-4 retest)
    slugs, mine = ([], None) if unattended or workspace else chatgpt.models(user, plan)
    for slug in slugs:
        try:
            resp = chatgpt.complete(user, kw, slug, (effort, mine))
        except chatgpt.UsageLimit as e:  # LLM-2-fix-18: SC-6, then the next model or the workspace providers
            failed("chatgpt", 429, "usage_limit", model=slug)
            err, limited = e, True
            continue
        except Exception as e:  # noqa: BLE001 - SC-6, then the next model or the workspace providers
            failed("chatgpt", 502, type(e).__name__, model=slug)
            err = explain_plan(e, slug)
            continue
        if resp is None:  # no access token (refresh refused): the workspace providers
            break
        _usage(user, resp, resp.model, agent, "chatgpt", None, project, spent)  # plan usage: no per-token cost
        if _answered(resp):
            return done(resp)
        empty = resp
        failed("chatgpt", 502, "empty", model=slug)
    alias = route(model, question)
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM llm_providers ORDER BY position, id").fetchall()
    tries = []
    for a in (alias, OTHER.get(alias)):
        if tries := [(p, p["models"][a], pick(effort, p["efforts"].get(a) or {})) for p in rows if a in p["models"]]:
            break
    if not tries and not os.environ.get("OPENAI_API_KEY"):  # no env provider to try (LLM-2-fix-18)
        if empty:
            return done(empty)
        if limited:
            raise NoModel(429, LIMITED)
        raise err or NoModel(400, NO_FLOW_MODEL if unattended else NO_MODEL)
    if not tries:
        name = os.environ.get("ABI_LLM_MODEL", "")
        try:
            resp = OpenAI().chat.completions.create(model=name, **kw)
        except openai.OpenAIError as e:
            failed("env", 502, type(e).__name__)  # SC-6 (LLM-2.7-fix-2)
            if empty:
                return done(empty)
            if limited:
                raise NoModel(429, LIMITED_ALL) from None
            raise explain(e, "env", name) from None
        resp.effort = None  # the env provider declares no levels: never an effort (D-71)
        _usage(user, resp, name, agent, "env", None, project, spent)
        return done(resp)
    for p, name, sent in tries:
        try:
            resp = _send(p, name, kw | ({"reasoning_effort": sent} if sent else {}))
        except ActionFailed as e:  # D-2 refusal (or a missing secret): SC-6, then the next provider
            failed(p["id"], e.status, str(e))
            err = e
        except openai.APIError as e:  # connection, timeout or HTTP status: SC-6, then the next provider
            failed(p["id"], 502, type(e).__name__)
            err = explain(e, p["id"], name)
        else:
            resp.effort = sent
            _usage(user, resp, name, agent, p["id"], p["prices"].get(name), project, spent)
            if _answered(resp):
                return done(resp)
            empty = resp
            failed(p["id"], 502, "empty")
    if empty:
        return done(empty)
    if limited:
        raise NoModel(429, LIMITED_ALL)
    raise err


def pick(effort: str | None, declared: dict) -> str | None:
    """The effort a provider alias gets: the wanted one if declared, else its default; none for an undeclared alias."""
    return effort if effort in declared.get("levels", []) else declared.get("default")


def unlisted(effort: str | None, model: str, ok: list[str]) -> None:
    """400 unless `effort` is none or one of `model`'s levels `ok` (D-71)."""
    if effort and effort not in ok:
        raise ActionFailed(400, f"'{effort}' is not an effort of {model}: " + (", ".join(ok) if ok else "it has no effort control") + ".")


def check(user: User, model: str, question: str, plan: str | None, effort: str | None, alias: str | None = None) -> None:
    """A person's per-chat `plan` model or workspace `alias`, and `effort`: checked against the model the chat goes to
    first, `plan` or their ChatGPT plan model, else `alias` or the workspace providers' routed alias (levels declared
    by any provider holding it)."""
    from abi.agent import chatgpt

    if plan and alias:
        raise ActionFailed(400, "Pick a ChatGPT model or a workspace model, not both.")
    slugs, _ = ([], None) if alias else chatgpt.models(user, plan)
    if plan and not slugs:
        raise ActionFailed(400, "Link your ChatGPT account first.")
    if slugs:
        offered = chatgpt.offered(user)
        if slugs[0] not in offered:
            raise ActionFailed(400, f"'{slugs[0]}' is not one of your ChatGPT plan's models.")
        unlisted(effort, slugs[0], offered[slugs[0]]["efforts"])
        return
    picked, alias = alias, route(alias or model, question)
    with db.connect() as conn:
        rows = conn.execute("SELECT models, efforts FROM llm_providers").fetchall()
    if picked and not any(picked in r["models"] for r in rows):
        raise ActionFailed(400, f"'{picked}' is not a workspace model.")
    if not any(alias in r["models"] for r in rows):
        alias = OTHER.get(alias, alias)
    unlisted(effort, alias, [lv for lv in LEVELS if any(lv in (r["efforts"].get(alias) or {}).get("levels", []) for r in rows)])


def _answered(resp) -> bool:
    msg = resp.choices[0].message
    return bool(msg.content or msg.tool_calls)


def _send(p: dict, name: str, kw: dict, embed: bool = False):
    from abi.tools import builtin  # not at top: builtin imports the agent package

    used: dict = {}
    with db.connect() as conn:
        key = credentials.resolve(conn, p["api_key"], used, None) if p["api_key"] else "none"
        credentials.allow(used, urllib.parse.urlsplit(p["base_url"]).hostname)
    # SC-4/D-27: no proxy from the environment, no redirect carries the key elsewhere; failover is the retry
    with httpx2.Client(verify=builtin._tls(), trust_env=False, follow_redirects=False) as h:
        client = OpenAI(api_key=key, base_url=p["base_url"], max_retries=0, http_client=h)
        return (client.embeddings if embed else client.chat.completions).create(model=name, **kw)  # D-117: /v1/embeddings


def reasoning(u) -> int | None:
    return getattr(getattr(u, "completion_tokens_details", None), "reasoning_tokens", None)


def cached(u) -> int | None:
    """D-117: the prompt tokens the provider served from its cache (Chat Completions `prompt_tokens_details`; the plan's
    Responses usage is mapped there by `chatgpt._completion`), None when not reported."""
    return getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", None)


def _usage(user: User, resp, model: str, agent: str, provider: str, price: dict | None, project: str | None, spent: list) -> None:
    u = resp.usage
    pt, ct, rt, kt = u and u.prompt_tokens, getattr(u, "completion_tokens", None), reasoning(u), cached(u)  # embeddings: no completion
    spent[:] = [spent[0] + (pt or 0), spent[1] + (ct or 0), spent[2] + (rt or 0), spent[3] + (kt or 0)]
    hit = (kt or 0) if price and price.get("cached") is not None else 0  # cached tokens at their own rate, if one is set
    cost = None if not (price and u) else (((pt or 0) - hit) * price["prompt"] + hit * (price.get("cached") or 0)
                                           + (ct or 0) * price["completion"]) / 1e6
    with db.connect() as conn:  # OB-1: per agent ('main' or a custom agent's ID) and provider (AG-6)
        conn.execute(
            "INSERT INTO llm_usage (user_id, agent, model, prompt_tokens, completion_tokens, provider, cost, effort, reasoning_tokens,"
            " cached_tokens) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [user.id, agent, resp.model or model, pt, ct, provider, cost, resp.effort, rt, kt])
        usage.record(conn, "llm", project, user.id, prompt_tokens=pt, completion_tokens=ct, cost=cost,  # SC-11
                     effort=resp.effort, reasoning_tokens=rt, cached_tokens=kt)
