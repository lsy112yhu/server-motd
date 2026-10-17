"""Network regression tests. Only AstrBot framework dependencies are replaced."""
import asyncio
import base64
import importlib.util
import json
import logging
import socket
import struct
import sys
import tempfile
import time
import types
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web
from jinja2 import Environment, StrictUndefined
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = []


def load_plugin():
    modules = {name: types.ModuleType(name) for name in (
        'astrbot', 'astrbot.api', 'astrbot.api.event', 'astrbot.api.star',
        'astrbot.api.message_components', 'astrbot.core', 'astrbot.core.star',
        'astrbot.core.star.star_tools', 'astrbot.core.star.filter',
        'astrbot.core.star.filter.command')}

    class Filters:
        EventMessageType = types.SimpleNamespace(ALL='all')

        def command(self, name, **kwargs):
            COMMANDS.append((name, kwargs))
            return lambda fn: fn

        def __getattr__(self, name):
            return lambda *args, **kwargs: lambda fn: fn

    class Star:
        def __init__(self, context):
            self.context = context

    modules['astrbot.api'].logger = logging.getLogger('motd-tests')
    modules['astrbot.api'].AstrBotConfig = dict
    modules['astrbot.api.event'].filter = Filters()
    modules['astrbot.api.event'].AstrMessageEvent = object
    modules['astrbot.api.star'].Context = object
    modules['astrbot.api.star'].Star = Star
    modules['astrbot.api.star'].register = lambda *a, **k: lambda cls: cls
    modules['astrbot.core.star.star_tools'].StarTools = types.SimpleNamespace(get_data_dir=lambda _: '')
    modules['astrbot.core.star.filter.command'].GreedyStr = str
    spec = importlib.util.spec_from_file_location('motd_under_test', ROOT / 'main.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


motd = load_plugin()
ALLOW_LOCAL = ['127.0.0.1/32']


def varint(value):
    value &= 0xffffffff
    result = bytearray()
    while True:
        byte = value & 127
        value >>= 7
        result.append(byte | (128 if value else 0))
        if not value:
            return bytes(result)


async def read_packet(reader):
    size = 0
    for shift in range(0, 35, 7):
        byte = (await reader.readexactly(1))[0]
        size |= (byte & 127) << shift
        if not byte & 128:
            return await reader.readexactly(size)
    raise ValueError('invalid test peer VarInt')


def png_uri():
    stream = BytesIO()
    Image.new('RGB', (64, 64), '#336699').save(stream, 'PNG')
    return 'data:image/png;base64,' + base64.b64encode(stream.getvalue()).decode()


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connections = []
        self.tasks = set()
        self.received = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.delay = 0
        self.response_mode = 'valid'
        self.status = {'version': {'name': '1.21.8', 'protocol': 772},
                       'players': {'online': 17, 'max': 40, 'sample': [{'name': f'Player{i}'} for i in range(17)]},
                       'description': '§aRegression server', 'favicon': png_uri()}

        async def handle(reader, writer):
            task = asyncio.current_task()
            self.tasks.add(task)
            self.connections.append(writer)
            try:
                handshake = await read_packet(reader)
                self.assertEqual(handshake[:6], b'\x00\xff\xff\xff\xff\x0f')
                self.assertEqual(await read_packet(reader), b'\x00')
                self.received.set()
                if self.response_mode == 'stall':
                    await reader.read()
                    return
                await asyncio.sleep(self.delay)
                data = json.dumps(self.status).encode()
                packet = b'\x00' + varint(len(data)) + data
                response = varint(len(packet)) + packet
                if self.response_mode == 'oversize':
                    response = varint(2 * 1024 * 1024 + 1)
                elif self.response_mode == 'bad_varint':
                    response = b'\xff' * 5
                elif self.response_mode == 'truncated':
                    response = response[:-10]
                writer.write(response)
                await writer.drain()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()
                self.tasks.discard(task)
                self.disconnected.set()

        self.server = await asyncio.start_server(handle, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.udp_packets = []
        self.udp_delay = 0
        self.udp_mode = 'valid'
        owner = self

        class BedrockPeer(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, data, addr):
                owner.udp_packets.append(data)
                if len(data) != 33 or data[9:25] != motd.RAKNET_MAGIC:
                    return
                owner.received.set()
                if owner.udp_mode == 'stall':
                    return
                text = b'MCPE;Bedrock regression;975;1.21.0;2;20;123;'
                pong = b'\x1c' + data[1:9] + struct.pack('>Q', 123) + motd.RAKNET_MAGIC + struct.pack('>H', len(text)) + text
                if owner.udp_mode == 'bad_then_valid':
                    self.transport.sendto(b'\x1c' + b'\x00' * 8 + pong[9:], addr)
                    self.transport.sendto(pong[:-1], addr)
                asyncio.get_running_loop().call_later(owner.udp_delay, self.transport.sendto, pong, addr)

        self.udp_transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(BedrockPeer, local_addr=('127.0.0.1', 0))
        self.udp_port = self.udp_transport.get_extra_info('sockname')[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        self.udp_transport.close()
        for writer in self.connections:
            writer.close()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def test_java_parallel_and_event_loop_responsive(self):
        self.delay = 0.2
        ticks = []
        async def ticker():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.01)
        tick_task = asyncio.create_task(ticker())
        await asyncio.sleep(0)
        started = time.monotonic()
        try:
            results = await asyncio.gather(*(motd.query_java_server_direct('127.0.0.1', self.port, 2, ALLOW_LOCAL) for _ in range(3)))
        finally:
            tick_task.cancel()
            await asyncio.gather(tick_task, return_exceptions=True)
        self.assertTrue(all('error' not in x for x in results))
        self.assertLess(time.monotonic() - started, 0.55)
        self.assertLess(max(b-a for a,b in zip(ticks,ticks[1:])), 0.15)

    async def test_java_timeout_closes_connection(self):
        self.response_mode = 'stall'
        started = time.monotonic()
        result = await motd.query_java_server_direct('127.0.0.1', self.port, 0.05, ALLOW_LOCAL)
        self.assertIn('error', result)
        self.assertLess(time.monotonic()-started, 0.3)
        await asyncio.wait_for(self.disconnected.wait(), 1)

    async def test_java_external_cancel_closes_connection(self):
        self.response_mode = 'stall'
        task = asyncio.create_task(motd.query_java_server_direct('127.0.0.1', self.port, 2, ALLOW_LOCAL))
        await asyncio.wait_for(self.received.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(self.disconnected.wait(), 1)

    async def test_java_rejects_invalid_packets(self):
        for mode in ['oversize', 'bad_varint', 'truncated']:
            with self.subTest(mode=mode):
                self.response_mode = mode
                self.assertIn('error', await motd.query_java_server_direct('127.0.0.1', self.port, 1, ALLOW_LOCAL))

    async def test_dns_is_resolved_once_and_pinned(self):
        loop = asyncio.get_running_loop()
        info = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', self.port))]
        with patch.object(loop, 'getaddrinfo', AsyncMock(return_value=info)) as resolve:
            result = await motd.query_java_server('status.example', self.port, 1, False, ALLOW_LOCAL)
        self.assertNotIn('error', result)
        self.assertEqual(resolve.await_count, 1)

    async def test_udp_packet_and_malformed_pongs(self):
        self.udp_mode = 'bad_then_valid'
        result = await motd.query_bedrock_server('127.0.0.1', self.udp_port, 1, ALLOW_LOCAL)
        self.assertNotIn('error', result)
        self.assertEqual(result['online_players'], '2')
        self.assertEqual(len(self.udp_packets[0]), 33)
        self.assertEqual(self.udp_packets[0][9:25], motd.RAKNET_MAGIC)

    async def test_udp_parallel_and_timeout(self):
        self.udp_delay = 0.15
        started = time.monotonic()
        results = await asyncio.gather(*(motd.query_bedrock_server('127.0.0.1', self.udp_port, 1, ALLOW_LOCAL) for _ in range(3)))
        self.assertTrue(all('error' not in x for x in results))
        self.assertLess(time.monotonic()-started, 0.4)
        self.udp_mode = 'stall'
        started = time.monotonic()
        self.assertIn('error', await motd.query_bedrock_server('127.0.0.1', self.udp_port, 0.05, ALLOW_LOCAL))
        self.assertLess(time.monotonic()-started, 0.3)

    async def test_udp_cancel(self):
        self.udp_mode = 'stall'
        task = asyncio.create_task(motd.query_bedrock_server('127.0.0.1', self.udp_port, 2, ALLOW_LOCAL))
        await asyncio.wait_for(self.received.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_real_query_to_render_context_for_single_and_multi_commands(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(motd.StarTools, 'get_data_dir', return_value=directory):
            config = {'default_servers': [f'127.0.0.1:{self.port}'] * 2,
                      'private_server_allowlist': ALLOW_LOCAL, 'use_api': False,
                      'enable_random_wallpaper': False, 'enable_player_avatars': False}
            plugin = motd.MOTDPlugin(None, config)
            rendered = []
            async def render(template, context, **kwargs):
                html = Environment(undefined=StrictUndefined).from_string(template).render(context)
                self.assertTrue(html.startswith('<!DOCTYPE html>'))
                rendered.append((template, context, html))
                return 'test-render.png'
            plugin.html_render = render
            event = types.SimpleNamespace(send=AsyncMock(), plain_result=lambda x:x,
                                          image_result=lambda x:x, stop_event=lambda:None)
            for command in [f'/motd 127.0.0.1:{self.port}', f'motd 127.0.0.1:{self.port}', f'在线 127.0.0.1:{self.port}']:
                event.message_str = command
                await plugin.on_message(event)
                self.assertEqual(len(rendered[-1][1]['player_list']), 15)
                self.assertEqual(rendered[-1][1]['extra_count'], 2)
                self.assertEqual(rendered[-1][1]['plugin_version'], motd.PLUGIN_VERSION)
                self.assertTrue(rendered[-1][1]['server_icon'].startswith('data:image/png;base64,'))
            plugin.default_servers = [f'127.0.0.1:{self.port}'] * 3
            event.message_str = '/motd'
            await plugin.on_message(event)
            self.assertEqual(len(rendered[-1][1]['servers']), 3)
            self.assertIn('MC-MOTD-V' + motd.PLUGIN_VERSION, rendered[-1][2])
            self.assertNotIn('MC-MOTD-V1.8.0', rendered[-1][2])


class PolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_nonpublic_and_mixed_dns(self):
        loop = asyncio.get_running_loop()
        for ip in ['127.0.0.1', '10.0.0.1', '172.16.0.1', '192.168.1.1', '169.254.169.254',
                   '100.64.0.1', '0.0.0.0', '224.0.0.1', '240.0.0.1', '::1', 'fc00::1',
                   'fe80::1', 'ff02::1', '::ffff:127.0.0.1']:
            family = socket.AF_INET6 if ':' in ip else socket.AF_INET
            info = [(family, socket.SOCK_STREAM, 0, '', (ip, 25565))]
            with self.subTest(ip=ip), patch.object(loop, 'getaddrinfo', AsyncMock(return_value=info)):
                with self.assertRaises(ValueError):
                    await motd._resolve_query_addresses('server.example', 25565, socket.SOCK_STREAM)
        info = [(socket.AF_INET,socket.SOCK_STREAM,0,'',(ip,25565)) for ip in ['8.8.8.8','127.0.0.1']]
        with patch.object(loop,'getaddrinfo',AsyncMock(return_value=info)):
            with self.assertRaises(ValueError):
                await motd._resolve_query_addresses('mixed.example',25565,socket.SOCK_STREAM)

    async def test_private_allowlist_is_exact_and_does_not_allow_multicast(self):
        loop = asyncio.get_running_loop()
        for ip, allowed in [('192.168.1.10',True),('192.168.1.11',False),('224.0.0.1',False)]:
            info = [(socket.AF_INET,socket.SOCK_STREAM,0,'',(ip,25565))]
            with self.subTest(ip=ip), patch.object(loop,'getaddrinfo',AsyncMock(return_value=info)):
                if allowed:
                    self.assertTrue(await motd._resolve_query_addresses('s.example',25565,socket.SOCK_STREAM,['192.168.1.10/32']))
                else:
                    with self.assertRaises(ValueError):
                        await motd._resolve_query_addresses('s.example',25565,socket.SOCK_STREAM,['192.168.1.10/32'])

    async def test_rejects_before_api_tcp_or_udp_connection(self):
        with patch.object(motd.aiohttp,'ClientSession') as session, patch.object(motd.asyncio,'open_connection',AsyncMock()) as connect:
            self.assertIn('error',await motd.query_java_server('127.0.0.1',25565))
            self.assertIn('error',await motd.query_java_server_api('127.0.0.1',25565))
            self.assertIn('error',await motd.query_java_server_direct('127.0.0.1',25565))
            self.assertIn('error',await motd.query_bedrock_server('127.0.0.1',19132))
            self.assertEqual(await motd._download_server_icon('http://127.0.0.1/a.png'),'')
            session.assert_not_called()
            connect.assert_not_awaited()

    async def test_bad_addresses_and_ports(self):
        for host, port in [('',25565),('a/b',25565),('user@host',25565),('a%00',25565),('host',0),('host',65536)]:
            with self.subTest(host=host,port=port), self.assertRaises(ValueError):
                await motd._resolve_query_addresses(host,port,socket.SOCK_STREAM)

    async def test_icon_download_is_pinned_bounded_and_has_no_redirects(self):
        calls = []
        image = base64.b64decode(png_uri().split(',')[1])
        async def handler(request):
            calls.append(request.path)
            if request.path == '/redirect':
                raise web.HTTPFound('/image')
            if request.path == '/large':
                return web.Response(body=b'x'*(motd.SERVER_ICON_MAX_BYTES+1))
            if request.path == '/invalid':
                return web.Response(body=b'not an image')
            return web.Response(body=image,content_type='image/png')
        app = web.Application()
        app.router.add_get('/{path}',handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner,'127.0.0.1',0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        info = [(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',('127.0.0.1',port))]
        try:
            # Transport test only: policy rejection is exercised independently above.
            with patch.object(motd,'_resolve_query_addresses',AsyncMock(return_value=info)) as resolver:
                self.assertEqual(await motd._download_server_icon(f'http://icon.example:{port}/image'),png_uri())
                self.assertEqual(resolver.await_count,1)
                for path in ['redirect','large','invalid']:
                    self.assertEqual(await motd._download_server_icon(f'http://icon.example:{port}/{path}'),'')
            self.assertEqual(calls.count('/image'),1)
        finally:
            await runner.cleanup()


class MessageMetadataTests(unittest.IsolatedAsyncioTestCase):
    def test_strip_trailing_msg_id_metadata(self):
        self.assertEqual(
            motd._strip_message_metadata('/motd set1 pl.okmc.cc:55141 [MSG_ID:25565]'),
            '/motd set1 pl.okmc.cc:55141',
        )
        self.assertEqual(motd._strip_message_metadata('/motd [MSG_ID:123456]'), '/motd')
        self.assertEqual(
            motd._strip_message_metadata('/motd example.org:25565'),
            '/motd example.org:25565',
        )

    def test_address_parser_defensively_ignores_msg_id_suffix(self):
        plugin = object.__new__(motd.MOTDPlugin)
        self.assertEqual(
            plugin._parse_server_address('pl.okmc.cc:55141 [MSG_ID:987654]', True),
            ('pl.okmc.cc', 55141),
        )

    async def test_dispatch_strips_msg_id_from_set_and_empty_query(self):
        plugin = object.__new__(motd.MOTDPlugin)
        plugin._handle_motd_config = AsyncMock()
        plugin._do_motd_query_v2 = AsyncMock()
        event = types.SimpleNamespace(stop_event=lambda: None)

        await plugin._dispatch_motd(
            event, 'motd', 'set1 pl.okmc.cc:55141 [MSG_ID:25565]'
        )
        plugin._handle_motd_config.assert_awaited_once_with(
            event, 'set1', 'pl.okmc.cc:55141'
        )
        plugin._do_motd_query_v2.assert_not_awaited()

        plugin._handle_motd_config.reset_mock()
        plugin._do_motd_query_v2.reset_mock()
        await plugin._dispatch_motd(event, 'motd', '[MSG_ID:123456]')
        plugin._handle_motd_config.assert_not_awaited()
        plugin._do_motd_query_v2.assert_awaited_once_with(
            event, '', is_java=True
        )


class VersionAndTemplateTests(unittest.TestCase):
    def test_protocols_and_reverse_ranges(self):
        expected = {'1.21.7':772,'1.21.8':772,'26.1':775,'26.1.1':775,'26.1.2':775,
                    '26.2':776,'26.3':777,'1.7.3':4,'1.7.8':5,'1.8.9':47,'1.10.2':210,
                    '1.11':315,'1.11.2':316,'1.13.2':404,'Velocity 3.4.0 1.21.8':772,
                    'Velocity 1.8 - 26.3':777}
        for name, protocol in expected.items():
            with self.subTest(name=name):
                self.assertEqual(motd._lookup_protocol_from_name(name),protocol)
        self.assertIsNone(motd._lookup_protocol_from_name('1.8.99'))

    def test_version_parsing(self):
        plugin = object.__new__(motd.MOTDPlugin)
        server, client, hint = plugin._parse_version({'name':'Velocity 1.8 - 26.3','protocol':-1})
        self.assertEqual(server,'26.3')
        self.assertEqual(client,'1.8 ~ 26.3')
        self.assertIn('Velocity',hint)

    def test_icons_only_accept_verified_raster_data(self):
        self.assertEqual(motd._validated_icon_src(png_uri()),png_uri())
        for value in ['http://127.0.0.1/x','https://example.com/x','data:image/svg+xml;base64,PHN2Zy8+',
                      'data:image/png;base64,bm90LWltYWdl','A'*(motd.SERVER_ICON_MAX_BYTES*2+1)]:
            self.assertEqual(motd._validated_icon_src(value),'')

    def test_templates_schema_and_version(self):
        env = Environment()
        for template in [motd.MOTD_HTML_TEMPLATE,motd.MOTD_STATUS_TEMPLATE,motd.MOTD_DUAL_TEMPLATE]:
            self.assertTrue(template.startswith('<!DOCTYPE html>'))
            env.parse(template)
        self.assertIn('{{ plugin_version }}',motd.MOTD_DUAL_TEMPLATE)
        schema = json.loads((ROOT/'_conf_schema.json').read_text(encoding='utf-8-sig'))
        self.assertEqual(schema['private_server_allowlist']['default'],[])
        self.assertIn('version: '+motd.PLUGIN_VERSION,(ROOT/'metadata.yaml').read_text(encoding='utf-8-sig'))
        self.assertTrue(any(name=='motd' and '/motd' in options['alias'] for name,options in COMMANDS))


if __name__ == '__main__':
    unittest.main()
