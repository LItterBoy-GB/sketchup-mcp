import re
import unittest
from pathlib import Path


class ReadmeCliExamplesTests(unittest.TestCase):
    def test_direct_cli_example_uses_existing_tool(self):
        readme = Path("README.md").read_text(encoding="utf-8")
        direct_cli_section = readme.split("### Direct CLI", 1)[1].split("Once connected", 1)[0]

        self.assertNotIn("call get_scene_info", direct_cli_section)
        self.assertRegex(direct_cli_section, re.compile(r"sketchup-mcp-cli --port 9877 call get_selection\b"))

    def test_direct_cli_examples_include_prevent_modal_hang(self):
        readme = Path("README.md").read_text(encoding="utf-8")

        self.assertIn("sketchup-mcp-cli --port 9877 eval --prevent-modal-hang", readme)
        self.assertIn("prevent_modal_hang", readme)

    def test_tool_list_uses_existing_selection_tool_name(self):
        readme = Path("README.md").read_text(encoding="utf-8")
        tools_section = readme.split("#### Tools", 1)[1].split("### Example Commands", 1)[0]

        self.assertNotIn("get_scene_info", tools_section)
        self.assertNotIn("get_selected_components", tools_section)
        self.assertIn("`get_selection`", tools_section)

    def test_menu_path_matches_ruby_menu_name(self):
        readme = Path("README.md").read_text(encoding="utf-8")

        self.assertNotIn("Extensions > SketchupMCP", readme)
        self.assertIn("Extensions > MCP Server > Start Server", readme)

    def test_ruby_listener_autostart_and_port_search_are_documented(self):
        readme = Path("README.md").read_text(encoding="utf-8")
        chinese_readme = Path("README.zh-CN.md").read_text(encoding="utf-8")

        self.assertIn("listener starts automatically", readme)
        self.assertIn("first available port starting at `9876`", readme)
        self.assertIn("listener 会自动启动", chinese_readme)
        self.assertIn("从 `9876` 开始", chinese_readme)

    def test_codex_and_opencode_mcp_config_are_documented(self):
        readme = Path("README.md").read_text(encoding="utf-8")

        self.assertIn("[mcp_servers.sketchup]", readme)
        self.assertIn("`opencode.json`", readme)
        self.assertIn('"type": "local"', readme)
        self.assertIn('"command": ["uvx", "sketchup-mcp"]', readme)

    def test_shared_http_codex_configuration_and_migration_are_documented(self):
        readme = Path("README.md").read_text(encoding="utf-8")

        self.assertIn("http://127.0.0.1:8765/mcp", readme)
        self.assertIn("SKETCHUP_MCP_HTTP_TOKEN", readme)
        self.assertIn("scripts/manage-http-daemon.ps1", readme)
        self.assertIn('[mcp_servers.sketchup_2022]', readme)
        self.assertIn('url = "http://127.0.0.1:8765/mcp"', readme)
        self.assertIn('bearer_token_env_var = "SKETCHUP_MCP_HTTP_TOKEN"', readme)
        self.assertIn("startup_timeout_sec = 20", readme)
        self.assertIn("tool_timeout_sec = 300", readme)
        self.assertIn("required = false", readme)
        self.assertIn("[mcp_servers.sketchup_2022.tools.eval_ruby]", readme)
        self.assertIn('approval_mode = "approve"', readme)
        self.assertIn("Multiple Codex\ntasks cannot share the same stdio process", readme)
        self.assertIn("explicit tool `port`, then that session's default", readme)
        self.assertIn("service default port `9876`", readme)
        self.assertIn("Calls to the same\nresolved port are serialized", readme)
        self.assertIn("does not use an idle watchdog", readme)
        self.assertIn("Restart Codex", readme)
        self.assertIn("temporary HTTP server name", readme)
        self.assertIn("exactly match the old\nconfiguration", readme)
        self.assertIn("Chrome, IDA, or\nPPT", readme)
        self.assertRegex(
            readme,
            re.compile(
                r"```toml\n"
                r"\[mcp_servers\.sketchup_2022\]\n"
                r"url = \"http://127\.0\.0\.1:8765/mcp\"\n"
                r"bearer_token_env_var = \"SKETCHUP_MCP_HTTP_TOKEN\"\n"
                r"startup_timeout_sec = 20\n"
                r"tool_timeout_sec = 300\n"
                r"required = false\n\n"
                r"\[mcp_servers\.sketchup_2022\.tools\.eval_ruby\]\n"
                r"approval_mode = \"approve\"\n```"
            ),
        )

    def test_chinese_readme_documents_shared_http_contract(self):
        readme = Path("README.zh-CN.md").read_text(encoding="utf-8")

        self.assertIn("http://127.0.0.1:8765/mcp", readme)
        self.assertIn("SKETCHUP_MCP_HTTP_TOKEN", readme)
        self.assertIn("scripts/manage-http-daemon.ps1", readme)
        self.assertIn("多个 Codex 任务不能共享同一个 stdio 进程", readme)
        self.assertIn("显式传入的 `port` >", readme)
        self.assertIn("服务默认端口 `9876`", readme)
        self.assertIn("相同解析端口的调用会串行执行；不同端口的调用可以并行执行", readme)
        self.assertIn("HTTP 服务不使用 idle watchdog", readme)
        self.assertIn("必须重启 Codex", readme)
        self.assertIn("临时 HTTP server 名称", readme)
        self.assertIn("精确匹配的 stdio 进程树", readme)
        self.assertIn("Chrome、IDA、PPT", readme)
        self.assertIn("兼容与回滚", readme)
        self.assertRegex(
            readme,
            re.compile(
                r"```toml\n"
                r"\[mcp_servers\.sketchup_2022\]\n"
                r"url = \"http://127\.0\.0\.1:8765/mcp\"\n"
                r"bearer_token_env_var = \"SKETCHUP_MCP_HTTP_TOKEN\"\n"
                r"startup_timeout_sec = 20\n"
                r"tool_timeout_sec = 300\n"
                r"required = false\n\n"
                r"\[mcp_servers\.sketchup_2022\.tools\.eval_ruby\]\n"
                r"approval_mode = \"approve\"\n```"
            ),
        )

    def test_shared_http_daemon_lifecycle_is_documented_in_both_languages(self):
        english = Path("README.md").read_text(encoding="utf-8")
        chinese = Path("README.zh-CN.md").read_text(encoding="utf-8")

        for readme in (english, chinese):
            for command in (
                ".\\scripts\\manage-http-daemon.ps1 Install",
                ".\\scripts\\manage-http-daemon.ps1 Status",
                ".\\scripts\\manage-http-daemon.ps1 Restart",
                ".\\scripts\\manage-http-daemon.ps1 RotateToken",
                ".\\scripts\\manage-http-daemon.ps1 Uninstall",
                ".\\scripts\\manage-http-daemon.ps1 Uninstall -RemoveToken",
                ".\\scripts\\manage-http-daemon.ps1 Install -AllowAutostart:$false",
            ):
                self.assertIn(command, readme)
            self.assertIn("-AllowAutostart:$true", readme)
            self.assertIn("%LOCALAPPDATA%\\SketchUpMCP\\logs\\http-daemon.log", readme)
            self.assertIn("5 MiB", readme)
            self.assertIn("1800", readme)
            self.assertIn("Uninstall -RemoveToken", readme)

        self.assertIn("valid bearer token\ncan trigger SketchUp autostart", english)
        self.assertIn("reloads the current user-scoped token on every start", english)
        self.assertIn("Restart Codex afterwards", english)
        self.assertIn("normal HTTP\n`DELETE` close", english)
        self.assertIn("恢复 stdio 配置并重启 Codex", chinese)
        self.assertIn("每次启动时重新读取当前用户级 Token", chinese)
        self.assertIn("必须重启 Codex", chinese)
        self.assertIn("正常通过 `DELETE` 关闭后立即清理状态", chinese)

    def test_local_development_commands_cover_activation_and_uvx(self):
        readme = Path("README.md").read_text(encoding="utf-8")

        self.assertIn(r".\.venv\Scripts\Activate.ps1", readme)
        self.assertIn("uvx --from . sketchup-mcp-cli --help", readme)

    def test_autostart_and_request_scoped_port_routing_are_documented(self):
        readme = Path("README.md").read_text(encoding="utf-8")

        self.assertIn("SKETCHUP_MCP_AUTOSTART", readme)
        self.assertIn("SKETCHUP_MCP_SKETCHUP_EXE", readme)
        self.assertIn("SKETCHUP_MCP_REQUEST_TIMEOUT_MS", readme)
        self.assertIn("SKETCHUP_MCP_IDLE_TIMEOUT_SEC", readme)
        self.assertIn("ask the user whether", readme)
        self.assertIn("allow_sketchup_autostart", readme)
        self.assertIn("set_connection_port", readme)
        self.assertIn("Every SketchUp tool accepts an optional `port`", readme)
        self.assertIn("list_sketchup_instances", readme)
        self.assertIn("get_instance_info", readme)
        self.assertIn("2-second read-only timeout with no retries", readme)
        self.assertIn("never start SketchUp", readme)
        self.assertNotIn("[mcp_servers.sketchup_9877]", readme)
        self.assertIn("--start-sketchup-if-needed", readme)

    def test_chinese_readme_documents_request_scoped_port_routing(self):
        readme = Path("README.zh-CN.md").read_text(encoding="utf-8")

        self.assertIn("所有 SketchUp 工具都支持可选 `port`", readme)
        self.assertIn("list_sketchup_instances", readme)
        self.assertIn("get_instance_info", readme)
        self.assertIn("2 秒只读超时、不重试", readme)
        self.assertIn("不会自动启动 SketchUp", readme)

    def test_tcp_protocol_framing_is_documented(self):
        readme = Path("README.md").read_text(encoding="utf-8")

        self.assertIn("Content-Length", readme)
        self.assertIn("legacy one-line JSON", readme)


if __name__ == "__main__":
    unittest.main()
