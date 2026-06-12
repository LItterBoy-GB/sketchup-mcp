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

    def test_ruby_startup_can_start_server_on_requested_port(self):
        source = RUBY_MAIN.read_text(encoding="utf-8")

        self.assertIn("def self.start_server(port = nil)", source)
        self.assertIn("@server.set_port(port) if port", source)
        self.assertIn("@server.start", source)

    def test_expired_requests_are_dropped_before_tool_dispatch(self):
        source = RUBY_MAIN.read_text(encoding="utf-8")

        self.assertIn('def request_expired?(request)', source)
        self.assertIn('request["_mcp"]', source)
        self.assertIn('if request_expired?(request)', source)
        self.assertIn('Dropped expired request', source)

    def test_content_length_framing_is_supported(self):
        source = RUBY_MAIN.read_text(encoding="utf-8")

        self.assertIn('def read_client_payload(client)', source)
        self.assertIn('Content-Length:', source)
        self.assertIn('return first_line', source)
        self.assertIn('client.read(length)', source)
        self.assertIn('def write_json_response(client, response)', source)
        self.assertIn('body.bytesize', source)
        self.assertIn('write_json_response(client, response)', source)

    def test_empty_probe_connections_are_ignored(self):
        source = RUBY_MAIN.read_text(encoding="utf-8")

        self.assertIn('rescue EOFError, Errno::ECONNRESET', source)
        self.assertIn('Client closed before sending a request', source)
        self.assertIn('return nil', source)

    def test_ping_tool_is_supported(self):
        source = RUBY_MAIN.read_text(encoding="utf-8")

        self.assertIn('when "ping"', source)
        self.assertIn('{ success: true, result: "pong" }', source)


if __name__ == "__main__":
    unittest.main()
