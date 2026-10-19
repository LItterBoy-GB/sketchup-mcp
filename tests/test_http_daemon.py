import os
import json
import socket
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

from sketchup_mcp import http_daemon


class HttpDaemonTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Requires Windows GUI Python")
    def test_scheduled_bootstrap_survives_rebuilt_console_venv_shim(self):
        base = Path(sys.base_prefix)
        pythonw = base / "pythonw.exe"
        if not pythonw.is_file():
            self.skipTest("Base GUI Python is not installed")
        pe = pythonw.read_bytes()
        offset = struct.unpack_from("<I", pe, 60)[0]
        self.assertEqual(struct.unpack_from("<H", pe, offset + 24 + 68)[0], 2)
        source_root = Path(__file__).resolve().parents[1]
        import mcp

        dependency_site = Path(mcp.__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory(prefix="MCP GUI 中文 ") as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            shutil.copyfile(source_root / "scripts/run-http-daemon.pyw", root / "scripts/run-http-daemon.pyw")
            packages = root / ".venv/Lib/site-packages"
            packages.mkdir(parents=True)
            scripts = root / ".venv/Scripts"
            scripts.mkdir()
            (root / ".venv/pyvenv.cfg").write_text(f"home = {base}\n", encoding="utf-8")
            (packages / "00-dependencies.pth").write_text(
                "import site; site.addsitedir(" + repr(str(dependency_site)) + ")\n" + str(source_root / "src") + "\n",
                encoding="utf-8")
            (packages / "zz-probe.pth").write_text("import bootstrap_test_hook\n", encoding="utf-8")
            (packages / "bootstrap_test_hook.py").write_text('''import ctypes, json, os, pathlib, sys, threading, time
root = pathlib.Path(os.environ["LOCALAPPDATA"])
(root / "runtime.json").write_text(json.dumps({"console": ctypes.windll.kernel32.GetConsoleWindow(),
    "executable": sys.executable, "prefix": sys.prefix, "isolated": sys.flags.isolated, "no_site": sys.flags.no_site}))
def watch():
    deadline = time.monotonic() + 35
    while not (root / "stop").exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    os._exit(0)
threading.Thread(target=watch, daemon=True).start()
from sketchup_mcp import http_daemon
http_daemon.read_user_token = lambda: "bootstrap-isolated-token"
''', encoding="utf-8")
            poison = root / "poison"
            poison.mkdir()
            (poison / "sketchup_mcp.py").write_text("raise RuntimeError('PYTHONPATH must be ignored')", encoding="utf-8")
            env = dict(os.environ, LOCALAPPDATA=str(root), PYTHONPATH=str(poison))
            for broken_shim in (b"invalid launcher", (base / "python.exe").read_bytes()):
                with self.subTest(console_shim_size=len(broken_shim)):
                    # 模拟 uv 重建 Scripts；任务从始至终不调用这里的可执行文件。
                    shutil.rmtree(scripts)
                    scripts.mkdir()
                    (scripts / "pythonw.exe").write_bytes(broken_shim)
                    (root / "stop").unlink(missing_ok=True)
                    with socket.socket() as sock:
                        sock.bind(("127.0.0.1", 0))
                        port = sock.getsockname()[1]
                    process = subprocess.Popen([str(pythonw), "-I", "-S", str(root / "scripts/run-http-daemon.pyw"),
                                                "--http-port", str(port)], env=env)
                    try:
                        deadline = time.monotonic() + 15
                        while True:
                            try:
                                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                                    self.assertEqual(response.status, 200)
                                break
                            except OSError:
                                if process.poll() is not None or time.monotonic() >= deadline:
                                    log = root / "SketchUpMCP/logs/http-launcher.log"
                                    self.fail(log.read_text(encoding="utf-8") if log.exists() else "GUI bootstrap failed")
                                time.sleep(0.1)
                        info = json.loads((root / "runtime.json").read_text())
                        self.assertEqual(info["console"], 0)
                        self.assertEqual(Path(info["executable"]), pythonw)
                        self.assertEqual(Path(info["prefix"]), root / ".venv")
                        self.assertEqual((info["isolated"], info["no_site"]), (1, 1))
                        data = json.dumps({"jsonrpc":"2.0","id":1,"method":"initialize","params":{
                            "protocolVersion":"2025-11-25","capabilities":{},
                            "clientInfo":{"name":"bootstrap-test","version":"1"}}}).encode()
                        headers = {"Content-Type":"application/json","Accept":"application/json, text/event-stream",
                                   "Authorization":"Bearer bootstrap-isolated-token"}
                        with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/mcp",data,headers),timeout=3) as response:
                            self.assertIn(b"serverInfo", response.read())
                    finally:
                        (root / "stop").touch()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)

    @unittest.skipUnless(os.name == "nt", "Requires Windows GUI Python")
    def test_bootstrap_missing_venv_has_file_diagnostics_and_nonzero_exit(self):
        pythonw = Path(sys.base_prefix) / "pythonw.exe"
        if not pythonw.is_file():
            self.skipTest("Base GUI Python is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            source = Path(__file__).resolve().parents[1] / "scripts/run-http-daemon.pyw"
            shutil.copyfile(source, root / "scripts/run-http-daemon.pyw")
            result = subprocess.run([str(pythonw), "-I", "-S", str(root / "scripts/run-http-daemon.pyw")],
                                    env=dict(os.environ, LOCALAPPDATA=str(root)), timeout=15)
            self.assertEqual(result.returncode, 1)
            log = (root / "SketchUpMCP/logs/http-launcher.log").read_text(encoding="utf-8")
            self.assertIn("Missing .venv/pyvenv.cfg", log)
            self.assertIn("HTTP bootstrap failed", log)

    @unittest.skipUnless(os.name == "nt", "Requires Windows pythonw")
    def test_real_pythonw_serves_mcp_without_a_console(self):
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not pythonw.is_file():
            self.skipTest("pythonw is not installed")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            probe = root / "probe.pyw"
            probe.write_text('''import ctypes, json, os, pathlib, threading, time
root = pathlib.Path(__file__).parent
def shutdown_watch():
    deadline = time.monotonic() + 45
    while not (root / "stop").exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    os._exit(0)
threading.Thread(target=shutdown_watch, daemon=True).start()
(root / "console.json").write_text(json.dumps({"console": ctypes.windll.kernel32.GetConsoleWindow()}))
from sketchup_mcp import http_daemon
http_daemon.read_user_token = lambda: "isolated-fresh-token"
http_daemon.main()
''', encoding="utf-8")
            env = dict(os.environ, LOCALAPPDATA=directory, SKETCHUP_MCP_HTTP_TOKEN="isolated-stale-token")
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            # 不使用 CREATE_NO_WINDOW，验证 pythonw 自身不会分配控制台。
            process = subprocess.Popen([str(pythonw), str(probe), "--http-port", str(port)], env=env)
            try:
                url = f"http://127.0.0.1:{port}"
                deadline = time.monotonic() + 20
                while True:
                    try:
                        with urllib.request.urlopen(url + "/healthz", timeout=1) as response:
                            self.assertEqual(response.status, 200)
                        break
                    except (OSError, urllib.error.URLError):
                        if time.monotonic() >= deadline:
                            self.fail("pythonw HTTP server did not become healthy")
                        time.sleep(0.1)
                self.assertEqual(json.loads((root / "console.json").read_text())["console"], 0)
                data = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "silent-launch-test", "version": "1"}
                }}).encode()
                headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                           "Authorization": "Bearer isolated-stale-token"}
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(urllib.request.Request(url + "/mcp", data, headers), timeout=3)
                self.assertEqual(denied.exception.code, 401)
                denied.exception.close()
                headers["Authorization"] = "Bearer isolated-fresh-token"
                with urllib.request.urlopen(urllib.request.Request(url + "/mcp", data, headers), timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"serverInfo", response.read())
                self.assertTrue((root / "SketchUpMCP/logs/http-daemon.log").is_file())
            finally:
                (root / "stop").touch()
                process.wait(timeout=25)

    def test_registry_token_overrides_inherited_environment(self):
        registry = MagicMock(REG_SZ=1, REG_EXPAND_SZ=2)
        registry.QueryValueEx.return_value = ("fresh-user-token", 1)
        with patch.dict(sys.modules, winreg=registry), patch.dict(
            os.environ, {http_daemon.HTTP_TOKEN_ENV: "stale-inherited-token"}
        ):
            self.assertEqual(http_daemon.read_user_token(), "fresh-user-token")
            registry.OpenKey.assert_called_once_with(registry.HKEY_CURRENT_USER, "Environment")

    def test_missing_registry_token_does_not_fall_back_to_stale_environment(self):
        registry = MagicMock(REG_SZ=1, REG_EXPAND_SZ=2)
        registry.OpenKey.side_effect = FileNotFoundError
        with patch.dict(sys.modules, winreg=registry), patch.dict(
            os.environ, {http_daemon.HTTP_TOKEN_ENV: "stale-inherited-token"}
        ), self.assertRaisesRegex(RuntimeError, "missing"):
            http_daemon.read_user_token()

    def test_pythonw_streams_and_token_are_ready_before_server_runs(self):
        def run_http(argv):
            self.assertEqual(argv, ["--http-port", "18765"])
            self.assertEqual(os.environ[http_daemon.HTTP_TOKEN_ENV], "fresh-user-token")
            self.assertEqual(sys.stdin.read(), "")
            sys.stdout.write("dependency output")
            sys.stderr.write("dependency error output")

        fake_server = types.ModuleType("sketchup_mcp.http_server")
        fake_server.main = run_http
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory, http_daemon.HTTP_TOKEN_ENV: "stale"}
        ), patch.dict(sys.modules, {"sketchup_mcp.http_server": fake_server}), patch.object(
            http_daemon, "read_user_token", return_value="fresh-user-token"
        ), patch.object(sys, "stdin", None), patch.object(sys, "stdout", None), patch.object(sys, "stderr", None):
            http_daemon.main(["--http-port", "18765"])
            self.assertIsNone(sys.stdout)
            self.assertIsNone(sys.stderr)

    def test_startup_failure_has_file_diagnostics_and_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ), patch.object(http_daemon, "read_user_token", side_effect=RuntimeError("test missing token")):
            with self.assertRaises(SystemExit) as raised:
                http_daemon.main([])
            self.assertEqual(raised.exception.code, 1)
            log = (Path(directory) / "SketchUpMCP/logs/http-launcher.log").read_text(encoding="utf-8")
            self.assertIn("test missing token", log)
            self.assertIn("Traceback", log)


if __name__ == "__main__":
    unittest.main()
