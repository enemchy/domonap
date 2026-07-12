import importlib.util
import sys
import types
import unittest
from pathlib import Path


class NotifyConsumerPayloadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        aiohttp = types.ModuleType("aiohttp")
        setattr(aiohttp, "ClientWebSocketResponse", object)
        setattr(aiohttp, "ClientError", Exception)
        setattr(aiohttp, "ServerTimeoutError", TimeoutError)
        setattr(
            aiohttp,
            "WSServerHandshakeError",
            type("WSServerHandshakeError", (Exception,), {"status": 0, "headers": {}}),
        )
        setattr(
            aiohttp,
            "WSMsgType",
            types.SimpleNamespace(TEXT="TEXT", PING="PING", CLOSED="CLOSED", ERROR="ERROR"),
        )
        sys.modules.setdefault("aiohttp", aiohttp)

        core = types.ModuleType("homeassistant.core")
        setattr(core, "HomeAssistant", object)
        client = types.ModuleType("homeassistant.helpers.aiohttp_client")
        setattr(client, "async_get_clientsession", lambda hass: None)
        sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
        sys.modules.setdefault("homeassistant.core", core)
        sys.modules.setdefault("homeassistant.helpers", types.ModuleType("homeassistant.helpers"))
        sys.modules.setdefault("homeassistant.helpers.aiohttp_client", client)

        package = types.ModuleType("custom_components.domonap")
        package.__path__ = [str(Path(__file__).parents[1] / "custom_components" / "domonap")]
        sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
        sys.modules.setdefault("custom_components.domonap", package)

        api = types.ModuleType("custom_components.domonap.api")
        setattr(api, "IntercomAPI", object)
        sys.modules.setdefault("custom_components.domonap.api", api)

        const = types.ModuleType("custom_components.domonap.const")
        setattr(const, "EVENT_INCOMING_CALL", "domonap_incoming_call")
        setattr(const, "WS_MESSAGE_END", "\x1e")
        setattr(const, "WS_HANDSHAKE_MESSAGE", '{"protocol":"json","version":1}\x1e')
        setattr(const, "WS_KEEPALIVE_INTERVAL", 15)
        setattr(const, "WS_PING_MESSAGE", '{"type":6}\x1e')
        setattr(const, "WS_SERVER_TIMEOUT", 30)
        setattr(const, "WS_URL", "wss://api.domonap.ru/notificationHub/?id=")
        sys.modules.setdefault("custom_components.domonap.const", const)

        path = Path(__file__).parents[1] / "custom_components" / "domonap" / "notify_consumer.py"
        spec = importlib.util.spec_from_file_location(
            "custom_components.domonap.notify_consumer", path
        )
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        cls.consumer = module.IntercomNotifyConsumer

    def test_extracts_legacy_third_argument_payload(self):
        payload = {"EventMessage": "DomofonCalling", "DoorId": "door-1"}
        args = ["user", "channel", payload]

        self.assertIs(self.consumer._extract_push_payload(args), payload)

    def test_extracts_wrapped_json_payload(self):
        args = [
            "user",
            {"payload": '{"PushType":"Domofon","DoorId":"door-2","CallId":"call-1"}'},
        ]

        payload = self.consumer._extract_push_payload(args)

        self.assertEqual(payload["DoorId"], "door-2")
        self.assertTrue(self.consumer._is_incoming_call_payload(payload))

    def test_recognizes_incoming_call_without_event_message(self):
        payload = {"PushType": "Domofon", "DoorId": "door-3"}

        self.assertTrue(self.consumer._is_incoming_call_payload(payload))

    def test_redacts_sensitive_values_from_summary(self):
        summary = self.consumer._summarize_payload(
            {"SipPassword": "secret", "access_token": "token", "DoorId": "door"}
        )

        self.assertIn("***", summary)
        self.assertNotIn("secret", summary)
        self.assertNotIn('"token"', summary)


if __name__ == "__main__":
    unittest.main()
