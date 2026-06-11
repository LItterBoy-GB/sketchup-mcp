import unittest
from pathlib import Path


RUBY_MAIN = Path("su_mcp/su_mcp/main.rb")


class RubyCaptureReviewViewsSourceTests(unittest.TestCase):
    def test_capture_review_views_rejects_non_top_level_targets(self):
        source = RUBY_MAIN.read_text(encoding="utf-8")

        self.assertIn("unless model.entities.include?(target)", source)
        self.assertIn("capture_review_views only supports top-level model entities", source)

    def test_capture_review_views_checks_write_image_result(self):
        source = RUBY_MAIN.read_text(encoding="utf-8")

        self.assertIn("unless view.write_image(", source)
        self.assertIn("Failed to write review image", source)


if __name__ == "__main__":
    unittest.main()
