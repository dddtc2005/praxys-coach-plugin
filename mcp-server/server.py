#!/usr/bin/env python3
"""Praxys MCP Server — dual-mode (local/remote) training data tools.

Mode detection (in priority order):
  - PRAXYS_LOCAL=1 (or legacy TRAINSIGHT_LOCAL=1) → local mode (direct Python
    imports, dev user, DB). Use this when iterating against your local DB
    instead of production.
  - Otherwise → remote mode (HTTP API with JWT auth). PRAXYS_URL overrides
    the default of https://api.praxys.run; PRAXYS_FRONTEND_URL likewise
    for the browser-login flow.

Production is the default so the plugin works out-of-the-box for end users.
Set PRAXYS_LOCAL=1 in your shell to develop against the local FastAPI/DB.
"""
import importlib
import importlib.util
import json
import os
import sys
import logging
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Add project root to path for local mode imports
_MCP_SERVER_DIR = os.path.abspath(os.path.dirname(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, _MCP_SERVER_DIR)

from mcp.server.fastmcp import FastMCP

_AUTH_MODULE_NAME = "praxys_mcp_auth"
_AUTH_MODULE_PATH = os.path.join(_MCP_SERVER_DIR, "auth.py")
_auth = sys.modules.get(_AUTH_MODULE_NAME)
if _auth is None:
    _auth_spec = importlib.util.spec_from_file_location(
        _AUTH_MODULE_NAME,
        _AUTH_MODULE_PATH,
    )
    if _auth_spec is None or _auth_spec.loader is None:
        raise ImportError(f"Cannot load {_AUTH_MODULE_PATH}")
    _auth = importlib.util.module_from_spec(_auth_spec)
    sys.modules[_AUTH_MODULE_NAME] = _auth
    _auth_spec.loader.exec_module(_auth)

get_auth_scope = _auth.get_auth_scope
get_token = _auth.get_token
get_context_token = _auth.get_context_token
get_pending_context_access = _auth.get_pending_context_access
clear_auth_scope = _auth.logout
clear_context_token = _auth.clear_context_token
clear_pending_context_access = _auth.clear_pending_context_access
save_config = _auth.save_config
save_token = _auth.save_token
save_context_token = _auth.save_context_token
save_pending_context_access = _auth.save_pending_context_access

mcp = FastMCP("praxys", instructions="Training data tools for Praxys dashboard")

# Mode detection — prefer PRAXYS_*, fall back to legacy TRAINSIGHT_* for one
# release (deprecation: 2026-05-19). Defaults to praxys.run production so the
# plugin works out-of-the-box; local devs opt in via PRAXYS_LOCAL=1 (see the
# module docstring and CLAUDE.md "Running" section).
_DEFAULT_BACKEND = "https://api.praxys.run"
_DEFAULT_FRONTEND = "https://www.praxys.run"


def _clean(value: str | None) -> str:
    """Trim and reject unexpanded ${VAR} placeholders that slip through MCP env."""
    if not value:
        return ""
    v = value.strip().rstrip("/")
    return "" if v.startswith("${") and v.endswith("}") else v


def _is_truthy(value: str | None) -> bool:
    return bool(value) and value.strip().lower() not in ("", "0", "false", "no")


# Local mode is opt-in. Without this flag we hit production by default, so
# end users get a working plugin without any env wiring. The previous
# heuristic ("no PRAXYS_URL means local") silently broke as soon as we
# baked in production defaults — it would have routed unconfigured users'
# tool calls to localhost and surfaced obscure connection errors.
IS_REMOTE = not (
    _is_truthy(os.environ.get("PRAXYS_LOCAL"))
    or _is_truthy(os.environ.get("TRAINSIGHT_LOCAL"))
)
REMOTE_URL = (
    _clean(os.environ.get("PRAXYS_URL"))
    or _clean(os.environ.get("TRAINSIGHT_URL"))
    or (_DEFAULT_BACKEND if IS_REMOTE else "")
)
FRONTEND_URL = (
    _clean(os.environ.get("PRAXYS_FRONTEND_URL"))
    or _clean(os.environ.get("TRAINSIGHT_FRONTEND_URL"))
    or (_DEFAULT_FRONTEND if IS_REMOTE else "http://localhost:5173")
)

_LOCAL_PRELOAD_MODULES = (
    "api.deps",
    "api.routes.plan",
    "api.routes.settings",
)


def _preload_local_modules() -> None:
    """Load Pandas-backed host modules before FastMCP starts its event loop."""
    if IS_REMOTE:
        return
    for module_name in _LOCAL_PRELOAD_MODULES:
        importlib.import_module(module_name)


# ---------------------------------------------------------------------------
# Remote helpers (HTTP API)
# ---------------------------------------------------------------------------

def _not_authenticated_message() -> str:
    """Return a profile-specific authentication hint."""
    scope = get_auth_scope()
    return (
        "Not authenticated. Run the `login` tool for Praxys profile "
        f"'{scope.profile}', or cache its token at {scope.token_path}"
    )


def _api_error_message(status_code: int, detail=None) -> str:
    suffix = f": {detail}" if detail else ""
    return f"API request failed (HTTP {status_code}){suffix}"


def _get_remote_headers():
    """Get auth headers for remote API calls."""
    token = get_token()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def _check_auth_error(res):
    """Raise on HTTP errors, surfacing the API's `detail` field when present.

    Without this, requests.HTTPError reports only "400 Client Error: Bad Request"
    and the API's structured 4xx detail (e.g., the sync_interval validation
    message) is dropped, leaving the LLM caller with no actionable info.
    """
    if res.status_code == 401:
        raise RuntimeError(_not_authenticated_message())
    if res.status_code >= 400:
        detail = ""
        try:
            body = res.json()
            if isinstance(body, dict):
                detail = body.get("detail") or body.get("message") or ""
        except ValueError:
            pass
        raise RuntimeError(_api_error_message(res.status_code, detail))


def _remote_get(path: str) -> dict:
    import requests
    res = requests.get(f"{REMOTE_URL}{path}", headers=_get_remote_headers(), timeout=30)
    _check_auth_error(res)
    return res.json()


def _remote_post(path: str, data: dict = None) -> dict:
    import requests
    headers = _get_remote_headers()
    headers["Content-Type"] = "application/json"
    res = requests.post(f"{REMOTE_URL}{path}", headers=headers, json=data, timeout=60)
    _check_auth_error(res)
    return res.json()


def _remote_put(path: str, data: dict = None) -> dict:
    import requests
    headers = _get_remote_headers()
    headers["Content-Type"] = "application/json"
    res = requests.put(f"{REMOTE_URL}{path}", headers=headers, json=data, timeout=30)
    _check_auth_error(res)
    return res.json()


def _remote_delete(path: str) -> dict:
    import requests
    res = requests.delete(f"{REMOTE_URL}{path}", headers=_get_remote_headers(), timeout=30)
    _check_auth_error(res)
    return res.json()


def _public_post(path: str, data: dict | None = None) -> dict:
    """POST without a bearer for one-time opaque handoff operations."""
    import requests

    res = requests.post(
        f"{REMOTE_URL}{path}",
        headers={"Content-Type": "application/json"},
        json=data,
        timeout=30,
    )
    _check_auth_error(res)
    return res.json() if res.content else {}


def _context_remote_headers() -> dict:
    """Return the separately cached short-lived context capability."""
    token = get_context_token()
    if token is None:
        raise RuntimeError(
            "Personal-context access is not authorized. Run "
            "`request_personal_context_access`, approve it in Praxys, then "
            "run `complete_personal_context_access`."
        )
    return {"Authorization": f"******"}


def _context_remote_get(path: str) -> dict:
    import requests

    res = requests.get(
        f"{REMOTE_URL}{path}",
        headers=_context_remote_headers(),
        timeout=30,
    )
    _check_auth_error(res)
    return res.json()


def _context_remote_post(path: str, data: dict | None = None) -> dict:
    import requests

    headers = _context_remote_headers()
    headers["Content-Type"] = "application/json"
    res = requests.post(
        f"{REMOTE_URL}{path}",
        headers=headers,
        json=data,
        timeout=30,
    )
    _check_auth_error(res)
    return res.json() if res.content else {}


def _frontend_link(path: str) -> str:
    """Join only a fixed same-client relative path to the frontend origin."""
    from urllib.parse import urlsplit

    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise RuntimeError("Praxys returned an invalid authorization path")
    frontend = urlsplit(FRONTEND_URL)
    if (
        frontend.scheme not in ("http", "https")
        or not frontend.netloc
        or frontend.username is not None
        or frontend.password is not None
        or frontend.path not in ("", "/")
        or frontend.query
        or frontend.fragment
    ):
        raise RuntimeError("PRAXYS_FRONTEND_URL must be an HTTP(S) origin")
    return f"{FRONTEND_URL}{path}"


# ---------------------------------------------------------------------------
# Local helpers (direct DB access)
# ---------------------------------------------------------------------------

_db_initialized = False
_cached_user_id: str | None = None


def _local_db():
    """Get a local DB session."""
    global _db_initialized
    from db.session import init_db
    if not _db_initialized:
        init_db()
        _db_initialized = True
    # Re-import after init_db sets the module-level SessionLocal
    from db import session as db_session
    return db_session.SessionLocal()


def _local_user_id() -> str:
    """Get the user ID for local mode.

    Priority:
    1. PRAXYS_USER_ID (or legacy TRAINSIGHT_USER_ID) env var (explicit override)
    2. First active user found in the database

    Raises RuntimeError if no users exist (register via the web UI first).
    """
    global _cached_user_id
    if _cached_user_id:
        return _cached_user_id

    # Check env var override (prefer PRAXYS_USER_ID; fall back to legacy)
    env_uid = os.environ.get("PRAXYS_USER_ID") or os.environ.get("TRAINSIGHT_USER_ID")
    if env_uid:
        _cached_user_id = env_uid
        return env_uid

    # Find first active user in DB
    db = _local_db()
    try:
        from db.models import User
        user = db.query(User).filter(User.is_active == True).first()
        if user:
            _cached_user_id = user.id
            logger.info("Local mode: using user %s (%s)", user.id, user.email)
            return user.id
    finally:
        db.close()

    raise RuntimeError(
        "No users found in database. Start the server and register "
        "via the web UI first: python -m uvicorn api.main:app --reload"
    )


def _local_dashboard_data() -> dict:
    db = _local_db()
    try:
        from api.deps import get_dashboard_data
        return get_dashboard_data(user_id=_local_user_id(), db=db)
    finally:
        db.close()


def _local_route_result(callback):
    """Call a FastAPI route directly while preserving its HTTP error text."""
    from fastapi import HTTPException
    from pydantic import ValidationError

    try:
        return callback()
    except ValidationError as exc:
        raise RuntimeError(
            _api_error_message(
                422,
                exc.errors(include_url=False),
            )
        ) from exc
    except HTTPException as exc:
        if exc.status_code == 401:
            raise RuntimeError(_not_authenticated_message()) from exc
        raise RuntimeError(
            _api_error_message(exc.status_code, exc.detail)
        ) from exc


def _local_data_user_id(db) -> str:
    """Mirror get_data_user_id without requiring an HTTP request."""
    from db.models import User

    user_id = _local_user_id()
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise RuntimeError(_not_authenticated_message())
    if not user.is_demo:
        return user_id
    target = db.query(User).filter(
        User.id == user.demo_of,
        User.is_active == True,
    ).first()
    if target is None:
        raise RuntimeError(
            _api_error_message(
                403,
                "Demo source account is no longer available",
            )
        )
    return target.id


def _local_write_user_id(db) -> str:
    """Mirror require_write_access without requiring an HTTP request."""
    from db.models import User

    user_id = _local_user_id()
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise RuntimeError(_not_authenticated_message())
    if user.is_demo:
        raise RuntimeError(
            _api_error_message(403, "Demo accounts cannot modify data")
        )
    return user_id


def _local_get_settings() -> dict:
    db = _local_db()
    try:
        from api.routes.settings import get_settings as get_settings_route

        viewer_user_id = _local_user_id()
        user_id = _local_data_user_id(db)
        return _local_route_result(
            lambda: get_settings_route(
                viewer_user_id=viewer_user_id,
                user_id=user_id,
                db=db,
            )
        )
    finally:
        db.close()


def _local_get_plan(start: str, end: str) -> dict:
    db = _local_db()
    try:
        from api.routes.plan import get_plan as get_plan_route
        from starlette.requests import Request
        from starlette.responses import Response

        viewer_user_id = _local_user_id()
        user_id = _local_data_user_id(db)
        request = Request({
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/plan",
            "raw_path": b"/api/plan",
            "query_string": b"",
            "headers": [],
            "client": ("local", 0),
            "server": ("local", 80),
        })
        result = _local_route_result(
            lambda: get_plan_route(
                request=request,
                response=Response(),
                start=start,
                end=end,
                viewer_user_id=viewer_user_id,
                user_id=user_id,
                db=db,
            )
        )
        if hasattr(result, "body"):
            return json.loads(bytes(result.body).decode("utf-8"))
        return result
    finally:
        db.close()


def _local_update_settings(payload: dict) -> dict:
    db = _local_db()
    try:
        from api.routes.settings import (
            SettingsUpdate,
            update_settings as update_settings_route,
        )

        user_id = _local_write_user_id(db)
        body = _local_route_result(lambda: SettingsUpdate(**payload))
        return _local_route_result(
            lambda: update_settings_route(
                body=body,
                user_id=user_id,
                db=db,
            )
        )
    finally:
        db.close()


def _local_cleanup_managed_deliveries() -> dict:
    db = _local_db()
    try:
        from api.routes.plan import (
            CleanupPlanDeliveriesRequest,
            cleanup_plan_deliveries,
        )
        from sqlalchemy.exc import SQLAlchemyError

        user_id = _local_write_user_id(db)
        try:
            return _local_route_result(
                lambda: cleanup_plan_deliveries(
                    request=CleanupPlanDeliveriesRequest(scope="future"),
                    current_user_id=user_id,
                    db=db,
                )
            )
        except SQLAlchemyError as exc:
            db.rollback()
            raise RuntimeError(_api_error_message(500)) from exc
    finally:
        db.close()


def _local_resolve_plan_conflict(
    reconciliation_id: str,
    action: str,
) -> dict:
    db = _local_db()
    try:
        from api.routes.plan import (
            ResolvePlanReconciliationRequest,
            resolve_plan_reconciliation,
        )

        user_id = _local_write_user_id(db)
        body = _local_route_result(
            lambda: ResolvePlanReconciliationRequest(
                reconciliation_id=reconciliation_id,
                action=action,
            )
        )
        return _local_route_result(
            lambda: resolve_plan_reconciliation(
                request=body,
                current_user_id=user_id,
                db=db,
            )
        )
    finally:
        db.close()


def _local_context_actor(db):
    """Resolve the cached context token through the host's live grant table."""
    from api.mcp_access import authenticate_access_token, token_claims
    from api.personal_context_auth import ContextActor

    raw_token = get_context_token()
    if raw_token is None:
        raise RuntimeError(
            "Personal-context access is not authorized. Run "
            "`request_personal_context_access`, approve it in Praxys, then "
            "run `complete_personal_context_access`."
        )
    token = authenticate_access_token(
        db,
        raw_token=raw_token,
        expected_type="context",
    )
    claims = token_claims(token)
    return ContextActor(
        user_id=token.user_id,
        actor_type="mcp",
        actor_id=token.actor_id,
        scopes=frozenset(token.scopes or []),
        purposes=frozenset(token.purposes or []),
        kinds=frozenset(token.kinds or []),
        is_demo=False,
        credential_kind="context_grant",
        token_id=token.id,
        grant_id=token.id,
        audience=claims["context_audience"],
    )


def _local_request_personal_context_access(payload: dict) -> dict:
    """Create the same pending handoff as the remote API without a DB bypass."""
    db = _local_db()
    try:
        from api.mcp_access import create_context_handoff
        from api.views import utc_isoformat

        created = create_context_handoff(
            db,
            user_id=_local_write_user_id(db),
            actor_id=f"mcp:{get_auth_scope().profile}",
            audience=payload["audience"],
            purpose=payload["purpose"],
            kind=payload["kind"],
            access=payload["access"],
        )
        db.commit()
        return {
            "state": created.state,
            "exchange_secret": created.exchange_secret,
            "authorize_path": (
                f"/mcp/authorize?state={created.state}"
            ),
            "expires_at": utc_isoformat(created.handoff.expires_at),
        }
    finally:
        db.close()


def _local_exchange_personal_context_access(payload: dict) -> dict:
    db = _local_db()
    try:
        from api.mcp_access import (
            McpAccessPending,
            access_names,
            exchange_handoff,
        )
        from api.views import utc_isoformat

        try:
            exchanged = exchange_handoff(
                db,
                state=payload["state"],
                exchange_secret=payload["exchange_secret"],
            )
        except McpAccessPending:
            db.rollback()
            return {"status": "pending"}
        db.commit()
        token = exchanged.token
        return {
            "access_token": exchanged.access_token,
            "token_type": "bearer",
            "expires_at": utc_isoformat(token.expires_at),
            "purpose": (token.purposes or [None])[0],
            "kind": (token.kinds or [None])[0],
            "access": access_names(token),
        }
    finally:
        db.close()


def _local_read_personal_context() -> dict:
    db = _local_db()
    try:
        from api.routes.personal_context import (
            select_scoped_personal_context,
        )
        from starlette.responses import Response

        actor = _local_context_actor(db)
        return _local_route_result(
            lambda: select_scoped_personal_context(
                response=Response(),
                actor=actor,
                db=db,
            )
        )
    finally:
        db.close()


def _local_preview_personal_context(payload: dict) -> dict:
    db = _local_db()
    try:
        from api.routes.personal_context import (
            ScopedContextDraftRequest,
            preview_scoped_personal_context,
        )
        from starlette.responses import Response

        actor = _local_context_actor(db)
        body = _local_route_result(
            lambda: ScopedContextDraftRequest(**payload)
        )
        return _local_route_result(
            lambda: preview_scoped_personal_context(
                body=body,
                response=Response(),
                actor=actor,
                db=db,
            )
        )
    finally:
        db.close()


def _local_revoke_personal_context_access() -> dict:
    db = _local_db()
    try:
        from api.routes.personal_context import (
            revoke_scoped_personal_context_access,
        )
        from starlette.responses import Response

        actor = _local_context_actor(db)
        return _local_route_result(
            lambda: revoke_scoped_personal_context_access(
                response=Response(),
                actor=actor,
                db=db,
            )
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# MCP Tools — Training Data
# ---------------------------------------------------------------------------

@mcp.tool()
def get_daily_brief() -> str:
    """Get today's training brief: training signal (Go/Modify/Rest), recovery status, upcoming workouts, last activity, weekly load."""
    if IS_REMOTE:
        data = _remote_get("/api/today")
    else:
        import pandas as pd
        from api.views import last_activity, upcoming_workouts, week_load
        raw = _local_dashboard_data()
        data = {
            "signal": raw["signal"],
            "recovery_analysis": raw.get("recovery_analysis"),
            "last_activity": last_activity(raw.get("activities", [])),
            "week_load": week_load(raw.get("weekly_review", {})),
            "upcoming": upcoming_workouts(raw.get("plan", pd.DataFrame())),
            "warnings": raw.get("warnings", []),
        }
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
def get_training_review() -> str:
    """Get training analysis: zone distribution, fitness/fatigue trends, diagnosis findings, suggestions, and workout flags."""
    if IS_REMOTE:
        data = _remote_get("/api/training")
    else:
        raw = _local_dashboard_data()
        data = {
            "diagnosis": raw.get("diagnosis"),
            "fitness_fatigue": raw.get("fitness_fatigue"),
            "cp_trend": raw.get("cp_trend"),
            "weekly_review": raw.get("weekly_review"),
            "workout_flags": raw.get("workout_flags"),
        }
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
def get_race_forecast() -> str:
    """Get race prediction: predicted finish time, required CP/pace, goal feasibility assessment, and CP trend."""
    if IS_REMOTE:
        data = _remote_get("/api/goal")
    else:
        raw = _local_dashboard_data()
        data = {
            "race_countdown": raw.get("race_countdown"),
            "cp_trend": raw.get("cp_trend"),
            "cp_trend_data": raw.get("cp_trend_data"),
            "latest_cp": raw.get("latest_cp"),
        }
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
def get_training_context() -> str:
    """Get full training context for AI plan generation: athlete profile, current fitness, recent training, recovery state, and active plan."""
    if IS_REMOTE:
        data = _remote_get("/api/ai/context")
    else:
        from api.ai import build_training_context
        data = build_training_context()
    return json.dumps(data, indent=2, default=str)


# ---------------------------------------------------------------------------
# MCP Tools — Purpose-bound personal context
# ---------------------------------------------------------------------------

_CONTEXT_PURPOSES = {
    "plan_generation",
    "execution_interpretation",
    "plan_adjustment",
    "goal_review",
    "outcome_review",
}
_CONTEXT_KINDS = {
    "temporary_constraint",
    "execution_explanation",
}
_CONTEXT_ACCESS = {"read", "write"}
_CONTEXT_AUDIENCE = "praxys-coach-plugin"
_CONTEXT_PURPOSE_KINDS = {
    "plan_generation": {"temporary_constraint"},
    "execution_interpretation": {"execution_explanation"},
    "plan_adjustment": {
        "temporary_constraint",
        "execution_explanation",
    },
    "goal_review": {"temporary_constraint"},
    "outcome_review": {
        "temporary_constraint",
        "execution_explanation",
    },
}


def _context_request_error(message: str) -> str:
    return json.dumps({"status": "error", "message": message}, indent=2)


@mcp.tool()
def request_personal_context_access(
    purpose: str,
    kind: str,
    access: list[str],
) -> str:
    """Request short-lived structured context access for one purpose.

    This tool never grants access. It returns a Praxys first-party approval
    link. Narrative and AI-consent authority are not requestable.
    """
    if purpose not in _CONTEXT_PURPOSES:
        return _context_request_error("purpose is not supported")
    if kind not in _CONTEXT_KINDS:
        return _context_request_error("kind is not supported")
    if kind not in _CONTEXT_PURPOSE_KINDS[purpose]:
        return _context_request_error(
            "kind is not available for the requested purpose"
        )
    if (
        not access
        or len(access) != len(set(access))
        or not set(access).issubset(_CONTEXT_ACCESS)
    ):
        return _context_request_error(
            "access must contain read, write, or both exactly once"
        )
    if IS_REMOTE and not (get_token() or "").startswith("praxys_mcp_"):
        return _context_request_error(
            "Run `login` again to replace the legacy account token with a "
            "revocable MCP session before requesting personal context."
        )
    ordered_access = [
        item for item in ("read", "write") if item in access
    ]
    payload = {
        "audience": _CONTEXT_AUDIENCE,
        "purpose": purpose,
        "kind": kind,
        "access": ordered_access,
    }
    if IS_REMOTE:
        response = _remote_post(
            "/api/personal-context/scoped-access/requests",
            payload,
        )
    else:
        response = _local_request_personal_context_access(payload)
    save_pending_context_access({
        "state": response["state"],
        "exchange_secret": response["exchange_secret"],
        "expires_at": response["expires_at"],
    })
    return json.dumps({
        "status": "approval_required",
        "authorization_url": _frontend_link(response["authorize_path"]),
        "expires_at": response["expires_at"],
        "audience": _CONTEXT_AUDIENCE,
        "purpose": purpose,
        "kind": kind,
        "access": ordered_access,
        "authority": {
            "narrative": False,
            "ai_consent": False,
            "durable_write": False,
        },
    }, indent=2)


@mcp.tool()
def complete_personal_context_access() -> str:
    """Exchange an athlete-approved request and cache its bounded token."""
    pending = get_pending_context_access()
    if pending is None:
        return _context_request_error(
            "No context request is pending. Run "
            "request_personal_context_access first."
        )
    payload = {
        "state": pending.get("state"),
        "exchange_secret": pending.get("exchange_secret"),
    }
    if IS_REMOTE:
        response = _public_post(
            "/api/auth/mcp/handoffs/exchange",
            payload,
        )
    else:
        response = _local_exchange_personal_context_access(payload)
    if response.get("status") == "pending":
        return json.dumps({
            "status": "pending",
            "message": "Approve or deny the request in Praxys first.",
            "expires_at": pending.get("expires_at"),
        }, indent=2)
    save_context_token(response["access_token"])
    clear_pending_context_access()
    return json.dumps({
        "status": "authorized",
        "expires_at": response["expires_at"],
        "purpose": response.get("purpose"),
        "kind": response.get("kind"),
        "access": response.get("access") or [],
        "authority": {
            "narrative": False,
            "ai_consent": False,
            "durable_write": False,
        },
    }, indent=2)


@mcp.tool()
def read_personal_context() -> str:
    """Read the minimum structured projection allowed by the active grant."""
    data = (
        _context_remote_get("/api/personal-context/scoped/selection")
        if IS_REMOTE
        else _local_read_personal_context()
    )
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
def preview_personal_context(
    kind: str,
    purpose: str,
    category: str,
    fields: dict,
    starts_at: str | None = None,
    expires_at: str | None = None,
    purge_after: str | None = None,
    linked_subject_type: str | None = None,
    linked_subject_id: str | None = None,
) -> str:
    """Draft one structured context item for first-party athlete confirmation.

    The grant is single-use. This tool cannot accept narrative and cannot save,
    correct, approve, or grant AI processing for personal context.
    """
    payload = {
        "kind": kind,
        "purpose": purpose,
        "payload": {
            "category": category,
            "fields": fields,
        },
        "starts_at": starts_at,
        "expires_at": expires_at,
        "purge_after": purge_after,
        "linked_subject_type": linked_subject_type,
        "linked_subject_id": linked_subject_id,
    }
    payload = {
        key: value for key, value in payload.items() if value is not None
    }
    data = (
        _context_remote_post(
            "/api/personal-context/scoped/preview",
            payload,
        )
        if IS_REMOTE
        else _local_preview_personal_context(payload)
    )
    data = dict(data)
    data["confirmation_url"] = _frontend_link(
        data["confirmation_path"]
    )
    data["durable_write_performed"] = False
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
def revoke_personal_context_access() -> str:
    """Revoke the current structured-context grant immediately."""
    data = (
        _context_remote_post(
            "/api/personal-context/scoped-access/revoke",
            {},
        )
        if IS_REMOTE
        else _local_revoke_personal_context_access()
    )
    clear_context_token()
    return json.dumps(data, indent=2, default=str)


# ---------------------------------------------------------------------------
# MCP Tools — Settings & Connections
# ---------------------------------------------------------------------------

@mcp.tool()
def get_settings() -> str:
    """Get current user settings: training base, thresholds, zones, goal, connected platforms, and display config."""
    if IS_REMOTE:
        data = _remote_get("/api/settings")
    else:
        data = _local_get_settings()
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
def update_settings(settings: dict) -> str:
    """Update ordinary user settings such as training_base, zones, or goal.

    Managed-plan fields are intentionally rejected here. Use the dedicated
    adopt, pause, resume, and leave tools so consent and preview fencing cannot
    be bypassed.
    """
    managed_fields = {
        "plan_management",
        "managed_plan_preview_start",
    }
    blocked = sorted(managed_fields.intersection(settings))
    if blocked:
        return _managed_plan_error(
            "Managed-plan settings require dedicated lifecycle tools; "
            f"unsupported fields: {', '.join(blocked)}"
        )
    if IS_REMOTE:
        data = _remote_put("/api/settings", settings)
    else:
        data = _local_update_settings(settings)
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
def get_sync_settings() -> str:
    """Get current auto-sync frequency and allowed options."""
    if IS_REMOTE:
        settings = _remote_get("/api/settings")
        source_options = settings.get("config", {}).get("source_options", {})
        from db.sync_scheduler import (
            ALLOWED_SYNC_INTERVAL_HOURS,
            DEFAULT_SYNC_INTERVAL_HOURS,
            get_user_sync_interval_hours,
        )
        data = {
            "sync_interval_hours": get_user_sync_interval_hours(source_options),
            "allowed_sync_interval_hours": settings.get(
                "sync_interval_options_hours", list(ALLOWED_SYNC_INTERVAL_HOURS)
            ),
            "default_sync_interval_hours": settings.get(
                "default_sync_interval_hours", DEFAULT_SYNC_INTERVAL_HOURS
            ),
        }
    else:
        db = _local_db()
        try:
            from analysis.config import load_config_from_db
            from db.sync_scheduler import (
                ALLOWED_SYNC_INTERVAL_HOURS,
                DEFAULT_SYNC_INTERVAL_HOURS,
                get_user_sync_interval_hours,
            )

            config = load_config_from_db(_local_user_id(), db)
            data = {
                "sync_interval_hours": get_user_sync_interval_hours(config.source_options),
                "allowed_sync_interval_hours": list(ALLOWED_SYNC_INTERVAL_HOURS),
                "default_sync_interval_hours": DEFAULT_SYNC_INTERVAL_HOURS,
            }
        finally:
            db.close()
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
def set_sync_frequency(hours: int) -> str:
    """Set auto-sync frequency in hours (allowed: 6, 12, 24)."""
    from db.sync_scheduler import (
        ALLOWED_SYNC_INTERVAL_HOURS,
        normalize_sync_interval_hours,
    )

    try:
        normalized_hours = normalize_sync_interval_hours(hours)
    except ValueError as exc:
        return json.dumps(
            {
                "status": "error",
                "message": str(exc),
                "allowed_sync_interval_hours": list(ALLOWED_SYNC_INTERVAL_HOURS),
            },
            indent=2,
            default=str,
        )
    if IS_REMOTE:
        try:
            updated = _remote_put("/api/settings", {
                "source_options": {"sync_interval_hours": normalized_hours}
            })
        except RuntimeError as exc:
            return json.dumps(
                {
                    "status": "error",
                    "message": str(exc),
                    "allowed_sync_interval_hours": list(ALLOWED_SYNC_INTERVAL_HOURS),
                },
                indent=2,
                default=str,
            )
        source_options = updated.get("config", {}).get("source_options", {})
        data = {
            "status": updated.get("status", "ok"),
            "sync_interval_hours": source_options.get("sync_interval_hours", normalized_hours),
        }
    else:
        db = _local_db()
        try:
            from analysis.config import load_config_from_db, save_config_to_db
            config = load_config_from_db(_local_user_id(), db)
            source_options = dict(config.source_options or {})
            source_options["sync_interval_hours"] = normalized_hours
            config.source_options = source_options
            save_config_to_db(_local_user_id(), config, db)
            data = {"status": "updated", "sync_interval_hours": normalized_hours}
        finally:
            db.close()
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
def get_connections() -> str:
    """Get connected platforms and their status. Credentials are never returned — only connection status."""
    if IS_REMOTE:
        data = _remote_get("/api/settings/connections")
    else:
        db = _local_db()
        try:
            from api.routes.settings import (
                get_connections as get_connections_route,
            )

            viewer_user_id = _local_user_id()
            user_id = _local_data_user_id(db)
            data = _local_route_result(
                lambda: get_connections_route(
                    viewer_user_id=viewer_user_id,
                    user_id=user_id,
                    db=db,
                )
            )
        finally:
            db.close()
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
def connect_platform(platform: str, credentials: dict) -> str:
    """Connect a platform by storing encrypted credentials.

    Args:
        platform: A platform currently offered in Praxys Settings.
        credentials: The platform-specific credential fields accepted by
            Praxys Settings.

    IMPORTANT: Never ask the user to type credentials in the conversation.
    Instead, ask them to enter credentials in the web Settings page. Only use
    this tool if the user explicitly provides credentials or if reading from
    a secure source.
    """
    if IS_REMOTE:
        data = _remote_post(f"/api/settings/connections/{platform}", credentials)
    else:
        db = _local_db()
        try:
            from api.routes.settings import (
                ConnectPlatformRequest,
                connect_platform as connect_platform_route,
            )

            user_id = _local_write_user_id(db)
            body = _local_route_result(
                lambda: ConnectPlatformRequest(**credentials)
            )
            data = _local_route_result(
                lambda: connect_platform_route(
                    platform=platform,
                    body=body,
                    user_id=user_id,
                    db=db,
                )
            )
        finally:
            db.close()
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
def disconnect_platform(platform: str) -> str:
    """Disconnect a platform — deletes stored credentials."""
    if IS_REMOTE:
        data = _remote_delete(f"/api/settings/connections/{platform}")
    else:
        db = _local_db()
        try:
            from api.routes.settings import (
                disconnect_platform as disconnect_platform_route,
            )

            user_id = _local_write_user_id(db)
            data = _local_route_result(
                lambda: disconnect_platform_route(
                    platform=platform,
                    user_id=user_id,
                    db=db,
                )
            )
        finally:
            db.close()
    return json.dumps(data, indent=2, default=str)


# ---------------------------------------------------------------------------
# MCP Tools — Plans & Sync
# ---------------------------------------------------------------------------

def _local_push_plan(plan_csv: str, mode: str) -> dict:
    """Local mirror of POST /api/plan/upload?mode=...."""
    db = _local_db()
    try:
        from api.routes.ai import PlanUpload, upload_plan

        user_id = _local_write_user_id(db)
        return _local_route_result(
            lambda: upload_plan(
                payload=PlanUpload(csv=plan_csv),
                mode=mode,
                user_id=user_id,
                db=db,
            )
        )
    finally:
        db.close()


_PLAN_AUTHORING_GUIDANCE = (
    "This operation saves canonical Praxys plan rows. It does not directly "
    "request an execution-platform mutation. If managed delivery is already "
    "enabled, Praxys may independently deliver the saved rows under that "
    "existing policy."
)


def _save_training_plan(plan_csv: str, mode: str) -> dict:
    if mode not in ("replace", "merge"):
        return {
            "status": "error",
            "message": "mode must be 'replace' or 'merge'",
            "operation": "plan_authoring",
            "direct_delivery_requested": False,
            "guidance": _PLAN_AUTHORING_GUIDANCE,
        }
    if IS_REMOTE:
        data = _remote_post(f"/api/plan/upload?mode={mode}", {"csv": plan_csv})
    else:
        data = _local_push_plan(plan_csv, mode)
    data = dict(data)
    data.update({
        "operation": "plan_authoring",
        "direct_delivery_requested": False,
        "guidance": _PLAN_AUTHORING_GUIDANCE,
    })
    return data


@mcp.tool()
def save_training_plan(plan_csv: str, mode: str = "replace") -> str:
    """Save authored workouts into the canonical Praxys plan.

    This is a plan-authoring operation, not a direct platform-delivery command.
    Pass CSV text with date, workout_type, planned_duration_min,
    planned_distance_km, target_power_min, target_power_max, and
    workout_description columns.

    mode='replace' replaces future Praxys-owned rows. mode='merge' changes only
    dates present in the CSV. Manual and external-coach rows remain untouched.
    """
    data = _save_training_plan(plan_csv, mode)
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
def push_training_plan(plan_csv: str, mode: str = "replace") -> str:
    """Backward-compatible alias for save_training_plan.

    Despite the historical name, this authors canonical Praxys plan rows; it
    does not directly push workouts to an execution platform. New callers
    should use save_training_plan so authoring and delivery are unambiguous.
    """
    data = _save_training_plan(plan_csv, mode)
    data["deprecated_tool"] = (
        "Use save_training_plan for plan authoring. Managed delivery is "
        "controlled separately."
    )
    return json.dumps(data, indent=2, default=str)


def _managed_plan_settings() -> dict:
    if IS_REMOTE:
        return _remote_get("/api/settings")
    return _local_get_settings()


def _managed_plan_window(days: int) -> dict:
    today = datetime.now(timezone.utc).date()
    end = today + timedelta(days=days - 1)
    start_iso = today.isoformat()
    end_iso = end.isoformat()
    if IS_REMOTE:
        return _remote_get(
            f"/api/plan?start={start_iso}&end={end_iso}"
        )
    return _local_get_plan(start_iso, end_iso)


def _managed_plan_update(payload: dict) -> dict:
    if IS_REMOTE:
        return _remote_put("/api/settings", payload)
    return _local_update_settings(payload)


def _managed_plan_cleanup() -> dict:
    if IS_REMOTE:
        import requests

        try:
            return _remote_post(
                "/api/plan/deliveries/cleanup",
                {"scope": "future"},
            )
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Managed-plan cleanup request failed: {exc}"
            ) from exc
    return _local_cleanup_managed_deliveries()


def _managed_plan_resolve(
    reconciliation_id: str,
    action: str,
) -> dict:
    if IS_REMOTE:
        return _remote_post(
            "/api/plan/reconciliation/resolve",
            {
                "reconciliation_id": reconciliation_id,
                "action": action,
            },
        )
    return _local_resolve_plan_conflict(reconciliation_id, action)


def _plan_management(settings: dict) -> dict:
    config = settings.get("config") or {}
    return dict(config.get("plan_management") or {})


def _managed_plan_error(message: str, settings: dict | None = None) -> str:
    data = {"status": "error", "message": message}
    if settings is not None:
        data["plan_management"] = _plan_management(settings)
    return json.dumps(data, indent=2, default=str)


def _managed_preview_error(
    preview_start: str,
    preview_end: str,
) -> str | None:
    try:
        start = date.fromisoformat(preview_start)
        end = date.fromisoformat(preview_end)
    except ValueError:
        return "preview_start and preview_end must be YYYY-MM-DD dates"
    if end - start != timedelta(days=13):
        return "Managed-plan adoption requires the reviewed 14-day window"
    return None


def _managed_plan_lifecycle_result(
    operation: str,
    response: dict,
    *,
    status: str | None = None,
) -> dict:
    return {
        "status": status or response.get("status") or "ok",
        "operation": operation,
        "plan_management": _plan_management(response),
        "connection_statuses": response.get("connection_statuses") or {},
    }


def _is_praxys_owned_workout(workout: dict) -> bool:
    owner = workout.get("owner")
    return owner == "praxys" or (
        owner is None and workout.get("source") == "ai"
    )


@mcp.tool()
def get_managed_plan_status(days: int = 14) -> str:
    """Get managed-plan settings, delivery state, workouts, and conflicts.

    The returned workouts include canonical IDs and opaque reconciliation IDs
    required by resolution tools. This read never changes a target calendar.
    """
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
        return _managed_plan_error("days must be an integer from 1 to 365")

    settings = _managed_plan_settings()
    plan = _managed_plan_window(days)
    management = _plan_management(settings)
    connection_statuses = settings.get("connection_statuses") or {}
    capabilities = settings.get("platform_capabilities") or {}
    target = management.get("execution_target")
    workouts = list(plan.get("workouts") or [])
    praxys_count = sum(
        1 for workout in workouts if _is_praxys_owned_workout(workout)
    )
    conflict_count = sum(
        1
        for workout in workouts
        if (workout.get("reconciliation") or {}).get("conflict") is True
    )
    available_targets = [
        platform
        for platform, connection_status in connection_statuses.items()
        if (
            connection_status == "connected"
            and (capabilities.get(platform) or {}).get("plan") is True
        )
    ]
    data = {
        "status": "ok",
        "plan_management": management,
        "execution_target_status": (
            connection_statuses.get(target, "not_configured")
            if target
            else "not_configured"
        ),
        "available_execution_targets": available_targets,
        "window": plan.get("window") or {},
        "summary": {
            "praxys_workouts": praxys_count,
            "external_workouts": len(workouts) - praxys_count,
            "conflicts": conflict_count,
        },
        "workouts": workouts,
        "semantics": {
            "authoring": (
                "save_training_plan changes the canonical Praxys plan."
            ),
            "delivery": (
                "Managed delivery copies canonical workouts to the selected "
                "execution target under the user's existing consent."
            ),
            "ownership": (
                "Manual and external-coach workouts remain untouched."
            ),
        },
    }
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
def adopt_managed_plan(
    execution_target: str,
    preview_start: str,
    preview_end: str,
) -> str:
    """Adopt Praxys as planner and enable delivery after explicit approval.

    Pass the exact window.start and window.end from a freshly fetched
    get_managed_plan_status(days=14) result. Only call after the user reviewed
    that window and explicitly consented. Existing manual and external-coach
    workouts are not adopted or removed.
    """
    target = execution_target.strip().lower()
    if not target:
        return _managed_plan_error("execution_target is required")
    preview_error = _managed_preview_error(preview_start, preview_end)
    if preview_error:
        return _managed_plan_error(preview_error)
    settings = _managed_plan_settings()
    management = _plan_management(settings)
    if management.get("mode") != "external":
        return _managed_plan_error(
            "Managed mode is already adopted; use resume_managed_plan if it "
            "is paused.",
            settings,
        )
    response = _managed_plan_update({
        "managed_plan_preview_start": preview_start,
        "plan_management": {
            "mode": "praxys",
            "execution_target": target,
            "delivery_enabled": True,
            "adjustment_policy": "suggest_only",
        },
    })
    return json.dumps(
        _managed_plan_lifecycle_result("adopt", response),
        indent=2,
        default=str,
    )


@mcp.tool()
def pause_managed_plan() -> str:
    """Pause managed delivery without changing the canonical Praxys plan."""
    settings = _managed_plan_settings()
    management = _plan_management(settings)
    if management.get("mode") != "praxys":
        return _managed_plan_error(
            "Managed mode is not active; adopt a plan before pausing.",
            settings,
        )
    if management.get("delivery_enabled") is False:
        return json.dumps(
            _managed_plan_lifecycle_result(
                "pause",
                settings,
                status="unchanged",
            ),
            indent=2,
            default=str,
        )
    response = _managed_plan_update({
        "plan_management": {"delivery_enabled": False},
    })
    return json.dumps(
        _managed_plan_lifecycle_result("pause", response),
        indent=2,
        default=str,
    )


@mcp.tool()
def resume_managed_plan(
    preview_start: str,
    preview_end: str,
) -> str:
    """Resume an adopted plan after reviewing its current 14-day window.

    Pass the exact window.start and window.end from a freshly fetched
    get_managed_plan_status(days=14) result.
    """
    preview_error = _managed_preview_error(preview_start, preview_end)
    if preview_error:
        return _managed_plan_error(preview_error)
    settings = _managed_plan_settings()
    management = _plan_management(settings)
    if management.get("mode") != "praxys":
        return _managed_plan_error(
            "Managed mode is not adopted; use adopt_managed_plan instead.",
            settings,
        )
    if management.get("delivery_enabled") is True:
        return json.dumps(
            _managed_plan_lifecycle_result(
                "resume",
                settings,
                status="unchanged",
            ),
            indent=2,
            default=str,
        )
    response = _managed_plan_update({
        "managed_plan_preview_start": preview_start,
        "plan_management": {"delivery_enabled": True},
    })
    return json.dumps(
        _managed_plan_lifecycle_result("resume", response),
        indent=2,
        default=str,
    )


@mcp.tool()
def leave_managed_plan(remove_future_deliveries: bool = False) -> str:
    """Leave managed mode, optionally removing ledger-owned future deliveries.

    Completed activity history is never removed. If removal is requested,
    only future workouts attributable to the Praxys delivery ledger are
    targeted; manual and external-coach workouts remain untouched.
    """
    settings = _managed_plan_settings()
    management = _plan_management(settings)
    if management.get("mode") == "external":
        response = settings
        status = "unchanged"
    else:
        response = _managed_plan_update({
            "plan_management": {
                "mode": "external",
                "delivery_enabled": False,
            },
        })
        status = None
    result = _managed_plan_lifecycle_result(
        "leave",
        response,
        status=status,
    )
    if not remove_future_deliveries:
        result["cleanup"] = {"status": "kept"}
        return json.dumps(result, indent=2, default=str)
    try:
        cleanup = _managed_plan_cleanup()
    except RuntimeError as exc:
        result["status"] = "partial"
        result["cleanup"] = {
            "status": "error",
            "message": str(exc),
            "retry_tool": "cleanup_managed_plan_deliveries",
        }
        return json.dumps(result, indent=2, default=str)
    result["cleanup"] = cleanup
    if cleanup.get("status") == "partial":
        result["status"] = "partial"
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def cleanup_managed_plan_deliveries() -> str:
    """Retry ledger-scoped removal after leaving managed mode."""
    settings = _managed_plan_settings()
    if _plan_management(settings).get("mode") != "external":
        return _managed_plan_error(
            "Leave managed mode before removing future deliveries.",
            settings,
        )
    return json.dumps(
        _managed_plan_cleanup(),
        indent=2,
        default=str,
    )


@mcp.tool()
def resolve_managed_plan_conflict(
    reconciliation_id: str,
    action: str,
) -> str:
    """Resolve one visible conflict using its opaque reconciliation ID.

    action must be accept_target (adopt the observed platform version) or
    restore_praxys (keep Praxys canonical and restore the target copy).
    """
    if action not in ("accept_target", "restore_praxys"):
        return _managed_plan_error(
            "action must be 'accept_target' or 'restore_praxys'"
        )
    if not reconciliation_id:
        return _managed_plan_error("reconciliation_id is required")
    return json.dumps(
        _managed_plan_resolve(reconciliation_id, action),
        indent=2,
        default=str,
    )


@mcp.tool()
def update_training_day(
    plan_date: str,
    workout_type: str,
    planned_duration_min: float | None = None,
    planned_distance_km: float | None = None,
    target_power_min: float | None = None,
    target_power_max: float | None = None,
    workout_description: str | None = None,
) -> str:
    """Upsert a single Praxys-owned workout for the given date (YYYY-MM-DD).

    Replaces any existing Praxys-owned entry for that date with the new values;
    external rows and other dates are untouched. Use this for shifts and
    partial edits — much safer than save_training_plan when you only want to
    change one day.
    """
    payload = {
        "workout_type": workout_type,
        "planned_duration_min": planned_duration_min,
        "planned_distance_km": planned_distance_km,
        "target_power_min": target_power_min,
        "target_power_max": target_power_max,
        "workout_description": workout_description,
    }
    if IS_REMOTE:
        data = _remote_put(f"/api/plan/{plan_date}", payload)
    else:
        db = _local_db()
        try:
            from api.routes.ai import PlanWorkout, upsert_plan_day

            user_id = _local_write_user_id(db)
            data = _local_route_result(
                lambda: upsert_plan_day(
                    plan_date=plan_date,
                    workout=PlanWorkout(**payload),
                    user_id=user_id,
                    db=db,
                )
            )
        finally:
            db.close()
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
def delete_training_day(plan_date: str) -> str:
    """Delete the Praxys-owned workout(s) for the given date (YYYY-MM-DD)."""
    if IS_REMOTE:
        data = _remote_delete(f"/api/plan/{plan_date}")
    else:
        db = _local_db()
        try:
            from api.routes.ai import delete_plan_day

            user_id = _local_write_user_id(db)
            data = _local_route_result(
                lambda: delete_plan_day(
                    plan_date=plan_date,
                    user_id=user_id,
                    db=db,
                )
            )
        finally:
            db.close()
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
def push_training_insights(
    insight_type: str,
    headline: str,
    summary: str,
    findings: list[dict] | None = None,
    recommendations: list[str] | None = None,
    meta: dict | None = None,
) -> str:
    """Push AI-generated insights to the web dashboard.

    Call this at the end of a training review, daily brief, or race forecast
    to persist your analysis so the user can see it on the web.

    Args:
        insight_type: One of 'training_review', 'daily_brief', 'race_forecast'
        headline: One-sentence summary (e.g., "Strong volume but threshold work needed")
        summary: 2-3 paragraph narrative analysis
        findings: List of {type: "positive"|"warning"|"neutral", text: "..."} findings
        recommendations: List of actionable recommendation strings
        meta: Optional metadata (data_range, training_base, etc.)
    """
    payload = {
        "insight_type": insight_type,
        "headline": headline,
        "summary": summary,
        "findings": findings or [],
        "recommendations": recommendations or [],
        "meta": meta or {},
    }

    if IS_REMOTE:
        data = _remote_post("/api/insights", payload)
    else:
        from datetime import datetime
        db = _local_db()
        try:
            from db.models import AiInsight
            user_id = _local_user_id()
            existing = db.query(AiInsight).filter(
                AiInsight.user_id == user_id,
                AiInsight.insight_type == insight_type,
            ).first()
            if existing:
                existing.headline = headline
                existing.summary = summary
                existing.findings = findings or []
                existing.recommendations = recommendations or []
                existing.meta = meta or {}
                existing.generated_at = datetime.utcnow()
            else:
                db.add(AiInsight(
                    user_id=user_id,
                    insight_type=insight_type,
                    headline=headline,
                    summary=summary,
                    findings=findings or [],
                    recommendations=recommendations or [],
                    meta=meta or {},
                ))
            db.commit()
            data = {"status": "saved", "insight_type": insight_type}
        finally:
            db.close()
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
def trigger_sync(sources: list[str] | None = None) -> str:
    """Trigger data sync, optionally limited to connected platform IDs. Requires the backend server to be running."""
    if IS_REMOTE:
        data = _remote_post("/api/sync", {"sources": sources} if sources else None)
    else:
        # Local mode: sync requires the API server (background threads, rate limiting).
        # Try calling the local API if it's running.
        import requests
        token = get_token()
        if token is None:
            return json.dumps({
                "status": "not_authenticated",
                "message": (
                    "Local sync still uses the authenticated API. "
                    + _not_authenticated_message()
                ),
            })
        local_headers = {"Authorization": f"Bearer {token}"}
        try:
            url = "http://localhost:8000/api/sync"
            if sources:
                results = []
                for s in sources:
                    res = requests.post(f"{url}/{s}", headers=local_headers, timeout=5)
                    results.append({"source": s, "status": res.json().get("status", "error")})
                data = {"results": results}
            else:
                res = requests.post(url, headers=local_headers, timeout=5)
                data = res.json()
        except requests.ConnectionError:
            data = {"status": "error", "message": "Backend server not running. Start it with: python -m uvicorn api.main:app --reload"}
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
def get_sync_status() -> str:
    """Check the current sync status for all connected platforms."""
    if IS_REMOTE:
        data = _remote_get("/api/sync/status")
    else:
        import requests
        try:
            res = requests.get("http://localhost:8000/api/sync/status", timeout=5)
            data = res.json()
        except requests.ConnectionError:
            # Fall back to checking connections in DB
            db = _local_db()
            try:
                from api.stryd_access import stryd_connection_enabled
                from db.models import UserConnection
                from db.sync_scheduler import ACTIVE_CONNECTION_STATUSES
                user_id = _local_user_id()
                allow_private_connection = stryd_connection_enabled(
                    db,
                    user_id=user_id,
                )
                connections = db.query(UserConnection).filter(
                    UserConnection.user_id == user_id
                ).all()
                data = {}
                for conn in connections:
                    if (
                        conn.platform == "stryd"
                        and not allow_private_connection
                    ):
                        continue
                    data[conn.platform] = {
                        "status": "idle",
                        "last_sync": conn.last_sync.isoformat() if conn.last_sync else None,
                        "connected": conn.status in ACTIVE_CONNECTION_STATUSES,
                    }
            finally:
                db.close()
    return json.dumps(data, indent=2, default=str)


def _opaque_browser_login(scope) -> str:
    """Complete browser login without putting a bearer in any URL."""
    import time
    import webbrowser
    import requests

    handoff = _public_post(
        "/api/auth/mcp/handoffs",
        {"audience": _CONTEXT_AUDIENCE},
    )
    webbrowser.open(_frontend_link(handoff["authorize_path"]))
    deadline = time.monotonic() + 120
    exchanged: dict | None = None
    while time.monotonic() < deadline:
        result = _public_post(
            "/api/auth/mcp/handoffs/exchange",
            {
                "state": handoff["state"],
                "exchange_secret": handoff["exchange_secret"],
            },
        )
        if result.get("access_token"):
            exchanged = result
            break
        time.sleep(2)
    if exchanged is None:
        return json.dumps({
            "status": "error",
            "message": "Login timed out. Start a new request and try again.",
        })

    token_path = save_token(exchanged["access_token"])
    me_res = requests.get(
        f"{REMOTE_URL}/api/auth/mcp/me",
        headers=_get_remote_headers(),
        timeout=10,
    )
    _check_auth_error(me_res)
    user_info = me_res.json()
    save_config({
        "url": REMOTE_URL,
        "email": user_info.get("email", ""),
    })
    return json.dumps({
        "status": "authenticated",
        "profile": scope.profile,
        "email": user_info.get("email", ""),
        "user_id": user_info.get("id"),
        "is_admin": user_info.get("is_superuser", False),
        "token_cached": str(token_path),
        "expires_at": exchanged.get("expires_at"),
    })


@mcp.tool()
def login() -> str:
    """Authenticate with Praxys via browser login.

    Opens a first-party approval page with opaque state, then exchanges the
    separately held verifier for a revocable MCP session. Account JWTs and
    passwords never enter the plugin process.
    """
    if not IS_REMOTE:
        return json.dumps({"status": "skipped", "message": "Login not needed in local mode"})
    scope = get_auth_scope()
    return _opaque_browser_login(scope)


@mcp.tool()
def whoami() -> str:
    """Show which Praxys account is currently authenticated."""
    scope = get_auth_scope()
    if not IS_REMOTE:
        uid = _local_user_id()
        db = _local_db()
        try:
            from db.models import User
            user = db.query(User).filter(User.id == uid).first()
            return json.dumps({
                "mode": "local",
                "profile": scope.profile,
                "email": user.email if user else "unknown",
                "user_id": uid,
            })
        finally:
            db.close()

    if get_token() is None:
        return json.dumps({
            "status": "not_authenticated",
            "mode": "remote",
            "profile": scope.profile,
            "url": REMOTE_URL,
            "message": _not_authenticated_message(),
        })

    import requests
    headers = _get_remote_headers()
    res = requests.get(
        f"{REMOTE_URL}/api/auth/mcp/me",
        headers=headers,
        timeout=10,
    )
    if res.status_code == 401:
        return json.dumps({"status": "token_expired", "message": "Token expired. Please run `login` again."})
    res.raise_for_status()
    data = res.json()
    return json.dumps({
        "mode": "remote",
        "profile": scope.profile,
        "url": REMOTE_URL,
        "email": data.get("email"),
        "user_id": data.get("id"),
        "is_admin": data.get("is_superuser", False),
    })


@mcp.tool()
def logout() -> str:
    """Revoke issued grants, then clear only the active profile."""
    scope = get_auth_scope()
    remote_session_revoked = False
    remote_context_revoked = False
    if IS_REMOTE:
        import requests

        if get_context_token() is not None:
            try:
                _context_remote_post(
                    "/api/personal-context/scoped-access/revoke",
                    {},
                )
                remote_context_revoked = True
            except (RuntimeError, requests.RequestException):
                pass
        session_token = get_token()
        if session_token and session_token.startswith("praxys_mcp_"):
            try:
                res = requests.post(
                    f"{REMOTE_URL}/api/auth/mcp/revoke",
                    headers=_get_remote_headers(),
                    timeout=10,
                )
                _check_auth_error(res)
                remote_session_revoked = True
            except (RuntimeError, requests.RequestException):
                pass
    result = clear_auth_scope()
    return json.dumps({
        "status": (
            "logged_out"
            if result.removed_paths
            or result.legacy_fallback_suppressed
            else "not_authenticated"
        ),
        "profile": scope.profile,
        "token_path": str(scope.token_path),
        "removed": [str(path) for path in result.removed_paths],
        "legacy_fallback_suppressed": (
            result.legacy_fallback_suppressed
        ),
        "remote_session_revoked": remote_session_revoked,
        "remote_context_revoked": remote_context_revoked,
    })


if __name__ == "__main__":
    _preload_local_modules()
    mcp.run()
