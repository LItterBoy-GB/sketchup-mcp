import asyncio
import json
import socket
import threading
import unittest
from unittest.mock import patch

from sketchup_mcp import server


class RequestSafetyTests(unittest.TestCase):
    def test_write_is_executed_once_when_response_is_lost(self):
        """The peer applies the write and closes without acknowledging it."""
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(0.3)
            received = []
            failures = []

            def peer():
                try:
                    while True:
                        try:
                            client, _ = listener.accept()
                        except socket.timeout:
                            break
                        with client:
                            client.settimeout(1)
                            data = b""
                            while server._extract_framed_body(data) is None:
                                chunk = client.recv(8192)
                                if not chunk:
                                    break
                                data += chunk
                            received.append(json.loads(server._extract_framed_body(data)))
                except Exception as exc:
                    failures.append(exc)

            worker = threading.Thread(target=peer)
            worker.start()
            connection = server.SketchupConnection(host="127.0.0.1", port=listener.getsockname()[1])
            try:
                with self.assertRaises(server.RequestOutcomeUnknown):
                    connection.send_command("eval_ruby", {"code": "$writes += 1"}, request_id=1)
            finally:
                connection.disconnect()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(len(received), 1)

    def test_partial_send_failure_never_replays_unknown_tools(self):
        class Peer:
            def settimeout(self, _timeout):
                pass

            def sendall(self, _payload):
                raise BrokenPipeError("partial write")

            def close(self):
                pass

        connection = server.SketchupConnection(host="localhost", port=9876, sock=Peer())
        with patch.object(connection, "connect", return_value=True) as connect:
            with self.assertRaises(server.RequestOutcomeUnknown):
                connection.send_command("future_mutating_tool", {}, max_retries=20)
        self.assertEqual(connect.call_count, 1)
        self.assertIsNone(connection.sock)

    def test_read_can_retry_after_response_loss(self):
        sends = []

        class Peer:
            def settimeout(self, _timeout):
                pass

            def sendall(self, payload):
                sends.append(payload)

            def close(self):
                pass

        connection = server.SketchupConnection(host="localhost", port=9876)

        def connect(**_kwargs):
            connection.sock = Peer()
            return True

        with (
            patch.object(connection, "connect", side_effect=connect),
            patch.object(connection, "receive_full_response", side_effect=[
                socket.timeout(), b'{"id":1,"result":{"content":[{"text":"pong"}]}}'
            ]),
        ):
            response = connection.send_command("ping", {}, request_id=1)
        connection.disconnect()
        self.assertEqual(response["content"][0]["text"], "pong")
        self.assertEqual(len(sends), 2)

    def test_timeout_after_send_never_replays_ruby(self):
        from unittest.mock import Mock
        peer = Mock()
        connection = server.SketchupConnection(host="localhost", port=9876, sock=peer)
        with (
            patch.object(connection, "connect", return_value=True),
            patch.object(connection, "receive_full_response", side_effect=socket.timeout("still running")),
        ):
            with self.assertRaises(server.RequestOutcomeUnknown):
                connection.send_command("eval_ruby", {"code": "$writes += 1"}, max_retries=8)
        self.assertEqual(peer.sendall.call_count, 1)
        self.assertIsNone(connection.sock)

    def test_modal_inspection_failure_keeps_execution_unknown(self):
        from unittest.mock import Mock
        peer = Mock()
        connection = server.SketchupConnection(host="localhost", port=9876, sock=peer)
        with (
            patch.object(connection, "connect", return_value=True),
            patch.object(connection, "receive_full_response", side_effect=socket.timeout()),
            patch.object(server.modal_guard, "interrupt_modal_for_port", side_effect=RuntimeError("inspection failed")),
        ):
            with self.assertRaises(server.RequestOutcomeUnknown):
                connection.send_command("eval_ruby", {"code": "1", "prevent_modal_hang": True})
        self.assertEqual(peer.sendall.call_count, 1)

    def test_invalid_response_to_write_is_unknown_and_not_replayed(self):
        from unittest.mock import Mock
        for response in (b'{"id":2,"result":{}}', b'[]', b'{"id":1}', b'{"id":1,"error":null}'):
            with self.subTest(response=response):
                connection = server.SketchupConnection(host="localhost", port=9876, sock=Mock())
                with (
                    patch.object(connection, "connect", return_value=True),
                    patch.object(connection, "receive_full_response", return_value=response) as receive,
                ):
                    with self.assertRaises(server.RequestOutcomeUnknown):
                        connection.send_command("eval_ruby", {"code": "1"}, request_id=1)
                self.assertEqual(receive.call_count, 1)

    def test_receive_uses_total_deadline_and_preserves_split_utf8(self):
        body = json.dumps({"result": "中文"}, ensure_ascii=False).encode()
        chunks = iter(bytes([byte]) for byte in body)
        timeouts = []

        class Peer:
            def settimeout(self, timeout):
                timeouts.append(timeout)

            def recv(self, _size):
                return next(chunks)

        connection = server.SketchupConnection(host="localhost", port=9876)
        self.assertEqual(connection.receive_full_response(Peer(), timeout_ms=1000), body)
        self.assertLessEqual(timeouts[-1], timeouts[0])
        from unittest.mock import Mock
        peer = Mock()
        peer.recv.return_value = b"{"
        with patch.object(server.time, "monotonic", side_effect=[0, 0.2, 1.1]):
            with self.assertRaises(ConnectionError):
                connection.receive_full_response(peer, timeout_ms=1000)
        self.assertEqual(peer.recv.call_count, 1)


