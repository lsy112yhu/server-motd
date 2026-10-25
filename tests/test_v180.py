"""v1.8.0 rendering resilience and per-group default query regression tests."""
import asyncio
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web

from test_motd import motd
from render_client import T2IRenderClient, normalize_endpoint
from group_config import parse_addresses, parse_group_server_rows


class Config(dict):
    def save_config(self):
        self.saved = getattr(self, "saved", 0) + 1


class FakeEvent:
    def __init__(self, group_id):
        self.group_id = group_id
        self.send = AsyncMock()
        self.plain_result = lambda value: value
        self.image_result = lambda value: value
        self.stop_event = lambda: None

    def get_group_id(self):
        return self.group_id


def make_plugin(config):
    with tempfile.TemporaryDirectory() as directory, patch.object(
        motd.StarTools, "get_data_dir", return_value=directory
    ):
        return motd.MOTDPlugin(None, config)


class GroupTests(unittest.IsolatedAsyncioTestCase):
    def test_group_config_skip_invalid_and_use_last_valid(self):
        plugin = make_plugin(Config(default_servers=["global.example"]))
        rows = [
            "bad-row", "a=host.example", "123=mc.one:25565,invalid:abc",
            "123=mc.two:25566", "123=bad:abc",
            "456=server.example,another.example",
        ]
        parsed = parse_group_server_rows(
            rows, plugin._parse_server_address, plugin._join_server_address
        )
        self.assertEqual(parsed["123"], ["mc.two:25566"])
        self.assertEqual(parsed["456"], ["server.example:25565", "another.example:25565"])

    def test_group_default_resolution_and_private_fallback(self):
        plugin = make_plugin(Config(default_servers=["global.example"],
            group_servers=["123=mc.a.com,mc2.a.com", "456=mc.b.com"]))
        self.assertEqual(plugin._default_servers_for_event(FakeEvent("123"), True),
                         ["mc.a.com:25565", "mc2.a.com:25565"])
        self.assertEqual(plugin._default_servers_for_event(FakeEvent("456"), True),
                         ["mc.b.com:25565"])
        self.assertEqual(plugin._default_servers_for_event(FakeEvent("999"), True),
                         ["global.example:25565"])
        self.assertEqual(plugin._default_servers_for_event(FakeEvent(None), True),
                         ["global.example:25565"])
        self.assertEqual(plugin._default_servers_for_event(FakeEvent("123"), False),
                         ["global.example:25565"])

    async def test_group_routing_and_explicit_bypass(self):
        plugin = make_plugin(Config(
            default_servers=["global.example"], group_servers=["123=mc.a.com", "456=mc.b.com"]
        ))
        captured = []
        async def query(address, is_java, avatar_limit=16):
            captured.append(address)
            return {"is_error":False, "online":0, "player_list":[], "server_display":address}
        plugin._query_server_context = query
        plugin._get_random_wallpaper_src = AsyncMock(return_value="")
        plugin._render_card = AsyncMock(return_value="https://example.com/result.jpg")
        for group, server in [("123","mc.a.com:25565"),("456","mc.b.com:25565"),
                              ("999","global.example:25565")]:
            captured.clear()
            await plugin._do_motd_query_v2(FakeEvent(group))
            self.assertEqual(captured, [server])
        captured.clear()
        await plugin._do_motd_query_v2(FakeEvent("123"), "manual.example")
        self.assertEqual(captured, ["manual.example:25565"])

    async def test_group_commands_persist_and_clear(self):
        config = Config(default_servers=["global.example"])
        plugin = make_plugin(config)
        plugin._is_admin = lambda event: True
        event = FakeEvent("123")
        await plugin._dispatch_motd(event, "motd", "group set mc.a.com,mc2.a.com")
        self.assertEqual(config["group_servers"],
                         ["123=mc.a.com:25565,mc2.a.com:25565"])
        self.assertEqual(config.saved, 1)
        await plugin._dispatch_motd(event, "motd", "group show")
        self.assertIn("mc.a.com", event.send.await_args.args[0])
        await plugin._dispatch_motd(event, "motd", "group clear")
        self.assertEqual(config["group_servers"], [])
        self.assertEqual(config.saved, 2)

    async def test_group_command_requires_astrbot_admin(self):
        config = Config(group_servers=["123=a.example"])
        plugin = make_plugin(config)
        plugin._is_admin = lambda event: False
        event = FakeEvent("123")
        await plugin._handle_group_config(event, "set b.example")
        self.assertNotIn("saved", vars(config) if hasattr(config, "__dict__") else {})
        self.assertEqual(config["group_servers"], ["123=a.example"])


