import logging
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from sketchup_mcp import http_server, server


async def _ok(_request):
    return PlainTextResponse("ok")


async def _health(_request):
    return PlainTextResponse("healthy")


class HttpServerTests(unittest.TestCase):
    def _preserve_http_loggers(self):
        targets = http_server._http_log_targets()
        return {
            target: (list(target.handlers), target.propagate, target.level)
            for target in targets
        }

    def _restore_http_loggers(self, state):
        added_handlers = set()
        for target, (handlers, propagate, level) in state.items():
            for handler in list(target.handlers):
                if handler not in handlers:
                    target.removeHandler(handler)
                    added_handlers.add(handler)
            for handler in handlers:
                if handler not in target.handlers:
                    target.addHandler(handler)
            target.propagate = propagate
            target.setLevel(level)
        for handler in added_handlers:
            handler.close()

    def test_http_defaults_and_loopback_validation(self):
        args = http_server.parse_args([])

        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.http_port, 8765)
        self.assertEqual(args.sketchup_port, 9876)
        self.assertEqual(args.session_idle_timeout, 1800.0)
        self.assertEqual(http_server.validate_loopback_host("::1"), "::1")
        with self.assertRaisesRegex(ValueError, "loopback"):
            http_server.validate_loopback_host("0.0.0.0")

    def test_missing_static_token_rejects_http_startup(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "SKETCHUP_MCP_HTTP_TOKEN"):
                http_server.required_http_token()

    def test_bearer_middleware_protects_mcp_but_not_health_check(self):
        app = Starlette(routes=[Route("/mcp", _ok, methods=["POST"]), Route("/healthz", _health)])
        app.add_middleware(http_server.StaticBearerTokenMiddleware, token="secret")
        client = TestClient(app)

        self.assertEqual(client.get("/healthz").status_code, 200)
        rejected = client.post("/mcp")
        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(rejected.headers["www-authenticate"], "Bearer")
        self.assertEqual(client.post("/mcp", headers={"Authorization": "Bearer secret"}).status_code, 200)

    def test_http_daemon_log_rotates_and_keeps_existing_console_handlers(self):
        root_handlers = tuple(logging.getLogger().handlers)
        token = "do-not-log-this-token"
        state = self._preserve_http_loggers()
        with tempfile.TemporaryDirectory() as temporary_directory:
            try:
                log_path = Path(temporary_directory) / "http-daemon.log"
                with patch.dict(os.environ, {http_server.HTTP_TOKEN_ENV: token}, clear=False):
                    logger = http_server.configure_http_logging(log_path)
                    logger.info("HTTP daemon logging test")
                for handler in logger.handlers:
                    handler.flush()

                handler = next(item for item in logger.handlers if getattr(item, "baseFilename", None) == str(log_path))
                self.assertEqual(handler.maxBytes, 5 * 1024 * 1024)
                self.assertEqual(handler.backupCount, 5)
                self.assertEqual(handler.encoding.lower(), "utf-8")
                self.assertEqual(log_path.read_text(encoding="utf-8").count("HTTP daemon logging test"), 1)
                self.assertNotIn(token, log_path.read_text(encoding="utf-8"))
                self.assertTrue(all(item in logging.getLogger().handlers for item in root_handlers))
                self.assertIn(handler, logging.getLogger().handlers)
            finally:
                self._restore_http_loggers(state)

    def test_http_logging_records_startup_auth_server_mcp_and_uvicorn_without_token(self):
        state = self._preserve_http_loggers()
        token = "never-write-this-token"
        with tempfile.TemporaryDirectory() as local_app_data:
            try:
                log_path = Path(local_app_data) / "SketchUpMCP" / "logs" / "http-daemon.log"
                with (
                    patch.dict(
                        os.environ,
                        {http_server.HTTP_TOKEN_ENV: token, "LOCALAPPDATA": local_app_data},
                        clear=False,
                    ),
                    patch.object(http_server, "create_http_app", return_value=object()),
                    patch("uvicorn.run"),
                ):
                    http_server.main(["--http-port", "8766"])

                app = Starlette(routes=[Route("/mcp", _ok, methods=["POST"])])
                app.add_middleware(http_server.StaticBearerTokenMiddleware, token=token)
                self.assertEqual(TestClient(app).post("/mcp").status_code, 401)
                server.logger.warning("SketchupMCPServer logging test")
                logging.getLogger("mcp.server.streamable_http_manager").warning("MCP lifecycle logging test")
                logging.getLogger("uvicorn.error").warning("Uvicorn logging test")
                for handler in logging.getLogger(http_server.HTTP_LOGGER_NAME).handlers:
                    handler.flush()

                contents = log_path.read_text(encoding="utf-8")
                for message in (
                    "Starting loopback Streamable HTTP service on 127.0.0.1:8766",
                    "Rejected unauthenticated HTTP MCP request on /mcp",
                    "SketchupMCPServer logging test",
                    "MCP lifecycle logging test",
                    "Uvicorn logging test",
                ):
                    self.assertIn(message, contents)
                self.assertNotIn(token, contents)
            finally:
                self._restore_http_loggers(state)

    def test_stateful_streamable_http_exposes_health_sessions_cleanup_and_idle_reclamation(self):
        with patch.dict(os.environ, {http_server.HTTP_TOKEN_ENV: "secret"}, clear=False):
            app = http_server.create_http_app(sketchup_port=9877)
        manager = server.mcp.session_manager
        self.assertEqual(manager.session_idle_timeout, 1800.0)

        def initialize(client, request_id):
            response = client.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer secret",
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
            )
            self.assertEqual(response.status_code, 200)
            return response.headers["mcp-session-id"]

        def headers(session_id):
            return {
                "Authorization": "Bearer secret",
                "Mcp-Session-Id": session_id,
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            }

        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            health = client.get("/healthz").json()
            self.assertEqual(health["status"], "ok")
            self.assertEqual(health["server_version"], server.__version__)
            self.assertGreaterEqual(health["uptime_seconds"], 0)
            self.assertEqual(client.post("/mcp").status_code, 401)
            first_session = initialize(client, 1)
            second_session = initialize(client, 2)
            self.assertEqual(len(manager._server_instances), 2)

            for request_id, session_id, port in ((3, first_session, 9877), (4, second_session, 9878)):
                tool_call = client.post(
                    "/mcp",
                    headers=headers(session_id),
                    json={
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {"name": "set_connection_port", "arguments": {"port": port}},
                    },
                )
                self.assertEqual(tool_call.status_code, 200)
                self.assertTrue(tool_call.headers["content-type"].startswith("text/event-stream"))

            for session_id in (first_session, second_session):
                terminated = client.delete(
                    "/mcp",
                    headers={
                        "Authorization": "Bearer secret",
                        "Mcp-Session-Id": session_id,
                        "Accept": "application/json",
                    },
                )
                self.assertEqual(terminated.status_code, 200)
                self.assertNotIn(session_id, manager._server_instances)
                self.assertNotIn(session_id, manager._session_owners)

            self.assertEqual(manager._server_instances, {})
            manager.session_idle_timeout = 0.05
            idle_session = initialize(client, 5)
            deadline = time.monotonic() + 2.0
            while idle_session in manager._server_instances and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertNotIn(idle_session, manager._server_instances)

        self.assertEqual(manager._server_instances, {})

    def test_cli_forwards_fixed_options_without_starting_stdio_watchdog(self):
        app = object()
        with tempfile.TemporaryDirectory() as local_app_data:
            with (
                patch.dict(
                    os.environ,
                    {http_server.HTTP_TOKEN_ENV: "secret", "LOCALAPPDATA": local_app_data},
                    clear=False,
                ),
                patch.object(http_server, "create_http_app", return_value=app) as create_app,
                patch.object(http_server, "configure_http_logging") as configure_logging,
                patch("uvicorn.run") as run,
            ):
                http_server.main(
                    [
                        "--http-port", "8766",
                        "--sketchup-port", "9877",
                        "--allow-autostart",
                        "--sketchup-executable", "C:/SketchUp/SketchUp.exe",
                        "--startup-timeout", "30",
                        "--request-timeout-ms", "12000",
                        "--session-idle-timeout", "90",
                    ]
                )

        create_app.assert_called_once_with(
            sketchup_port=9877,
            allow_autostart=True,
            sketchup_executable="C:/SketchUp/SketchUp.exe",
            startup_timeout=30.0,
            request_timeout_ms=12000,
            session_idle_timeout=90.0,
        )
        configure_logging.assert_called_once_with()
        run.assert_called_once_with(app, host="127.0.0.1", port=8766, log_config=None)

    def test_cli_rejects_non_positive_session_idle_timeout(self):
        with self.assertRaises(SystemExit):
            http_server.parse_args(["--session-idle-timeout", "0"])
