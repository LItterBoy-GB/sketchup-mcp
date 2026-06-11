import json
import unittest
from dataclasses import dataclass
from unittest.mock import patch

from sketchup_mcp import server


@dataclass
class MockContext:
    request_id: int = 42


class CaptureReviewViewsTests(unittest.TestCase):
    def test_capture_review_views_sends_persistent_id_to_sketchup(self):
        captured = {}

        class FakeConnection:
            def send_command(self, method, params=None, request_id=None):
                captured["method"] = method
                captured["params"] = params
                captured["request_id"] = request_id
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "front": "C:/tmp/front.png",
                                    "right": "C:/tmp/right.png",
                                    "top": "C:/tmp/top.png",
                                }
                            ),
                        }
                    ],
                    "success": True,
                }

        with patch.object(server, "get_sketchup_connection", return_value=FakeConnection()):
            result = server.capture_review_views(
                MockContext(),
                persistent_id=12345,
                output_dir="C:/tmp/review",
                hide_others=True,
                width=1600,
                height=1200,
            )

        parsed = json.loads(result)
        self.assertTrue(parsed["success"])
        self.assertEqual(captured["method"], "tools/call")
        self.assertEqual(captured["request_id"], 42)
        self.assertEqual(captured["params"]["name"], "capture_review_views")
        self.assertEqual(
            captured["params"]["arguments"],
            {
                "persistent_id": 12345,
                "output_dir": "C:/tmp/review",
                "hide_others": True,
                "width": 1600,
                "height": 1200,
            },
        )


if __name__ == "__main__":
    unittest.main()