class T2IClientTests(unittest.IsolatedAsyncioTestCase):
    def test_url_normalization(self):
        self.assertEqual(normalize_endpoint("http://example.com/text2img/generate"),
                         "http://example.com/text2img")
        self.assertEqual(normalize_endpoint("https://example.com"),
                         "https://example.com/text2img")
        self.assertEqual(normalize_endpoint("file:///tmp/evil"), "")

    async def asyncSetUp(self):
        self.slow = False
        self.hits = 0
        self.received = None
        self.image_id = "test-id"
        async def generate(request):
            self.hits += 1
            self.received = await request.json()
            if self.slow:
                await asyncio.sleep(2)
            return web.json_response({"data": {"id": self.image_id}})
        app = web.Application()
        app.router.add_post("/text2img/generate", generate)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.endpoint = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/text2img"

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def test_fast_failover_success_stickiness_and_protocol(self):
        client = T2IRenderClient(
            custom_endpoints=["http://127.0.0.1:1/text2img", self.endpoint],
            endpoint_timeout=0.5, cooldown=300
        )
        url = await client.render("<html>{{ message }}</html>", {"message":"ok"}, 4)
        self.assertEqual(url, self.endpoint + "/test-id")
        self.assertEqual(self.received["tmpldata"], {"message":"ok"})
        self.assertEqual(self.received["options"], {"full_page":True,"type":"jpeg","quality":40})
        self.assertIn("127.0.0.1:1", next(iter(client._failed_until)))
        self.assertEqual(client._last_success, self.endpoint)
        self.assertEqual(await client.render("ok", {}, 4), self.endpoint + "/test-id")
        self.assertEqual(self.hits, 2)

    async def test_path_style_render_id_accepted(self):
        # Official-style services return relative paths, e.g. data/rendered_x.png.
        self.image_id = "data/rendered_42.png"
        client = T2IRenderClient(
            custom_endpoints=[self.endpoint], endpoint_timeout=2, cooldown=300
        )
        url = await client.render("<html>ok</html>", {}, 4)
        self.assertEqual(url, self.endpoint + "/data/rendered_42.png")

    async def test_traversal_render_id_rejected(self):
        self.image_id = "../../etc/passwd"
        client = T2IRenderClient(
            custom_endpoints=[self.endpoint], endpoint_timeout=2, cooldown=300
        )
        self.assertIsNone(await client.render("<html>ok</html>", {}, 4))

    async def test_total_deadline_including_fallback(self):
        client = T2IRenderClient(custom_endpoints=[self.endpoint], endpoint_timeout=1)
        self.slow = True
        async def fallback():
            await asyncio.sleep(3)
            return "should-never-return"
        started = time.monotonic()
        result = await client.render("hello", {}, 0.35, fallback=fallback)
        self.assertIsNone(result)
        self.assertLess(time.monotonic() - started, 0.8)

    async def test_probe_one_when_every_endpoint_cooling(self):
        client = T2IRenderClient(custom_endpoints=["https://a.example/text2img",
                                                  "https://b.example/text2img"])
        now = time.monotonic()
        client._official = []
        client._failed_until = {
            "https://a.example/text2img":now+100,
            "https://b.example/text2img":now+50,
        }
        self.assertEqual(await client._candidate_endpoints(),
                         ["https://b.example/text2img"])


if __name__ == "__main__":
    unittest.main()
