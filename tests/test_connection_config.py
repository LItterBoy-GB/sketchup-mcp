import unittest
from unittest.mock import patch

from sketchup_mcp import server


class ConnectionConfigTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
