import unittest
import json
from unittest.mock import patch

from sketchup_mcp import server


class ConnectionConfigTests(unittest.TestCase):
    def tearDown(self):
        server.clear_sketchup_port_override()
        server.startup.clear_session_autostart_allowed()
        server._active_request_count = 0
        server._last_activity_monotonic = 0.0
        if server._sketchup_connection is not None:
            try:
                server._sketchup_connection.disconnect()
            except Exception:
                pass
            server._sketchup_connection = None

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

    def test_cached_connection_is_returned_without_probe_ping(self):
        calls = []

        class FakeSocket:
            def sendall(self, payload):
                calls.append(payload)

            def close(self):
                pass

        cached = server.SketchupConnection(host="localhost", port=9876, sock=FakeSocket())
        server._sketchup_connection = cached

        self.assertIs(server.get_sketchup_connection(), cached)
        self.assertEqual(calls, [])

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


if __name__ == "__main__":
    unittest.main()
