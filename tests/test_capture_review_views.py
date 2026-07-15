import json
import asyncio
import unittest
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

from sketchup_mcp import server


@dataclass
class MockContext:
    request_id: int = 42


class CaptureReviewViewsTests(unittest.TestCase):
    def test_capture_review_views_sends_persistent_id_to_sketchup(self):
        captured = {}

        async def send_tool(name, arguments, request_id, *, port=None, allow_autostart=True):
            captured["method"] = "tools/call"
            captured["params"] = {"name": name, "arguments": arguments}
            captured["request_id"] = request_id
            return (
                {
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
                },
                {"host": "localhost", "port": port},
            )

        with patch.object(server, "_send_ruby_tool", new=AsyncMock(side_effect=send_tool)):
            result = asyncio.run(server.capture_review_views(
                MockContext(),
                persistent_id=12345,
                output_dir="C:/tmp/review",
                hide_others=True,
                width=1600,
                height=1200,
                port=9877,
            ))

        parsed = json.loads(result)
        self.assertTrue(parsed["success"])
        self.assertEqual(parsed["target"], {"host": "localhost", "port": 9877})
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
