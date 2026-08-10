"""Purpose-bound personal-context MCP contract tests."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest import mock


SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"


class _FakeFastMCP:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def tool(self):
        return lambda function: function


def _load_server(*, local: bool) -> ModuleType:
    mcp_module = ModuleType("mcp")
    mcp_server_module = ModuleType("mcp.server")
    fastmcp_module = ModuleType("mcp.server.fastmcp")
    fastmcp_module.FastMCP = _FakeFastMCP
    module_name = f"praxys_context_server_test_{int(local)}"
    spec = importlib.util.spec_from_file_location(module_name, SERVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with (
        mock.patch.dict(
            os.environ,
            {
                "PRAXYS_LOCAL": "1" if local else "0",
                "TRAINSIGHT_LOCAL": "0",
            },
        ),
        mock.patch.dict(
            sys.modules,
            {
                "mcp": mcp_module,
                "mcp.server": mcp_server_module,
                "mcp.server.fastmcp": fastmcp_module,
                module_name: module,
            },
        ),
    ):
        spec.loader.exec_module(module)
    return module


class PersonalContextToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.remote = _load_server(local=False)
        cls.local = _load_server(local=True)

    def test_access_request_never_returns_exchange_secret_to_agent(self) -> None:
        response = {
            "state": "opaque-state",
            "exchange_secret": "private-exchange-secret",
            "authorize_path": "/mcp/authorize?state=opaque-state",
            "expires_at": "2026-08-10T12:10:00Z",
        }
        with (
            mock.patch.object(
                self.remote,
                "_remote_post",
                return_value=response,
            ) as post,
            mock.patch.object(
                self.remote,
                "get_token",
                return_value="praxys_mcp_session",
            ),
            mock.patch.object(
                self.remote,
                "save_pending_context_access",
            ) as save_pending,
        ):
            result = json.loads(
                self.remote.request_personal_context_access(
                    purpose="plan_adjustment",
                    kind="temporary_constraint",
                    access=["read", "write"],
                )
            )

        self.assertEqual(
            post.call_args,
            mock.call(
                "/api/personal-context/scoped-access/requests",
                {
                    "audience": "praxys-coach-plugin",
                    "purpose": "plan_adjustment",
                    "kind": "temporary_constraint",
                    "access": ["read", "write"],
                },
            ),
        )
        save_pending.assert_called_once_with({
            "state": "opaque-state",
            "exchange_secret": "private-exchange-secret",
            "expires_at": "2026-08-10T12:10:00Z",
        })
        self.assertEqual(
            result["authorization_url"],
            "https://www.praxys.run/mcp/authorize?state=opaque-state",
        )
        self.assertNotIn("exchange_secret", result)
        self.assertNotIn("private-exchange-secret", json.dumps(result))

    def test_login_deep_link_contains_state_not_an_account_bearer(self) -> None:
        handoff = {
            "state": "opaque-login-state",
            "exchange_secret": "client-held-verifier",
            "authorize_path": "/mcp/authorize?state=opaque-login-state",
            "expires_at": "2026-08-10T12:10:00Z",
        }
        exchanged = {
            "access_token": "praxys_mcp_session",
            "expires_at": "2026-08-11T12:00:00Z",
        }
        me = mock.Mock(status_code=200, ok=True)
        me.json.return_value = {
            "id": "owner-id",
            "email": "owner@example.test",
            "is_superuser": False,
        }
        scope = mock.Mock(profile="default")
        with (
            mock.patch.object(
                self.remote,
                "_public_post",
                side_effect=[handoff, exchanged],
            ) as public_post,
            mock.patch("webbrowser.open") as browser_open,
            mock.patch("requests.get", return_value=me),
            mock.patch.object(
                self.remote,
                "save_token",
                return_value=Path("token"),
            ) as save_token,
            mock.patch.object(self.remote, "save_config"),
        ):
            result = json.loads(
                self.remote._opaque_browser_login(scope)
            )

        opened = browser_open.call_args.args[0]
        self.assertEqual(
            opened,
            "https://www.praxys.run/mcp/authorize?state=opaque-login-state",
        )
        self.assertNotIn("token=", opened)
        self.assertNotIn("praxys_mcp_session", opened)
        self.assertNotIn("client-held-verifier", opened)
        save_token.assert_called_once_with("praxys_mcp_session")
        self.assertEqual(result["status"], "authenticated")
        self.assertEqual(
            public_post.call_args_list,
            [
                mock.call(
                    "/api/auth/mcp/handoffs",
                    {"audience": "praxys-coach-plugin"},
                ),
                mock.call(
                    "/api/auth/mcp/handoffs/exchange",
                    {
                        "state": "opaque-login-state",
                        "exchange_secret": "client-held-verifier",
                    },
                ),
            ],
        )

    def test_frontend_handoffs_reject_redirects_outside_configured_origin(
        self,
    ) -> None:
        with self.assertRaisesRegex(RuntimeError, "invalid authorization"):
            self.remote._frontend_link("//attacker.example/callback")
        with (
            mock.patch.object(
                self.remote,
                "FRONTEND_URL",
                "https://www.praxys.run/embedded/path",
            ),
            self.assertRaisesRegex(RuntimeError, "HTTP\\(S\\) origin"),
        ):
            self.remote._frontend_link("/mcp/authorize?state=opaque")

    def test_complete_access_caches_only_the_bounded_context_token(self) -> None:
        pending = {
            "state": "opaque-state",
            "exchange_secret": "private-exchange-secret",
            "expires_at": "2026-08-10T12:10:00Z",
        }
        exchanged = {
            "access_token": "praxys_ctx_secret",
            "token_type": "bearer",
            "expires_at": "2026-08-10T12:15:00Z",
            "purpose": "plan_adjustment",
            "kind": "temporary_constraint",
            "access": ["read", "write"],
        }
        with (
            mock.patch.object(
                self.remote,
                "get_pending_context_access",
                return_value=pending,
            ),
            mock.patch.object(
                self.remote,
                "_public_post",
                return_value=exchanged,
            ) as post,
            mock.patch.object(
                self.remote,
                "save_context_token",
            ) as save_token,
            mock.patch.object(
                self.remote,
                "clear_pending_context_access",
            ) as clear_pending,
        ):
            result = json.loads(
                self.remote.complete_personal_context_access()
            )

        post.assert_called_once_with(
            "/api/auth/mcp/handoffs/exchange",
            {
                "state": "opaque-state",
                "exchange_secret": "private-exchange-secret",
            },
        )
        save_token.assert_called_once_with("praxys_ctx_secret")
        clear_pending.assert_called_once_with()
        self.assertEqual(result["status"], "authorized")
        self.assertNotIn("access_token", result)

    def test_access_request_rejects_kind_purpose_broadening_locally(self) -> None:
        with mock.patch.object(self.remote, "_remote_post") as post:
            result = json.loads(
                self.remote.request_personal_context_access(
                    purpose="plan_generation",
                    kind="execution_explanation",
                    access=["read"],
                )
            )

        self.assertEqual(result["status"], "error")
        self.assertIn("not available", result["message"])
        post.assert_not_called()

    def test_structured_read_matches_remote_and_local_modes(self) -> None:
        projection = {
            "items": [{
                "kind": "temporary_constraint",
                "purpose": "plan_adjustment",
                "category": "less_time",
                "fields": {"maximum_available_minutes": 30},
                "starts_at": "2026-08-10T00:00:00Z",
                "expires_at": "2026-08-12T00:00:00Z",
            }],
        }
        with mock.patch.object(
            self.remote,
            "_context_remote_get",
            return_value=projection,
        ) as remote_get:
            remote = json.loads(self.remote.read_personal_context())
        with mock.patch.object(
            self.local,
            "_local_read_personal_context",
            return_value=projection,
        ):
            local = json.loads(self.local.read_personal_context())

        self.assertEqual(remote, local)
        remote_get.assert_called_once_with(
            "/api/personal-context/scoped/selection"
        )
        serialized = json.dumps(remote)
        for forbidden in (
            "narrative",
            "encrypted_payload",
            "source_actor_id",
            "consent_receipt_id",
            "lineage_id",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_structured_preview_matches_modes_and_never_accepts_narrative(
        self,
    ) -> None:
        preview = {
            "kind": "temporary_constraint",
            "purpose": "plan_adjustment",
            "payload": {
                "category": "less_time",
                "fields": {"maximum_available_minutes": 30},
            },
            "starts_at": "2026-08-10T00:00:00Z",
            "expires_at": "2026-08-12T00:00:00Z",
            "purge_after": "2026-09-11T00:00:00Z",
            "confirmation_required": True,
            "confirmation_path": "/training#plan-context",
            "miniapp_path": "/pages/training/index",
        }
        kwargs = {
            "kind": "temporary_constraint",
            "purpose": "plan_adjustment",
            "category": "less_time",
            "fields": {"maximum_available_minutes": 30},
            "starts_at": "2026-08-10T00:00:00Z",
            "expires_at": "2026-08-12T00:00:00Z",
            "purge_after": "2026-09-11T00:00:00Z",
        }
        with mock.patch.object(
            self.remote,
            "_context_remote_post",
            return_value=preview,
        ) as remote_post:
            remote = json.loads(
                self.remote.preview_personal_context(**kwargs)
            )
        with mock.patch.object(
            self.local,
            "_local_preview_personal_context",
            return_value=preview,
        ):
            local = json.loads(
                self.local.preview_personal_context(**kwargs)
            )

        self.assertEqual(
            remote["confirmation_url"],
            "https://www.praxys.run/training#plan-context",
        )
        self.assertEqual(
            local["confirmation_url"],
            "http://localhost:5173/training#plan-context",
        )
        self.assertEqual(
            {key: value for key, value in remote.items() if key != "confirmation_url"},
            {key: value for key, value in local.items() if key != "confirmation_url"},
        )
        sent = remote_post.call_args.args[1]
        self.assertNotIn("narrative", sent["payload"])
        self.assertTrue(remote["confirmation_required"])

    def test_revoke_clears_context_token_in_both_modes(self) -> None:
        with (
            mock.patch.object(
                self.remote,
                "_context_remote_post",
                return_value={"status": "revoked"},
            ),
            mock.patch.object(
                self.remote,
                "clear_context_token",
            ) as remote_clear,
        ):
            remote = json.loads(
                self.remote.revoke_personal_context_access()
            )
        with (
            mock.patch.object(
                self.local,
                "_local_revoke_personal_context_access",
                return_value={"status": "revoked"},
            ),
            mock.patch.object(
                self.local,
                "clear_context_token",
            ) as local_clear,
        ):
            local = json.loads(
                self.local.revoke_personal_context_access()
            )

        self.assertEqual(remote, local)
        remote_clear.assert_called_once_with()
        local_clear.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
