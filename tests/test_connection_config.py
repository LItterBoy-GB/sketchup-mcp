import unittest
from unittest.mock import patch

from sketchup_mcp import server


class ConnectionConfigTests(unittest.TestCase):
    def tearDown(self):
        server.clear_sketchup_port_override()
        server.startup.clear_session_autostart_allowed()

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


if __name__ == "__main__":
    unittest.main()
