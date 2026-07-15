import asyncio
import collections
import json
import threading
import time
import unittest
from unittest.mock import patch

from sketchup_mcp import instance_registry, server


class RequestRoutingTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        server.clear_sketchup_port_override()

    async def test_different_ports_run_in_parallel_but_same_port_is_serialized(self):
        state_lock = threading.Lock()
        active_by_port = collections.defaultdict(int)
        max_by_port = collections.defaultdict(int)
        active_total = 0
        max_total = 0
        disconnected = []

        class FakeConnection:
            def __init__(self, host, port):
                self.host = host
                self.port = port

            def connect(self, allow_autostart=True):
                return True

            def disconnect(self):
                disconnected.append(self.port)

            def send_command(
                self,
                method,
                params=None,
                request_id=None,
                *,
                allow_autostart=True,
                request_timeout_ms=None,
                max_retries=2,
            ):
                nonlocal active_total, max_total
                with state_lock:
                    active_by_port[self.port] += 1
                    active_total += 1
                    max_by_port[self.port] = max(max_by_port[self.port], active_by_port[self.port])
                    max_total = max(max_total, active_total)
                time.sleep(0.08)
                with state_lock:
                    active_by_port[self.port] -= 1
                    active_total -= 1
                return {"content": [{"text": str(self.port)}], "success": True}

        with patch.object(server, "SketchupConnection", FakeConnection):
            await asyncio.gather(
                server._send_ruby_tool("ping", {}, 1, port=9876),
                server._send_ruby_tool("ping", {}, 2, port=9877),
            )

            self.assertGreaterEqual(max_total, 2)
            self.assertEqual(max_by_port[9876], 1)
            self.assertEqual(max_by_port[9877], 1)

            active_by_port.clear()
            max_by_port.clear()
            active_total = 0
            max_total = 0
            await asyncio.gather(
                server._send_ruby_tool("ping", {}, 3, port=9876),
                server._send_ruby_tool("ping", {}, 4, port=9876),
            )

        self.assertEqual(max_by_port[9876], 1)
        self.assertEqual(max_total, 1)
        self.assertEqual(sorted(disconnected), [9876, 9876, 9876, 9877])

    async def test_discovery_only_queries_registered_or_explicit_ports(self):
        registered = [{"pid": 42, "port": 9876, "instance_id": "registered", "registry_path": "C:/tmp/42.json"}]
        calls = []

        def get_info(port):
            calls.append(port)
            if port == 9876:
                return {"instance_id": "registered", "pid": 42, "host": "localhost", "port": 9876}
            raise ConnectionError("not listening")

        with (
            patch.object(instance_registry, "load_registered_instances", return_value=registered),
            patch.object(server, "_get_instance_info_sync", side_effect=get_info),
        ):
            response = json.loads(await server.list_sketchup_instances(None, ports=[9877]))

        self.assertCountEqual(calls, [9876, 9877])
        self.assertEqual(response["instances"][0]["port"], 9876)
        self.assertTrue(response["instances"][0]["registered"])
        self.assertEqual(response["unavailable"][0]["port"], 9877)
        self.assertFalse(response["unavailable"][0]["registered"])

    def test_instance_discovery_never_enables_autostart(self):
        calls = []

        class FakeConnection:
            def __init__(self, host, port):
                self.host = host
                self.port = port

            def connect(self, allow_autostart=True):
                calls.append(("connect", allow_autostart))
                return True

            def disconnect(self):
                calls.append(("disconnect", None))

            def send_command(
                self,
                method,
                params=None,
                request_id=None,
                *,
                allow_autostart=True,
                request_timeout_ms=None,
                max_retries=2,
            ):
                calls.append(("send", allow_autostart, request_timeout_ms, max_retries))
                return {
                    "content": [{"text": json.dumps({"instance_id": "instance", "pid": 42})}],
                    "success": True,
                }

        with patch.object(server, "SketchupConnection", FakeConnection):
            info = server._get_instance_info_sync(9876)

        self.assertEqual(info["port"], 9876)
        self.assertEqual(
            calls,
            [
                ("connect", False),
                ("send", False, server.DISCOVERY_REQUEST_TIMEOUT_MS, 0),
                ("disconnect", None),
            ],
        )

    def test_instance_discovery_probes_different_ports_in_parallel(self):
        barrier = threading.Barrier(2)

        def get_info(port):
            barrier.wait(timeout=1.0)
            return {"instance_id": str(port), "pid": port, "host": "localhost", "port": port}

        with (
            patch.object(instance_registry, "load_registered_instances", return_value=[]),
            patch.object(server, "_get_instance_info_sync", side_effect=get_info),
        ):
            response = server._list_sketchup_instances_sync([9876, 9877])

        self.assertEqual([item["port"] for item in response["instances"]], [9876, 9877])
        self.assertEqual(response["unavailable"], [])

    def test_discovery_connection_error_does_not_suggest_autostart(self):
        error = server._connection_error("localhost", 9876, allow_autostart=False)

        self.assertNotIn("allow_sketchup_autostart", str(error))

    def test_every_sketchup_tool_exposes_optional_port(self):
        names = [
            "get_instance_info",
            "get_modal_state",
            "close_modal",
            "create_component",
            "delete_component",
            "transform_component",
            "get_selection",
            "set_material",
            "export_scene",
            "capture_review_views",
            "create_mortise_tenon",
            "create_dovetail",
            "create_finger_joint",
            "eval_ruby",
        ]

        for name in names:
            with self.subTest(tool=name):
                tool = server.mcp._tool_manager.get_tool(name)
                self.assertIn("port", tool.parameters["properties"])
                self.assertNotIn("port", tool.parameters.get("required", []))


if __name__ == "__main__":
    unittest.main()
