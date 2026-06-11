import re
import unittest
from pathlib import Path


class ReadmeCliExamplesTests(unittest.TestCase):
    def test_direct_cli_example_uses_existing_tool(self):
        readme = Path("README.md").read_text(encoding="utf-8")
        direct_cli_section = readme.split("### Direct CLI", 1)[1].split("### Using with Claude", 1)[0]

        self.assertNotIn("call get_scene_info", direct_cli_section)
        self.assertRegex(direct_cli_section, re.compile(r"sketchup-mcp-cli --port 9877 call get_selection\b"))

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


if __name__ == "__main__":
    unittest.main()
