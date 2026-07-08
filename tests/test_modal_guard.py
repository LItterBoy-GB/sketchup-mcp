import unittest
from unittest.mock import patch

from sketchup_mcp import modal_guard


class ModalGuardTests(unittest.TestCase):
    def test_non_windows_guard_is_noop(self):
        with patch("sketchup_mcp.modal_guard.sys.platform", "linux"):
            self.assertIsNone(modal_guard.interrupt_modal_for_port("localhost", 9876, request_id=1))

    def test_missing_listener_pid_is_noop(self):
        with (
            patch("sketchup_mcp.modal_guard.sys.platform", "win32"),
            patch("sketchup_mcp.modal_guard.find_pid_for_local_port", return_value=None),
        ):
            self.assertIsNone(modal_guard.interrupt_modal_for_port("localhost", 9876, request_id=1))

    def test_detect_modal_uses_last_active_popup(self):
        windows = [
            {
                "hwnd": "0x00000001",
                "hwnd_int": 1,
                "title": "SketchUp",
                "class": "SketchUp",
                "enabled": True,
                "owner": 0,
                "root_owner": 1,
                "last_active_popup": 2,
            },
            {
                "hwnd": "0x00000002",
                "hwnd_int": 2,
                "title": "Plugin Dialog",
                "class": "PluginDialog",
                "enabled": True,
                "owner": 0,
                "root_owner": 2,
                "last_active_popup": 2,
            },
        ]

        with patch("sketchup_mcp.modal_guard._visible_windows_for_pid", return_value=windows):
            modal = modal_guard.detect_modal_for_pid(42, request_id=7)

        self.assertEqual(modal["hwnd"], "0x00000002")
        self.assertEqual(modal["title"], "Plugin Dialog")
        self.assertFalse(modal["main_disabled"])
        self.assertEqual(modal["request_id"], 7)


if __name__ == "__main__":
    unittest.main()
