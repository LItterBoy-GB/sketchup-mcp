import unittest
import asyncio
import json
import socket
from unittest.mock import AsyncMock, patch

from sketchup_mcp import server


class ConnectionConfigTests(unittest.TestCase):
    def tearDown(self):
        server.clear_sketchup_port_override()
        server.startup.clear_session_autostart_allowed()
        server._active_request_count = 0
        server._last_activity_monotonic = 0.0

    def test_default_connection_target_uses_default_port(self):
        self.assertEqual(server.get_sketchup_host(), "localhost")
        self.assertEqual(server.get_sketchup_port(), 9876)

    def test_connection_target_uses_environment_port(self):
        with patch.dict("os.environ", {"SKETCHUP_MCP_PORT": "9877"}):
            self.assertEqual(server.get_sketchup_port(), 9877)

    def test_connection_target_rejects_invalid_environment_port(self):
        with patch.dict("os.environ", {"SKETCHUP_MCP_PORT": "70000"}):
            with self.assertRaisesRegex(ValueError, "SKETCHUP_MCP_PORT"):
                server.get_sketchup_port()

    def test_idle_timeout_defaults_to_positive_value(self):
        self.assertEqual(server.get_idle_timeout_sec(), server.DEFAULT_IDLE_TIMEOUT_SEC)

    def test_idle_timeout_can_be_configured_or_disabled(self):
        with patch.dict("os.environ", {"SKETCHUP_MCP_IDLE_TIMEOUT_SEC": "120"}):
            self.assertEqual(server.get_idle_timeout_sec(), 120.0)

        with patch.dict("os.environ", {"SKETCHUP_MCP_IDLE_TIMEOUT_SEC": "0"}):
            self.assertEqual(server.get_idle_timeout_sec(), 0.0)

    def test_idle_timeout_rejects_invalid_values(self):
        with patch.dict("os.environ", {"SKETCHUP_MCP_IDLE_TIMEOUT_SEC": "-1"}):
            with self.assertRaisesRegex(ValueError, "SKETCHUP_MCP_IDLE_TIMEOUT_SEC"):
                server.get_idle_timeout_sec()

    def test_idle_timeout_waits_for_inactive_server(self):
        server._last_activity_monotonic = 100.0
        server._active_request_count = 0

        self.assertFalse(server._server_is_idle_too_long(now=150.0, timeout_sec=60.0))
        self.assertTrue(server._server_is_idle_too_long(now=161.0, timeout_sec=60.0))

        server._active_request_count = 1
        self.assertFalse(server._server_is_idle_too_long(now=1_000.0, timeout_sec=60.0))

    def test_connection_target_can_be_changed_for_current_mcp_process(self):
        with patch.dict("os.environ", {"SKETCHUP_MCP_PORT": "9876"}):
            server.set_sketchup_port_override(9877)

            self.assertEqual(server.get_sketchup_port(), 9877)

    def test_explicit_request_port_overrides_session_and_environment(self):
        with patch.dict("os.environ", {"SKETCHUP_MCP_PORT": "9876"}):
            server.set_sketchup_port_override(9877)
            self.assertEqual(server.get_sketchup_port(9878), 9878)

    def test_set_connection_port_tool_returns_current_target(self):
        result = server.set_connection_port(None, 9878)

        self.assertIn('"success": true', result)
        self.assertIn('"port": 9878', result)
        self.assertEqual(server.get_sketchup_port(), 9878)

    def test_connection_failure_does_not_autostart_without_permission(self):
        connection = server.SketchupConnection(host="localhost", port=9879)

        with (
            patch.object(connection, "_connect_once", return_value=False) as connect_once,
            patch("sketchup_mcp.server.startup.launch_sketchup") as launch,
        ):
            self.assertFalse(connection.connect())

        launch.assert_not_called()
        self.assertEqual(connect_once.call_count, 1)

    def test_connection_failure_can_trigger_autostart_after_permission(self):
        connection = server.SketchupConnection(host="localhost", port=9879)
        server.startup.set_session_autostart_allowed(True)

        with (
            patch.object(connection, "_connect_once", side_effect=[False, False]) as connect_once,
            patch("sketchup_mcp.server.startup.launch_sketchup") as launch,
            patch("sketchup_mcp.server.startup.get_startup_timeout", return_value=0),
        ):
            self.assertFalse(connection.connect())

        launch.assert_called_once_with(9879)
        self.assertEqual(connect_once.call_count, 2)

    def test_connection_probe_can_disable_autostart(self):
        connection = server.SketchupConnection(host="localhost", port=9879)

        with (
            patch.object(connection, "_connect_once", return_value=False),
            patch("sketchup_mcp.server.startup.maybe_start_sketchup") as autostart,
        ):
            self.assertFalse(connection.connect(allow_autostart=False))

        autostart.assert_not_called()

    def test_allow_sketchup_autostart_tool_records_user_permission(self):
        result = server.allow_sketchup_autostart(None, True)

        self.assertIn('"success": true', result)
        self.assertIn('"autostart_allowed": true', result)
        self.assertTrue(server.startup.autostart_allowed())

    def test_connection_error_tells_agent_to_ask_user_before_autostart(self):
        with patch.object(server.SketchupConnection, "_connect_once", return_value=False):
            with self.assertRaisesRegex(Exception, "Ask the user whether to start SketchUp"):
                server.get_sketchup_connection()

    def test_direct_connection_is_not_cached(self):
        created = []

        class FakeConnection:
            def __init__(self, host, port):
                self.host = host
                self.port = port
                created.append(self)

            def connect(self, allow_autostart=True):
                return True

        with patch.object(server, "SketchupConnection", FakeConnection):
            first = server.get_sketchup_connection()
            second = server.get_sketchup_connection()

        self.assertEqual(len(created), 2)
        self.assertIs(first, created[0])
        self.assertIs(second, created[1])
        self.assertIsNot(created[0], created[1])

    def test_request_metadata_includes_sent_at_and_timeout(self):
        sent = {}

        class FakeSocket:
            def settimeout(self, timeout):
                sent["timeout"] = timeout

            def sendall(self, payload):
                sent["payload"] = payload
                header, body = payload.split(b"\r\n\r\n", 1)
                sent["header"] = header.decode("utf-8")
                sent["request"] = json.loads(body.decode("utf-8"))

        connection = server.SketchupConnection(host="localhost", port=9876, sock=FakeSocket())

        with (
            patch.object(connection, "connect", return_value=True),
            patch.object(connection, "receive_full_response", return_value=b'{"result":{"success":true}}'),
            patch("sketchup_mcp.server.time.time", return_value=1_780_000_000.123),
        ):
            connection.send_command("get_selection", request_id=7)

        self.assertEqual(sent["request"]["_mcp"]["sent_at_ms"], 1_780_000_000_123)
        self.assertEqual(sent["request"]["_mcp"]["timeout_ms"], server.DEFAULT_REQUEST_TIMEOUT_MS)
        self.assertRegex(sent["header"], r"Content-Length: \d+")
        self.assertEqual(sent["timeout"], server.DEFAULT_REQUEST_TIMEOUT_MS / 1000)

    def test_receive_full_response_accepts_content_length_frame(self):
        body = b'{"result":{"success":true}}'
        payload = b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body

        class FakeSocket:
            def __init__(self):
                self._payload = payload

            def settimeout(self, timeout):
                pass

            def recv(self, buffer_size):
                if not self._payload:
                    return b""
                chunk = self._payload[:buffer_size]
                self._payload = self._payload[buffer_size:]
                return chunk

        connection = server.SketchupConnection(host="localhost", port=9876)

        self.assertEqual(connection.receive_full_response(FakeSocket(), buffer_size=8), body)

    def test_receive_full_response_still_accepts_legacy_json(self):
        payload = b'{"result":{"success":true}}\n'

        class FakeSocket:
            def __init__(self):
                self._payload = payload

            def settimeout(self, timeout):
                pass

            def recv(self, buffer_size):
                if not self._payload:
                    return b""
                chunk = self._payload[:buffer_size]
                self._payload = self._payload[buffer_size:]
                return chunk

        connection = server.SketchupConnection(host="localhost", port=9876)

        self.assertEqual(connection.receive_full_response(FakeSocket(), buffer_size=8), payload)

    def test_receive_full_response_preserves_timeout_without_data(self):
        class FakeSocket:
            def settimeout(self, timeout):
                pass

            def recv(self, buffer_size):
                raise socket.timeout("timed out")

        connection = server.SketchupConnection(host="localhost", port=9876)

        with self.assertRaises(socket.timeout):
            connection.receive_full_response(FakeSocket(), buffer_size=8)

    def test_eval_ruby_tool_passes_prevent_modal_hang_to_sketchup(self):
        sent = {}

        class FakeContext:
            request_id = 123

        async def send_tool(name, arguments, request_id, *, port=None, allow_autostart=True):
            sent["method"] = "tools/call"
            sent["params"] = {"name": name, "arguments": arguments}
            sent["request_id"] = request_id
            return (
                {
                    "content": [{"text": "2"}],
                    "ui_events": [{"api": "UI.messagebox", "result": "IDNO"}],
                },
                {"host": "localhost", "port": port},
            )

        with patch.object(server, "_send_ruby_tool", new=AsyncMock(side_effect=send_tool)):
            response = json.loads(asyncio.run(server.eval_ruby(FakeContext(), "1 + 1", prevent_modal_hang=True, port=9878)))

        self.assertEqual(sent["method"], "tools/call")
        self.assertEqual(sent["params"]["name"], "eval_ruby")
        self.assertEqual(sent["params"]["arguments"], {"code": "1 + 1", "prevent_modal_hang": True})
        self.assertEqual(sent["request_id"], 123)
        self.assertEqual(response["result"], "2")
        self.assertEqual(response["target"], {"host": "localhost", "port": 9878})
        self.assertEqual(response["ui_events"], [{"api": "UI.messagebox", "result": "IDNO"}])

    def test_get_modal_state_uses_configured_connection_target_without_ruby_request(self):
        class FakeContext:
            request_id = 123

        expected = {
            "success": True,
            "status": "modal_detected",
            "is_modal": True,
            "pid": 42,
            "modal": {"title": "EW Example"},
        }

        with patch.object(server.modal_guard, "modal_state_for_port", return_value=expected) as state_for_port:
            response = json.loads(asyncio.run(server.get_modal_state(FakeContext(), port=9877)))

        state_for_port.assert_called_once_with("localhost", 9877, request_id=123)
        self.assertEqual(response, expected)

    def test_close_modal_uses_configured_connection_target_without_ruby_request(self):
        class FakeContext:
            request_id = 123

        expected = {
            "success": True,
            "status": "modal_closed",
            "is_modal": True,
            "closed": True,
            "pid": 42,
            "modal": {"title": "EW Example", "action": "wm_close"},
        }

        with patch.object(server.modal_guard, "close_modal_for_port", return_value=expected) as close_for_port:
            response = json.loads(asyncio.run(server.close_modal(FakeContext(), port=9877)))

        close_for_port.assert_called_once_with("localhost", 9877, request_id=123)
        self.assertEqual(response, expected)

    def test_modal_guard_runs_on_eval_timeout_when_prevent_modal_hang_enabled(self):
        class FakeSocket:
            def settimeout(self, timeout):
                pass

            def sendall(self, payload):
                pass

            def close(self):
                pass

        connection = server.SketchupConnection(host="localhost", port=9876, sock=FakeSocket())
        modal = {
            "pid": 42,
            "hwnd": "0x0000002a",
            "title": "Confirm",
            "class": "#32770",
            "main_disabled": True,
            "action": "wm_close",
            "request_id": 9,
        }

        with (
            patch.object(connection, "connect", return_value=True),
            patch.object(connection, "receive_full_response", side_effect=socket.timeout("timed out")),
            patch("sketchup_mcp.server.modal_guard.interrupt_modal_for_port", return_value=modal) as guard,
        ):
            with self.assertRaises(server.modal_guard.ModalGuardInterrupted) as raised:
                connection.send_command(
                    "eval_ruby",
                    {"code": "UI.messagebox('x')", "prevent_modal_hang": True},
                    request_id=9,
                )

        guard.assert_called_once_with("localhost", 9876, request_id=9)
        self.assertEqual(raised.exception.to_payload()["status"], "interrupted_by_modal")
        self.assertEqual(raised.exception.to_payload()["modal"], modal)

    def test_modal_guard_does_not_run_on_timeout_without_switch(self):
        class FakeSocket:
            def settimeout(self, timeout):
                pass

            def sendall(self, payload):
                pass

            def close(self):
                pass

        connection = server.SketchupConnection(host="localhost", port=9876, sock=FakeSocket())

        def reconnect(*args, **kwargs):
            connection.sock = FakeSocket()
            return True

        with (
            patch.object(connection, "connect", side_effect=reconnect),
            patch.object(connection, "receive_full_response", side_effect=socket.timeout("timed out")),
            patch("sketchup_mcp.server.modal_guard.interrupt_modal_for_port") as guard,
        ):
            with self.assertRaisesRegex(Exception, "Connection to Sketchup lost"):
                connection.send_command("eval_ruby", {"code": "1 + 1"}, request_id=9)

        guard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
