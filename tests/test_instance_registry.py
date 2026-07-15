import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sketchup_mcp import instance_registry


class InstanceRegistryTests(unittest.TestCase):
    def test_load_registered_instances_ignores_invalid_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sketchup-mcp" / "instances"
            root.mkdir(parents=True)
            (root / "valid.json").write_text(
                json.dumps({"schema_version": 1, "instance_id": "abc", "pid": 42, "port": 9876}),
                encoding="utf-8",
            )
            (root / "bad-json.json").write_text("not json", encoding="utf-8")
            (root / "bad-port.json").write_text(json.dumps({"pid": 42, "port": 70000}), encoding="utf-8")

            with patch.object(instance_registry.tempfile, "gettempdir", return_value=directory):
                entries = instance_registry.load_registered_instances()

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["pid"], 42)
        self.assertEqual(entries[0]["port"], 9876)
        self.assertTrue(entries[0]["registry_path"].endswith("valid.json"))

    def test_load_registered_instances_requires_current_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sketchup-mcp" / "instances"
            root.mkdir(parents=True)
            (root / "old-schema.json").write_text(
                json.dumps({"schema_version": 999, "pid": 42, "port": 9876}),
                encoding="utf-8",
            )

            with patch.object(instance_registry.tempfile, "gettempdir", return_value=directory):
                self.assertEqual(instance_registry.load_registered_instances(), [])


if __name__ == "__main__":
    unittest.main()
