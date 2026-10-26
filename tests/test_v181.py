"""v1.8.1 玩家头像身份解析回归测试：离线 v3 UUID 不得冒充正版身份。"""
import asyncio
import importlib.util
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from yarl import URL

ROOT = Path(__file__).resolve().parents[1]


def load_plugin():
    modules = {name: types.ModuleType(name) for name in (
        'astrbot', 'astrbot.api', 'astrbot.api.event', 'astrbot.api.star',
        'astrbot.api.message_components', 'astrbot.core', 'astrbot.core.star',
        'astrbot.core.star.star_tools', 'astrbot.core.star.filter',
        'astrbot.core.star.filter.command')}

    class Filters:
        EventMessageType = types.SimpleNamespace(ALL='all')

        def command(self, name, **kwargs):
            return lambda fn: fn

        def __getattr__(self, name):
            return lambda *args, **kwargs: lambda fn: fn

    class Star:
        def __init__(self, context):
            self.context = context

    modules['astrbot.api'].logger = logging.getLogger('motd-tests-v181')
    modules['astrbot.api'].AstrBotConfig = dict
    modules['astrbot.api.event'].filter = Filters()
    modules['astrbot.api.event'].AstrMessageEvent = object
    modules['astrbot.api.star'].Context = object
    modules['astrbot.api.star'].Star = Star
    modules['astrbot.api.star'].register = lambda *a, **k: lambda cls: cls
    modules['astrbot.core.star.star_tools'].StarTools = types.SimpleNamespace(get_data_dir=lambda _: '')
    modules['astrbot.core.star.filter.command'].GreedyStr = str
    spec = importlib.util.spec_from_file_location('motd_v181', ROOT / 'main.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


motd = load_plugin()

V4_UUID = "f4706c34-d91a-4576-a975-5ee892b90f26"      # Mojang 签发（Macaron_）
V3_UUID = "7f792ff7-fd8b-37b3-907c-6f13b7637c79"      # 离线模式名字派生
ZERO_UUID = "00000000-0000-0000-0000-000000000000"
PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-head"


def make_plugin():
    plugin = motd.MOTDPlugin.__new__(motd.MOTDPlugin)
    plugin._avatar_cache = {}
    plugin._premium_uuid_cache = {}
    return plugin


class _Content:
    def __init__(self, body):
        self._body = body

    async def _gen(self):
        yield self._body

    def iter_chunked(self, _size):
        return self._gen()


class _Response:
    def __init__(self, url, status=200, body=b"", json_data=None, headers=None):
        self.url = URL(url)
        self.status = status
        self.headers = headers or {}
        self.content = _Content(body)
        self._json_data = json_data

    async def json(self, content_type=None):
        return self._json_data


class _RequestContext:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """按 URL 子串路由的假会话，记录全部请求过的 URL。"""

    def __init__(self, routes):
        self.routes = routes  # list[(substring, _Response | Exception)]
        self.requested = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        for needle, outcome in self.routes:
            if needle in url:
                if isinstance(outcome, Exception):
                    raise outcome
                return _RequestContext(outcome)
        raise AssertionError(f"unexpected URL: {url}")


def png_response(url):
    return _Response(url, status=200, body=PNG_BYTES,
                     headers={"Content-Type": "image/png"})


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.plugin = make_plugin()

    def test_v4_uuid_wins(self):
        ident, key = self.plugin._player_avatar_identity({"id": V4_UUID, "name": "Macaron_"})
        self.assertEqual(ident, V4_UUID.replace("-", ""))
        self.assertTrue(key.startswith("uuid:"))

    def test_v3_uuid_falls_back_to_name(self):
        ident, key = self.plugin._player_avatar_identity({"id": V3_UUID, "name": "Macaron_"})
        self.assertEqual(ident, "Macaron_")
        self.assertEqual(key, "name:macaron_")

    def test_zero_uuid_anonymous_has_no_identity(self):
        ident, key = self.plugin._player_avatar_identity({"id": ZERO_UUID, "name": "Anonymous Player"})
        self.assertEqual((ident, key), ("", ""))

    def test_name_only(self):
        ident, key = self.plugin._player_avatar_identity({"name": "Notch"})
        self.assertEqual(ident, "Notch")
        self.assertEqual(key, "name:notch")


class FetchTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, plugin, session, player):
        return await plugin._fetch_player_avatar(session, asyncio.Semaphore(4), player)

    async def test_v3_uuid_resolves_name_to_premium_uuid(self):
        plugin = make_plugin()
        session = FakeSession([
            ("api.minecraftservices.com", _Response(
                "https://api.minecraftservices.com/minecraft/profile/lookup/name/Macaron_",
                json_data={"id": "f4706c34d91a4576a9755ee892b90f26", "name": "Macaron_"})),
            ("mc-heads.net", png_response("https://mc-heads.net/avatar/f4706c34d91a4576a9755ee892b90f26/64.png")),
        ])
        result = await self.fetch(plugin, session, {"id": V3_UUID, "name": "Macaron_"})
        self.assertTrue(result.startswith("data:image/png;base64,"))
        avatar_urls = [u for u in session.requested if "mc-heads" in u or "minotar" in u]
        self.assertEqual(len(avatar_urls), 1)
        self.assertIn("f4706c34d91a4576a9755ee892b90f26", avatar_urls[0])
        self.assertNotIn("7f792ff7", avatar_urls[0])

    async def test_unknown_name_falls_back_to_steve(self):
        plugin = make_plugin()
        session = FakeSession([
            ("api.minecraftservices.com", _Response(
                "https://api.minecraftservices.com/minecraft/profile/lookup/name/Cracked99", status=404)),
            ("mc-heads.net", png_response("https://mc-heads.net/avatar/MHF_Steve/64.png")),
        ])
        result = await self.fetch(plugin, session, {"id": V3_UUID, "name": "Cracked99"})
        self.assertTrue(result.startswith("data:image/png;base64,"))
        avatar_urls = [u for u in session.requested if "mc-heads" in u or "minotar" in u]
        self.assertTrue(any("MHF_Steve" in u for u in avatar_urls))

    async def test_anonymous_entry_falls_back_to_steve(self):
        plugin = make_plugin()
        session = FakeSession([
            ("mc-heads.net", png_response("https://mc-heads.net/avatar/MHF_Steve/64.png")),
        ])
        result = await self.fetch(plugin, session, {"id": ZERO_UUID, "name": "Anonymous Player"})
        self.assertTrue(result.startswith("data:image/png;base64,"))
        self.assertFalse(any("minecraftservices" in u or "api.mojang" in u for u in session.requested))
        self.assertTrue(any("MHF_Steve" in u for u in session.requested))

    async def test_v4_uuid_skips_mojang_lookup(self):
        plugin = make_plugin()
        session = FakeSession([
            ("mc-heads.net", png_response("https://mc-heads.net/avatar/f4706c34d91a4576a9755ee892b90f26/64.png")),
        ])
        result = await self.fetch(plugin, session, {"id": V4_UUID, "name": "Macaron_"})
        self.assertTrue(result.startswith("data:image/png;base64,"))
        self.assertFalse(any("minecraftservices" in u or "api.mojang" in u for u in session.requested))

    async def test_mojang_outage_falls_back_to_name_fetch(self):
        plugin = make_plugin()
        session = FakeSession([
            ("api.minecraftservices.com", ConnectionError("down")),
            ("api.mojang.com", ConnectionError("down")),
            ("mc-heads.net", _Response("https://mc-heads.net/avatar/Macaron_/64.png", status=403)),
            ("minotar.net", png_response("https://minotar.net/helm/Macaron_/64.png")),
        ])
        result = await self.fetch(plugin, session, {"id": V3_UUID, "name": "Macaron_"})
        self.assertTrue(result.startswith("data:image/png;base64,"))
        self.assertTrue(any("minotar.net/helm/Macaron_" in u for u in session.requested))


if __name__ == "__main__":
    unittest.main()
