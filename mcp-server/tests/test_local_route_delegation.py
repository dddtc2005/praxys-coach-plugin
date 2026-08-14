"""Local MCP delegation tests with no private-repository dependency."""

from __future__ import annotations

import importlib.util
from contextlib import contextmanager
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


def _load_server():
    mcp_module = ModuleType("mcp")
    mcp_server_module = ModuleType("mcp.server")
    fastmcp_module = ModuleType("mcp.server.fastmcp")
    fastmcp_module.FastMCP = _FakeFastMCP
    module_name = "praxys_local_route_server_test"
    spec = importlib.util.spec_from_file_location(module_name, SERVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with (
        mock.patch.dict(
            os.environ,
            {"PRAXYS_LOCAL": "0", "TRAINSIGHT_LOCAL": "0"},
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


def _module(name: str, **attributes) -> ModuleType:
    module = ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


class LocalRouteDelegationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _load_server()

    @contextmanager
    def _base_patches(self, db):
        with (
            mock.patch.object(self.server, "IS_REMOTE", False),
            mock.patch.object(self.server, "_local_db", return_value=db),
            mock.patch.object(
                self.server,
                "_local_route_result",
                side_effect=lambda callback: callback(),
            ),
        ):
            yield

    def test_local_settings_passes_viewer_and_data_user(self) -> None:
        db = mock.Mock()
        route = mock.Mock(return_value={"config": {}})
        settings_module = _module(
            "api.routes.settings",
            get_settings=route,
        )
        with (
            self._base_patches(db),
            mock.patch.object(
                self.server,
                "_local_user_id",
                return_value="viewer-user",
            ),
            mock.patch.object(
                self.server,
                "_local_data_user_id",
                return_value="data-user",
            ),
            mock.patch.dict(
                sys.modules,
                {"api.routes.settings": settings_module},
            ),
        ):
            result = self.server._local_get_settings()

        self.assertEqual(result, {"config": {}})
        route.assert_called_once_with(
            viewer_user_id="viewer-user",
            user_id="data-user",
            db=db,
        )
        db.close.assert_called_once_with()

    def test_local_plan_passes_viewer_and_data_user(self) -> None:
        db = mock.Mock()
        route = mock.Mock(return_value={"workouts": []})
        plan_module = _module("api.routes.plan", get_plan=route)

        class _Request:
            def __init__(self, scope) -> None:
                self.scope = scope

        class _Response:
            pass

        with (
            self._base_patches(db),
            mock.patch.object(
                self.server,
                "_local_user_id",
                return_value="viewer-user",
            ),
            mock.patch.object(
                self.server,
                "_local_data_user_id",
                return_value="data-user",
            ),
            mock.patch.dict(
                sys.modules,
                {
                    "api.routes.plan": plan_module,
                    "starlette.requests": _module(
                        "starlette.requests",
                        Request=_Request,
                    ),
                    "starlette.responses": _module(
                        "starlette.responses",
                        Response=_Response,
                    ),
                },
            ),
        ):
            result = self.server._local_get_plan(
                "2026-08-01",
                "2026-08-14",
            )

        self.assertEqual(result, {"workouts": []})
        self.assertEqual(route.call_args.kwargs["viewer_user_id"], "viewer-user")
        self.assertEqual(route.call_args.kwargs["user_id"], "data-user")
        self.assertIs(route.call_args.kwargs["db"], db)
        db.close.assert_called_once_with()

    def test_local_connections_use_filtered_settings_route(self) -> None:
        db = mock.Mock()
        route = mock.Mock(return_value={"connections": {"garmin": {}}})
        settings_module = _module(
            "api.routes.settings",
            get_connections=route,
        )
        with (
            self._base_patches(db),
            mock.patch.object(
                self.server,
                "_local_user_id",
                return_value="viewer-user",
            ),
            mock.patch.object(
                self.server,
                "_local_data_user_id",
                return_value="data-user",
            ),
            mock.patch.dict(
                sys.modules,
                {"api.routes.settings": settings_module},
            ),
        ):
            result = json.loads(self.server.get_connections())

        self.assertEqual(result, {"connections": {"garmin": {}}})
        route.assert_called_once_with(
            viewer_user_id="viewer-user",
            user_id="data-user",
            db=db,
        )

    def test_local_connection_mutations_use_host_routes(self) -> None:
        db = mock.Mock()

        class _ConnectPlatformRequest:
            def __init__(self, **payload) -> None:
                self.payload = payload

        connect_route = mock.Mock(
            return_value={"status": "connected", "platform": "garmin"},
        )
        disconnect_route = mock.Mock(
            return_value={"status": "disconnected", "platform": "garmin"},
        )
        settings_module = _module(
            "api.routes.settings",
            ConnectPlatformRequest=_ConnectPlatformRequest,
            connect_platform=connect_route,
            disconnect_platform=disconnect_route,
        )
        with (
            self._base_patches(db),
            mock.patch.object(
                self.server,
                "_local_write_user_id",
                return_value="writer-user",
            ),
            mock.patch.dict(
                sys.modules,
                {"api.routes.settings": settings_module},
            ),
        ):
            connected = json.loads(
                self.server.connect_platform(
                    "garmin",
                    {"email": "athlete@example.test", "password": "secret"},
                )
            )
            disconnected = json.loads(
                self.server.disconnect_platform("garmin")
            )

        self.assertEqual(connected["status"], "connected")
        self.assertEqual(disconnected["status"], "disconnected")
        connect_call = connect_route.call_args.kwargs
        self.assertEqual(connect_call["platform"], "garmin")
        self.assertEqual(connect_call["user_id"], "writer-user")
        self.assertEqual(
            connect_call["body"].payload,
            {"email": "athlete@example.test", "password": "secret"},
        )
        self.assertIs(connect_call["db"], db)
        disconnect_route.assert_called_once_with(
            platform="garmin",
            user_id="writer-user",
            db=db,
        )


if __name__ == "__main__":
    unittest.main()
