"""Authentication-profile isolation tests for the Praxys MCP server."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest import mock


MCP_SERVER_DIR = Path(__file__).resolve().parents[1]
AUTH_COMPAT_PATH = MCP_SERVER_DIR / "auth.py"
SERVER_PATH = MCP_SERVER_DIR / "server.py"

if str(MCP_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_SERVER_DIR))

import auth


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
    module_name = f"praxys_auth_profile_server_test_{int(local)}"
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


class AuthProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name)
        self.paths_patch = mock.patch.multiple(
            auth,
            CONFIG_DIR=str(self.home / ".praxys"),
            LEGACY_CONFIG_DIR=str(self.home / ".trainsight"),
            TOKEN_PATH=str(self.home / ".praxys" / "token"),
            CONFIG_PATH=str(self.home / ".praxys" / "config.json"),
            _LEGACY_TOKEN_PATH=str(
                self.home / ".trainsight" / "token"
            ),
            _LEGACY_CONFIG_PATH=str(
                self.home / ".trainsight" / "config.json"
            ),
        )
        self.paths_patch.start()
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "PRAXYS_PROFILE": "",
                "PRAXYS_TOKEN_PATH": "",
            },
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.paths_patch.stop()
        self.tempdir.cleanup()

    def test_default_profile_reads_modern_then_legacy_fallback(self) -> None:
        legacy_dir = self.home / ".trainsight"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "token").write_text(
            "legacy-token",
            encoding="utf-8",
        )
        (legacy_dir / "config.json").write_text(
            '{"email": "legacy@example.test"}',
            encoding="utf-8",
        )

        self.assertEqual(auth.get_token(), "legacy-token")
        self.assertEqual(
            auth.get_config(),
            {"email": "legacy@example.test"},
        )

        auth.save_token("modern-token")
        auth.save_config({"email": "modern@example.test"})

        self.assertEqual(auth.get_token(), "modern-token")
        self.assertEqual(
            auth.get_config(),
            {"email": "modern@example.test"},
        )

    def test_legacy_auth_module_loads_directly_and_keeps_path_constants(
        self,
    ) -> None:
        module_name = "praxys_legacy_auth_compat_test"
        spec = importlib.util.spec_from_file_location(
            module_name,
            AUTH_COMPAT_PATH,
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {module_name: module}):
            cached_auth = sys.modules.pop("praxys_auth", None)
            try:
                spec.loader.exec_module(module)
            finally:
                if cached_auth is not None:
                    sys.modules["praxys_auth"] = cached_auth

        self.assertTrue(module.TOKEN_PATH.endswith(
            os.path.join(".praxys", "token")
        ))
        self.assertTrue(module.CONFIG_PATH.endswith(
            os.path.join(".praxys", "config.json")
        ))
        self.assertTrue(callable(module.get_token))

    def test_named_profile_uses_isolated_paths_without_fallback(self) -> None:
        default_dir = self.home / ".praxys"
        legacy_dir = self.home / ".trainsight"
        default_dir.mkdir(parents=True)
        legacy_dir.mkdir(parents=True)
        (default_dir / "token").write_text(
            "default-token",
            encoding="utf-8",
        )
        (legacy_dir / "token").write_text(
            "legacy-token",
            encoding="utf-8",
        )

        os.environ["PRAXYS_PROFILE"] = "dev-test"
        self.assertIsNone(auth.get_token())
        self.assertEqual(auth.get_config(), {})

        token_path = auth.save_token("dev-token")
        auth.save_config({"email": "dev@example.test"})
        scope = auth.get_auth_scope()

        self.assertEqual(scope.profile, "dev-test")
        self.assertEqual(
            token_path,
            self.home / ".praxys" / "profiles" / "dev-test" / "token",
        )
        self.assertEqual(auth.get_token(), "dev-token")
        self.assertEqual(
            auth.get_config(),
            {"email": "dev@example.test"},
        )
        self.assertEqual(
            (default_dir / "token").read_text(encoding="utf-8"),
            "default-token",
        )

    def test_invalid_profile_names_are_rejected(self) -> None:
        for profile in ("..", "../dev", "dev/test", r"dev\test", ".dev"):
            with self.subTest(profile=profile):
                os.environ["PRAXYS_PROFILE"] = profile
                with self.assertRaisesRegex(ValueError, "PRAXYS_PROFILE"):
                    auth.get_auth_scope()

    def test_explicit_token_path_overrides_profile_storage(self) -> None:
        os.environ["PRAXYS_PROFILE"] = "dev-test"
        token_path = self.home / "custom" / "dev.jwt"
        os.environ["PRAXYS_TOKEN_PATH"] = str(token_path)

        saved_path = auth.save_token("explicit-token")
        auth.save_config({"url": "https://api.example.test"})

        self.assertEqual(saved_path, token_path)
        self.assertEqual(auth.get_token(), "explicit-token")
        self.assertEqual(
            auth.get_auth_scope().config_path,
            token_path.with_name("dev.jwt.config.json"),
        )
        self.assertFalse(
            (
                self.home
                / ".praxys"
                / "profiles"
                / "dev-test"
                / "token"
            ).exists()
        )

    def test_logout_deletes_only_the_active_scope(self) -> None:
        auth.save_token("default-token")
        auth.save_config({"email": "default@example.test"})
        auth.save_context_token("default-context-token")
        auth.save_pending_context_access({
            "state": "default-state",
            "exchange_secret": "default-secret",
        })
        default_scope = auth.get_auth_scope()

        os.environ["PRAXYS_PROFILE"] = "dev-test"
        auth.save_token("dev-token")
        auth.save_config({"email": "dev@example.test"})
        auth.save_context_token("dev-context-token")
        auth.save_pending_context_access({
            "state": "dev-state",
            "exchange_secret": "dev-secret",
        })
        dev_scope = auth.get_auth_scope()

        result = auth.logout()

        self.assertCountEqual(
            result.removed_paths,
            [
                dev_scope.token_path,
                dev_scope.config_path,
                dev_scope.context_token_path,
                dev_scope.pending_context_path,
            ],
        )
        self.assertFalse(result.legacy_fallback_suppressed)
        self.assertTrue(default_scope.token_path.exists())
        self.assertTrue(default_scope.config_path.exists())
        self.assertTrue(default_scope.context_token_path.exists())
        self.assertTrue(default_scope.pending_context_path.exists())
        self.assertFalse(dev_scope.token_path.exists())
        self.assertFalse(dev_scope.config_path.exists())
        self.assertFalse(dev_scope.context_token_path.exists())
        self.assertFalse(dev_scope.pending_context_path.exists())

    def test_default_logout_suppresses_without_deleting_legacy_auth(self) -> None:
        legacy_dir = self.home / ".trainsight"
        legacy_dir.mkdir(parents=True)
        legacy_token = legacy_dir / "token"
        legacy_config = legacy_dir / "config.json"
        legacy_token.write_text("legacy-token", encoding="utf-8")
        legacy_config.write_text("{}", encoding="utf-8")

        result = auth.logout()

        self.assertEqual(result.removed_paths, ())
        self.assertTrue(result.legacy_fallback_suppressed)
        self.assertTrue(legacy_token.exists())
        self.assertTrue(legacy_config.exists())
        self.assertIsNone(auth.get_token())
        self.assertEqual(auth.get_config(), {})

        auth.save_token("new-token")
        self.assertEqual(auth.get_token(), "new-token")

    def test_local_trigger_sync_uses_only_the_named_profile_token(self) -> None:
        auth.save_token("default-token")
        os.environ["PRAXYS_PROFILE"] = "dev-test"
        auth.save_token("dev-token")
        os.environ["PRAXYS_TOKEN_PATH"] = str(
            auth.get_auth_scope().token_path
        )
        server = _load_server(local=True)
        response = mock.Mock()
        response.json.return_value = {"status": "started"}

        with mock.patch("requests.post", return_value=response) as post:
            data = json.loads(server.trigger_sync(["garmin"]))

        self.assertEqual(data, {
            "results": [{"source": "garmin", "status": "started"}],
        })
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            "Bearer dev-token",
        )

        auth.logout()
        with mock.patch("requests.post") as missing_post:
            missing = json.loads(server.trigger_sync(["garmin"]))

        self.assertEqual(missing["status"], "not_authenticated")
        self.assertIn("dev-test", missing["message"])
        missing_post.assert_not_called()

    def test_whoami_and_logout_report_the_active_profile(self) -> None:
        os.environ["PRAXYS_PROFILE"] = "dev-test"
        auth.save_token("dev-token")
        os.environ["PRAXYS_TOKEN_PATH"] = str(
            auth.get_auth_scope().token_path
        )
        server = _load_server(local=False)
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "id": "test-user-id",
            "email": "dev@example.test",
            "is_superuser": False,
        }

        with mock.patch("requests.get", return_value=response):
            identity = json.loads(server.whoami())

        self.assertEqual(identity["mode"], "remote")
        self.assertEqual(identity["profile"], "dev-test")
        self.assertEqual(identity["user_id"], "test-user-id")
        self.assertEqual(identity["email"], "dev@example.test")

        logged_out = json.loads(server.logout())
        self.assertEqual(logged_out["status"], "logged_out")
        self.assertEqual(logged_out["profile"], "dev-test")
        self.assertIsNone(auth.get_token())


if __name__ == "__main__":
    unittest.main()
