import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sketchup_mcp import startup


class SketchupStartupTests(unittest.TestCase):
    def tearDown(self):
        startup.clear_session_autostart_allowed()

    def test_autostart_is_disabled_by_default(self):
        with patch.dict("os.environ", {}, clear=True), patch.object(startup, "launch_sketchup") as launch:
            self.assertFalse(startup.maybe_start_sketchup(9876))

        launch.assert_not_called()

    def test_autostart_can_use_session_permission(self):
        startup.set_session_autostart_allowed(True)

        with patch.dict("os.environ", {}, clear=True), patch.object(startup, "launch_sketchup") as launch:
            self.assertTrue(startup.maybe_start_sketchup(9876))

        launch.assert_called_once_with(9876)

    def test_autostart_can_use_environment_preapproval(self):
        with patch.dict("os.environ", {startup.AUTOSTART_ENV: "1"}, clear=True), patch.object(startup, "launch_sketchup") as launch:
            self.assertTrue(startup.maybe_start_sketchup(9876))

        launch.assert_called_once_with(9876)

    def test_launch_sketchup_uses_rubystartup_script_with_target_port(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sketchup_exe = Path(temp_dir) / "SketchUp.exe"
            sketchup_exe.write_text("", encoding="utf-8")

            with patch("subprocess.Popen") as popen:
                script_path = startup.launch_sketchup(9877, sketchup_exe=str(sketchup_exe))

            self.assertEqual(
                popen.call_args.args[0],
                [str(sketchup_exe), "-RubyStartup", str(script_path)],
            )

            script = Path(script_path).read_text(encoding="utf-8")
            self.assertIn("SU_MCP.start_server(9877)", script)
            self.assertIn("require 'su_mcp/main'", script)


if __name__ == "__main__":
    unittest.main()
