import asyncio
import gc
import json
import unittest
import weakref

from sketchup_mcp import server, startup


class _Session:
    pass


class _Context:
    def __init__(self, session):
        self.session = session
        self.request_id = "request"


class SessionStateTests(unittest.TestCase):
    def setUp(self):
        server.clear_sketchup_port_override()
        server.reset_service_defaults()
        startup.clear_session_autostart_allowed()

    def tearDown(self):
        server.clear_sketchup_port_override()
        server.reset_service_defaults()
        startup.clear_session_autostart_allowed()

    def test_connection_port_isolated_by_session_even_when_client_identity_would_match(self):
        first = _Context(_Session())
        second = _Context(_Session())

        self.assertEqual(json.loads(server.set_connection_port(first, 9877))["port"], 9877)
        self.assertEqual(json.loads(server.set_connection_port(second, 9878))["port"], 9878)

        self.assertEqual(server.get_sketchup_port(ctx=first), 9877)
        self.assertEqual(server.get_sketchup_port(ctx=second), 9878)
        self.assertEqual(server.get_sketchup_port(9879, ctx=first), 9879)

    def test_temporary_autostart_consent_is_isolated_by_session(self):
        first = _Context(_Session())
        second = _Context(_Session())

        allowed = json.loads(server.allow_sketchup_autostart(first, True))

        self.assertTrue(allowed["autostart_allowed"])
        self.assertTrue(startup.autostart_allowed(first.session))
        self.assertFalse(startup.autostart_allowed(second.session))

    def test_session_state_uses_weak_references_for_disconnect_reclamation(self):
        session = _Session()
        context = _Context(session)
        session_ref = weakref.ref(session)

        server.set_connection_port(context, 9877)
        startup.set_session_autostart_allowed(True, session=session)
        del context
        del session
        gc.collect()

        self.assertIsNone(session_ref())

    def test_service_default_precedes_environment_only_when_session_has_no_default(self):
        first = _Context(_Session())
        server.configure_service_defaults(sketchup_port=9877)

        self.assertEqual(server.get_sketchup_port(ctx=first), 9877)
        server.set_connection_port(first, 9878)
        self.assertEqual(server.get_sketchup_port(ctx=first), 9878)

    def test_real_session_context_reaches_send_path_with_context(self):
        context = _Context(_Session())
        server.set_connection_port(context, 9877)
        calls = []

        async def fake_send(name, arguments, request_id, *, port=None, ctx=None, **_kwargs):
            calls.append((name, port, ctx))
            return {"content": [{"text": "ok"}]}, {"host": "localhost", "port": 9877}

        original = server._send_ruby_tool
        server._send_ruby_tool = fake_send
        try:
            asyncio.run(server.get_selection(context))
        finally:
            server._send_ruby_tool = original

        self.assertEqual(calls, [("get_selection", None, context)])
