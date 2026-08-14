"""Local MCP startup regression tests."""

from __future__ import annotations

import importlib
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

    def run(self) -> None:
        _EVENTS.append("run")


_EVENTS: list[str] = []


class LocalStartupTests(unittest.TestCase):
    def setUp(self) -> None:
        _EVENTS.clear()

    def _run_server_main(self, *, local: bool) -> None:
        mcp_module = ModuleType("mcp")
        mcp_server_module = ModuleType("mcp.server")
        fastmcp_module = ModuleType("mcp.server.fastmcp")
        fastmcp_module.FastMCP = _FakeFastMCP
        main_module = ModuleType("__main__")
        main_module.__file__ = str(SERVER_PATH)
        statsig_module = ModuleType("api.statsig_client")

        async def init_statsig() -> None:
            _EVENTS.append("statsig-init")

        async def shutdown_statsig() -> None:
            _EVENTS.append("statsig-shutdown")

        statsig_module.init_statsig = init_statsig
        statsig_module.shutdown_statsig = shutdown_statsig

        def record_import(module_name: str) -> ModuleType:
            _EVENTS.append(module_name)
            return ModuleType(module_name)

        source = compile(
            SERVER_PATH.read_text(encoding="utf-8"),
            str(SERVER_PATH),
            "exec",
        )
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
                    "api.statsig_client": statsig_module,
                },
            ),
            mock.patch.object(
                importlib,
                "import_module",
                side_effect=record_import,
            ),
        ):
            exec(source, main_module.__dict__)

    def test_local_main_preloads_host_modules_before_fastmcp(self) -> None:
        self._run_server_main(local=True)

        self.assertEqual(
            _EVENTS,
            [
                "api.deps",
                "api.routes.plan",
                "api.routes.settings",
                "statsig-init",
                "run",
                "statsig-shutdown",
            ],
        )

    def test_remote_main_skips_local_host_modules(self) -> None:
        self._run_server_main(local=False)

        self.assertEqual(_EVENTS, ["run"])


if __name__ == "__main__":
    unittest.main()
