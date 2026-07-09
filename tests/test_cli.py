import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from sketchup_mcp import cli


class CliEvalTests(unittest.TestCase):
    def test_eval_uses_inline_code(self):
        args = cli.parse_args(["--port", "9877", "eval", "Sketchup.active_model.title"])

        self.assertEqual(cli.load_eval_code(args), "Sketchup.active_model.title")

    def test_eval_uses_file_code(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script_path = Path(temp_dir) / "probe.rb"
            script_path.write_text("1 + 1\n", encoding="utf-8")

            args = cli.parse_args(["eval", "--file", str(script_path)])
            self.assertEqual(cli.load_eval_code(args), "1 + 1\n")

    def test_eval_rejects_code_and_file_together(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script_path = Path(temp_dir) / "probe.rb"
            script_path.write_text("1 + 1\n", encoding="utf-8")

            with self.assertRaises(SystemExit):
                cli.parse_args(["eval", "1 + 1", "--file", str(script_path)])

    def test_start_sketchup_options_are_parsed(self):
        args = cli.parse_args([
            "--start-sketchup-if-needed",
            "--sketchup-exe",
            r"C:\Program Files\SketchUp\SketchUp 2026\SketchUp.exe",
            "--startup-timeout",
            "12",
            "ping",
        ])

        self.assertTrue(args.start_sketchup_if_needed)
        self.assertEqual(args.sketchup_exe, r"C:\Program Files\SketchUp\SketchUp 2026\SketchUp.exe")
        self.assertEqual(args.startup_timeout, 12.0)

    def test_eval_sends_eval_ruby_tool_call(self):
        sent = {}

        class FakeConnection:
            def __init__(self, host, port):
                self.host = host
                self.port = port
                sent["host"] = host
                sent["port"] = port

            def send_command(self, method, params=None, request_id=None):
                sent["method"] = method
                sent["params"] = params
                sent["request_id"] = request_id
                return {"success": True, "content": [{"text": "2"}]}

            def disconnect(self):
                sent["disconnected"] = True

        stdout = io.StringIO()
        with patch.object(cli, "SketchupConnection", FakeConnection):
            exit_code = cli.main(["--host", "127.0.0.1", "--port", "9877", "eval", "1 + 1"], stdout=stdout)

        self.assertEqual(exit_code, 0)
        self.assertEqual(sent["host"], "127.0.0.1")
        self.assertEqual(sent["port"], 9877)
        self.assertEqual(sent["method"], "eval_ruby")
        self.assertEqual(sent["params"], {"code": "1 + 1"})
        self.assertEqual(sent["request_id"], 1)
        self.assertTrue(sent["disconnected"])
        self.assertEqual(json.loads(stdout.getvalue()), {"success": True, "content": [{"text": "2"}]})

    def test_eval_can_enable_prevent_modal_hang(self):
        sent = {}

        class FakeConnection:
            def __init__(self, host, port):
                self.host = host
                self.port = port

            def send_command(self, method, params=None, request_id=None):
                sent["method"] = method
                sent["params"] = params
                sent["request_id"] = request_id
                return {"success": True, "content": [{"text": "2"}]}

            def disconnect(self):
                pass

        stdout = io.StringIO()
        with patch.object(cli, "SketchupConnection", FakeConnection):
            exit_code = cli.main(["eval", "--prevent-modal-hang", "1 + 1"], stdout=stdout)

        self.assertEqual(exit_code, 0)
        self.assertEqual(sent["method"], "eval_ruby")
        self.assertEqual(sent["params"], {"code": "1 + 1", "prevent_modal_hang": True})
        self.assertEqual(sent["request_id"], 1)

    def test_modal_state_uses_modal_guard_without_tcp_connection(self):
        expected = {
            "success": True,
            "status": "modal_detected",
            "is_modal": True,
            "pid": 42,
            "modal": {"title": "EW Example"},
        }
        stdout = io.StringIO()

        with (
            patch.object(cli, "SketchupConnection", side_effect=AssertionError("should not connect")),
            patch.object(cli.modal_guard, "modal_state_for_port", return_value=expected) as state_for_port,
        ):
            exit_code = cli.main(
                ["--host", "127.0.0.1", "--port", "9877", "modal-state"],
                stdout=stdout,
            )

        self.assertEqual(exit_code, 0)
        state_for_port.assert_called_once_with("127.0.0.1", 9877, request_id=1)
        self.assertEqual(json.loads(stdout.getvalue()), expected)

    def test_close_modal_uses_modal_guard_without_tcp_connection(self):
        expected = {
            "success": True,
            "status": "modal_closed",
            "is_modal": True,
            "closed": True,
            "pid": 42,
            "modal": {"title": "EW Example", "action": "wm_close"},
        }
        stdout = io.StringIO()

        with (
            patch.object(cli, "SketchupConnection", side_effect=AssertionError("should not connect")),
            patch.object(cli.modal_guard, "close_modal_for_port", return_value=expected) as close_for_port,
        ):
            exit_code = cli.main(
                ["--host", "127.0.0.1", "--port", "9877", "close-modal"],
                stdout=stdout,
            )

        self.assertEqual(exit_code, 0)
        close_for_port.assert_called_once_with("127.0.0.1", 9877, request_id=1)
        self.assertEqual(json.loads(stdout.getvalue()), expected)

    def test_ping_sends_protocol_request_instead_of_connect_probe(self):
        sent = {}

        class FakeConnection:
            def __init__(self, host, port):
                self.host = host
                self.port = port
                sent["host"] = host
                sent["port"] = port

            def connect(self):
                raise AssertionError("ping should send a protocol request")

            def send_command(self, method, params=None, request_id=None):
                sent["method"] = method
                sent["params"] = params
                sent["request_id"] = request_id
                return {"success": True}

            def disconnect(self):
                sent["disconnected"] = True

        stdout = io.StringIO()
        with patch.object(cli, "SketchupConnection", FakeConnection):
            exit_code = cli.main(["--host", "127.0.0.1", "--port", "9877", "ping"], stdout=stdout)

        self.assertEqual(exit_code, 0)
        self.assertEqual(sent["host"], "127.0.0.1")
        self.assertEqual(sent["port"], 9877)
        self.assertEqual(sent["method"], "ping")
        self.assertEqual(sent["params"], {})
        self.assertEqual(sent["request_id"], 1)
        self.assertTrue(sent["disconnected"])
        self.assertEqual(json.loads(stdout.getvalue()), {"success": True, "host": "127.0.0.1", "port": 9877})

    def test_call_help_mentions_existing_tool(self):
        stdout = io.StringIO()

        with self.assertRaises(SystemExit), redirect_stdout(stdout):
            cli.parse_args(["call", "--help"])

        help_text = stdout.getvalue()
        self.assertNotIn("get_scene_info", help_text)
        self.assertIn("get_selection", help_text)

    def test_invalid_environment_port_returns_json_error(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch.dict("os.environ", {"SKETCHUP_MCP_PORT": "70000"}):
            exit_code = cli.main(["ping"], stdout=stdout, stderr=stderr)

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("port must be an integer from 1 to 65535", json.loads(stderr.getvalue())["error"])

    def test_invalid_startup_timeout_returns_json_error(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = cli.main(["--startup-timeout", "-1", "ping"], stdout=stdout, stderr=stderr)

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("startup timeout must be a positive number", json.loads(stderr.getvalue())["error"])


if __name__ == "__main__":
    unittest.main()
