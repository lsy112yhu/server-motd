"""v1.9.0 group-aware set1/set2/set3, remove and get regression tests."""
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from test_motd import motd


class Config(dict):
    def save_config(self):
        self.saved = getattr(self, "saved", 0) + 1


class FakeEvent:
    def __init__(self, group_id):
        self.group_id = group_id
        self.sent = []
        self.send = AsyncMock(side_effect=self._record)
        self.plain_result = lambda value: value
        self.image_result = lambda value: value
        self.stop_event = lambda: None

    async def _record(self, value):
        self.sent.append(value)

    def get_group_id(self):
        return self.group_id


def make_plugin(config):
    with tempfile.TemporaryDirectory() as directory, patch.object(
        motd.StarTools, "get_data_dir", return_value=directory
    ):
        plugin = motd.MOTDPlugin(None, config)
    plugin._is_admin = lambda event: True
    return plugin


class GroupAwareConfigTests(unittest.IsolatedAsyncioTestCase):
    async def dispatch_set(self, plugin, event, argument):
        with patch.object(motd, "query_java_server", new=AsyncMock(return_value={})):
            await plugin._dispatch_motd(event, "motd", argument)

    async def test_group_set1_updates_bound_group_only(self):
        config = Config(default_servers=["global.example"],
                        group_servers=["123=a.example,b.example"])
        plugin = make_plugin(config)
        event = FakeEvent("123")
        await self.dispatch_set(plugin, event, "set1 new.example")
        self.assertEqual(config["group_servers"],
                         ["123=new.example:25565,b.example:25565"])
        self.assertEqual(plugin.default_servers, ["global.example:25565"])
        self.assertEqual(plugin.default_server, "global.example")

    async def test_group_set1_creates_binding_from_defaults(self):
        config = Config(default_servers=["g1.example", "g2.example"])
        plugin = make_plugin(config)
        event = FakeEvent("999")
        await self.dispatch_set(plugin, event, "set1 x.example")
        self.assertEqual(config["group_servers"],
                         ["999=x.example:25565,g2.example:25565"])
        self.assertEqual(plugin.default_servers, ["g1.example:25565", "g2.example:25565"])
        self.assertIn("已为本群创建专属绑定", event.sent[-1])

    async def test_group_set2_requires_first_slot(self):
        config = Config(default_servers=[])
        plugin = make_plugin(config)
        event = FakeEvent("999")
        await self.dispatch_set(plugin, event, "set2 x.example")
        self.assertNotIn("group_servers", config)
        self.assertIn("请先使用 /motd set1", event.sent[-1])

    async def test_group_set_rejects_duplicate_within_group(self):
        config = Config(default_servers=["global.example"],
                        group_servers=["123=a.example,b.example"])
        plugin = make_plugin(config)
        event = FakeEvent("123")
        await self.dispatch_set(plugin, event, "set1 b.example")
        self.assertEqual(config["group_servers"], ["123=a.example,b.example"])
        self.assertIn("不能重复", event.sent[-1])

    async def test_private_set1_still_updates_global(self):
        config = Config(default_servers=["global.example"],
                        group_servers=["123=a.example"])
        plugin = make_plugin(config)
        event = FakeEvent(None)
        await self.dispatch_set(plugin, event, "set1 p.example")
        self.assertEqual(config["default_servers"], ["p.example:25565"])
        self.assertEqual(config["group_servers"], ["123=a.example"])

    async def test_group_remove_shifts_within_group_only(self):
        config = Config(default_servers=["global.example"],
                        group_servers=["123=a.example,b.example"])
        plugin = make_plugin(config)
        event = FakeEvent("123")
        await plugin._dispatch_motd(event, "motd", "remove set1")
        self.assertEqual(config["group_servers"], ["123=b.example:25565"])
        self.assertEqual(plugin.default_servers, ["global.example:25565"])

    async def test_group_remove_last_clears_binding(self):
        config = Config(default_servers=["global.example"],
                        group_servers=["456=only.example"])
        plugin = make_plugin(config)
        event = FakeEvent("456")
        await plugin._dispatch_motd(event, "motd", "remove 1")
        self.assertEqual(config["group_servers"], [])
        self.assertIn("回退使用全局默认", event.sent[-1])

    async def test_group_get_shows_group_scope(self):
        config = Config(default_servers=["global.example"],
                        group_servers=["123=a.example"])
        plugin = make_plugin(config)
        bound_event = FakeEvent("123")
        await plugin._dispatch_motd(bound_event, "motd", "get")
        self.assertIn("群 123", bound_event.sent[-1])
        self.assertIn("本群专属绑定", bound_event.sent[-1])
        self.assertIn("a.example", bound_event.sent[-1])

        unbound_event = FakeEvent("999")
        await plugin._dispatch_motd(unbound_event, "motd", "get")
        self.assertIn("全局默认", unbound_event.sent[-1])
        self.assertIn("global.example", unbound_event.sent[-1])

    async def test_private_get_shows_global(self):
        config = Config(default_servers=["global.example"])
        plugin = make_plugin(config)
        event = FakeEvent("")
        await plugin._dispatch_motd(event, "motd", "get")
        self.assertIn("MOTD 默认服务器", event.sent[-1])
        self.assertIn("global.example", event.sent[-1])


if __name__ == "__main__":
    unittest.main()