class ResultFormatTests(unittest.IsolatedAsyncioTestCase):
    async def test_eval_preserves_types_and_legacy_extension_text(self):
        for result in ({"中文": [1, False, None]}, None, False, 42, "42"):
            with self.subTest(result=result), patch.object(server, "_send_ruby_tool", return_value=(
                {"value": result, "content": [{"text": "legacy"}]}, {"host": "localhost", "port": 9877}
            )):
                response = await server.eval_ruby(None, "probe", port=9877)
                self.assertFalse(response.isError)
                self.assertEqual(response.structuredContent["result"], result)
                self.assertEqual(json.loads(response.content[0].text), response.structuredContent)
        with patch.object(server, "_send_ruby_tool", return_value=(
            {"content": [{"text": "{not parsed as JSON}"}]}, {"host": "localhost", "port": 9877}
        )):
            response = await server.eval_ruby(None, "probe", port=9877)
        self.assertEqual(response.structuredContent["result"], "{not parsed as JSON}")

    async def test_error_has_mcp_failure_flag_target_and_execution_state(self):
        for exc, code, state in (
            (server.RequestOutcomeUnknown("response lost"), "execution_unknown", "unknown"),
            (ConnectionError("not listening"), "connection_error", "not_started"),
            (server.RubyToolError({"message": "bad Ruby", "data": {
                "type": "SyntaxError", "backtrace": ["probe:1"], "execution_state": "failed"
            }}), "ruby_error", "failed"),
        ):
            with self.subTest(code=code), patch.object(server, "_send_ruby_tool", side_effect=exc):
                response = await server.eval_ruby(None, "probe", port=9878)
                self.assertTrue(response.isError)
                payload = response.structuredContent
                self.assertEqual(payload["target"]["port"], 9878)
                self.assertEqual(payload["error"]["code"], code)
                self.assertEqual(payload["error"]["execution_state"], state)
                if state != "not_started":
                    self.assertFalse(payload["error"]["retryable"])

    async def test_registered_mcp_tools_return_native_structured_objects(self):
        tool = server.mcp._tool_manager.get_tool("eval_ruby")
        class Context:
            request_id = 1
        with patch.object(server, "_send_ruby_tool", return_value=(
            {"value": {"a": [1, False, None]}}, {"host": "localhost", "port": 9877}
        )):
            response = await tool.run({"code": "probe", "port": 9877}, context=Context(), convert_result=True)
        self.assertEqual(response.structuredContent["result"], {"a": [1, False, None]})
        with patch.object(server, "_send_ruby_tool", side_effect=server.RequestOutcomeUnknown("lost")):
            response = await tool.run({"code": "probe", "port": 9877}, context=Context(), convert_result=True)
        self.assertTrue(response.isError)
        self.assertEqual(response.structuredContent["error"]["code"], "execution_unknown")

    async def test_missing_result_content_does_not_advertise_a_safe_retry(self):
        with patch.object(server, "_send_ruby_tool", return_value=({}, {"host": "localhost", "port": 9877})):
            response = await server.eval_ruby(None, "probe", port=9877)
        self.assertTrue(response.isError)
        self.assertEqual(response.structuredContent["error"]["execution_state"], "unknown")
        self.assertFalse(response.structuredContent["error"]["retryable"])
