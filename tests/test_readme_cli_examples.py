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

    def test_codex_and_opencode_mcp_config_are_documented(self):
        readme = Path("README.md").read_text(encoding="utf-8")

        self.assertIn("[mcp_servers.sketchup]", readme)
        self.assertIn("`opencode.json`", readme)
        self.assertIn('"type": "local"', readme)
        self.assertIn('"command": ["uvx", "sketchup-mcp"]', readme)

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
