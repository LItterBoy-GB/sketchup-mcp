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

    def test_modal_state_reports_detected_modal_without_closing(self):
        modal = {
            "pid": 42,
            "hwnd": "0x0000002a",
            "class": "Chrome_WidgetWin_1",
            "title": "EW Example",
            "main_disabled": True,
            "request_id": 9,
        }

        with (
            patch("sketchup_mcp.modal_guard.sys.platform", "win32"),
            patch("sketchup_mcp.modal_guard.find_pid_for_local_port", return_value=42),
            patch("sketchup_mcp.modal_guard.detect_modal_for_pid", return_value=modal),
            patch("sketchup_mcp.modal_guard.close_modal_window") as close_modal,
        ):
            state = modal_guard.modal_state_for_port("localhost", 9876, request_id=9)

        close_modal.assert_not_called()
        self.assertTrue(state["success"])
        self.assertEqual(state["status"], "modal_detected")
        self.assertTrue(state["is_modal"])
        self.assertEqual(state["pid"], 42)
        self.assertEqual(state["modal"], modal)

    def test_close_modal_for_port_closes_detected_modal(self):
        modal = {
            "pid": 42,
            "hwnd": "0x0000002a",
            "class": "Chrome_WidgetWin_1",
            "title": "EW Example",
            "main_disabled": True,
            "request_id": 9,
        }

        with (
            patch("sketchup_mcp.modal_guard.sys.platform", "win32"),
            patch("sketchup_mcp.modal_guard.find_pid_for_local_port", return_value=42),
            patch("sketchup_mcp.modal_guard.detect_modal_for_pid", side_effect=[modal.copy(), None]),
            patch("sketchup_mcp.modal_guard.close_modal_window", return_value="wm_close") as close_modal,
        ):
            state = modal_guard.close_modal_for_port("localhost", 9876, request_id=9)

        close_modal.assert_called_once_with(42)
        self.assertTrue(state["success"])
        self.assertEqual(state["status"], "modal_closed")
        self.assertTrue(state["closed"])
        self.assertEqual(state["action"], "wm_close")
        self.assertEqual(state["closed_count"], 1)
        self.assertEqual(state["last_closed_modal"]["action"], "wm_close")

    def test_close_modal_for_port_repeats_until_no_modal(self):
        first_modal = {
            "pid": 42,
            "hwnd": "0x0000002a",
            "class": "#32770",
            "title": "First Modal",
            "main_disabled": True,
            "request_id": 9,
        }
        second_modal = {
            "pid": 42,
            "hwnd": "0x0000002b",
            "class": "#32770",
            "title": "Second Modal",
            "main_disabled": True,
            "request_id": 9,
        }

        with (
            patch("sketchup_mcp.modal_guard.sys.platform", "win32"),
            patch("sketchup_mcp.modal_guard.find_pid_for_local_port", return_value=42),
            patch("sketchup_mcp.modal_guard.detect_modal_for_pid", side_effect=[first_modal, second_modal, None]),
            patch("sketchup_mcp.modal_guard.close_modal_window", side_effect=["wm_close", "escape"]) as close_modal,
        ):
            state = modal_guard.close_modal_for_port("localhost", 9876, request_id=9)

        self.assertEqual([call.args[0] for call in close_modal.call_args_list], [42, 43])
        self.assertEqual(state["status"], "modal_closed")
        self.assertFalse(state["is_modal"])
        self.assertTrue(state["closed"])
        self.assertEqual(state["closed_count"], 2)
        self.assertEqual([modal["action"] for modal in state["closed_modals"]], ["wm_close", "escape"])

    def test_modal_state_reports_no_modal_when_listener_pid_is_missing(self):
        with (
            patch("sketchup_mcp.modal_guard.sys.platform", "win32"),
            patch("sketchup_mcp.modal_guard.find_pid_for_local_port", return_value=None),
        ):
            state = modal_guard.modal_state_for_port("localhost", 9876, request_id=9)

        self.assertTrue(state["success"])
        self.assertEqual(state["status"], "listener_not_found")
        self.assertFalse(state["is_modal"])
        self.assertIsNone(state["modal"])

    def test_detect_modal_uses_last_active_popup(self):
        windows = [
            {
                "hwnd": "0x00000001",
                "hwnd_int": 1,
                "title": "SketchUp",
                "class": "SketchUp",
                "enabled": False,
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
        self.assertTrue(modal["main_disabled"])
        self.assertEqual(modal["request_id"], 7)

    def test_detect_modal_ignores_modeless_owned_popup_when_main_is_enabled(self):
        windows = [
            {
                "hwnd": "0x00000002",
                "hwnd_int": 2,
                "title": "Ruby Console",
                "class": "#32770",
                "enabled": True,
                "owner": 1,
                "root_owner": 1,
                "last_active_popup": 2,
            },
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
        ]

        with patch("sketchup_mcp.modal_guard._visible_windows_for_pid", return_value=windows):
            self.assertIsNone(modal_guard.detect_modal_for_pid(42, request_id=7))

    def test_detect_modal_prefers_enabled_popup_when_main_is_disabled(self):
        windows = [
            {
                "hwnd": "0x00000003",
                "hwnd_int": 3,
                "title": "Disabled Wrapper",
                "class": "#32770",
                "enabled": False,
                "owner": 1,
                "root_owner": 1,
                "last_active_popup": 3,
            },
            {
                "hwnd": "0x00000002",
                "hwnd_int": 2,
                "title": "Enabled Dialog",
                "class": "#32770",
                "enabled": True,
                "owner": 1,
                "root_owner": 1,
                "last_active_popup": 2,
            },
            {
                "hwnd": "0x00000001",
                "hwnd_int": 1,
                "title": "SketchUp",
                "class": "SketchUp",
                "enabled": False,
                "owner": 0,
                "root_owner": 1,
                "last_active_popup": 2,
            },
        ]

        with patch("sketchup_mcp.modal_guard._visible_windows_for_pid", return_value=windows):
            modal = modal_guard.detect_modal_for_pid(42, request_id=7)

        self.assertEqual(modal["hwnd"], "0x00000002")
        self.assertEqual(modal["title"], "Enabled Dialog")


if __name__ == "__main__":
    unittest.main()
