"""
AstrBot MOTD 查询插件
用于查询 Minecraft 服务器状态
"""

import socket
import struct
import json
import time
import re
import asyncio
import base64
import hashlib
import random
from io import BytesIO
from pathlib import Path
import aiohttp
from urllib.parse import quote, urlparse
from typing import Optional, Dict, Any, Tuple

from PIL import Image as PILImage, ImageOps

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.core.star.star_tools import StarTools
from astrbot.core.star.filter.command import GreedyStr
import astrbot.api.message_components as Comp


# Java 版标准端口
JAVA_DEFAULT_PORT = 25565
# 基岩版标准端口
BEDROCK_DEFAULT_PORT = 19132

PLUGIN_NAME = "astrbot_plugin_minecraft_motd"
PLUGIN_VERSION = "1.7.5"
WALLPAPER_API_BASE = "https://t.alcy.cc/json/"
WALLPAPER_ALLOWED_HOSTS = {"t.alcy.cc", "tc.alcy.cc"}
WALLPAPER_ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}
WALLPAPER_MAX_BYTES = 6 * 1024 * 1024

AVATAR_ALLOWED_HOSTS = {"mc-heads.net", "www.mc-heads.net", "minotar.net", "www.minotar.net"}
AVATAR_MAX_BYTES = 256 * 1024
AVATAR_DISPLAY_LIMIT = 6

# ============================================================
# 协议号 → 版本名 完整映射（2026-06 更新）
# 格式: protocol: (显示版本, 主版本号)
# 注: 26.x 起 Minecraft 采用年份.版本的新命名规则
# ============================================================
PROTOCOL_VERSION_MAP: Dict[int, Tuple[str, str]] = {
    4:    ("1.7.2-1.7.5", "1.7"),
    5:    ("1.7.6-1.7.10", "1.7"),
    47:   ("1.8.x", "1.8"),
    107:  ("1.9", "1.9"),
    108:  ("1.9.1", "1.9"),
    109:  ("1.9.2", "1.9"),
    110:  ("1.9.3-1.9.4", "1.9"),
    210:  ("1.10.x", "1.10"),
    315:  ("1.11.x", "1.11"),
    316:  ("1.11.1", "1.11"),
    335:  ("1.12", "1.12"),
    338:  ("1.12.1", "1.12"),
    340:  ("1.12.2", "1.12"),
    393:  ("1.13", "1.13"),
    394:  ("1.13.1", "1.13"),
    401:  ("1.13.2", "1.13"),
    477:  ("1.14", "1.14"),
    480:  ("1.14.1", "1.14"),
    485:  ("1.14.2", "1.14"),
    490:  ("1.14.3", "1.14"),
    498:  ("1.14.4", "1.14"),
    573:  ("1.15", "1.15"),
    575:  ("1.15.1", "1.15"),
    578:  ("1.15.2", "1.15"),
    735:  ("1.16", "1.16"),
    736:  ("1.16.1", "1.16"),
    751:  ("1.16.2", "1.16"),
    753:  ("1.16.3", "1.16"),
    754:  ("1.16.4-1.16.5", "1.16"),
    755:  ("1.17", "1.17"),
    756:  ("1.17.1", "1.17"),
    757:  ("1.18-1.18.1", "1.18"),
    758:  ("1.18.2", "1.18"),
    759:  ("1.19", "1.19"),
    760:  ("1.19.1-1.19.2", "1.19"),
    761:  ("1.19.3", "1.19"),
    762:  ("1.19.4", "1.19"),
    763:  ("1.20-1.20.1", "1.20"),
    764:  ("1.20.2", "1.20"),
    765:  ("1.20.3-1.20.4", "1.20"),
    766:  ("1.20.5-1.20.6", "1.20"),
    767:  ("1.21-1.21.1", "1.21"),
    768:  ("1.21.2-1.21.3", "1.21"),
    769:  ("1.21.4", "1.21"),
    770:  ("1.21.5", "1.21"),
    771:  ("1.21.6", "1.21"),
    # 注: 1.21.7 复用了 1.21-1.21.1 的协议号 767，已在上方 767 条目中覆盖
    772:  ("1.21.8", "1.21"),
    773:  ("1.21.9-1.21.10", "1.21"),
    774:  ("1.21.11", "1.21"),
}

# 已知代理/跨版本软件关键词 → 显示名
_PROXY_KEYWORDS = {
    "velocity": "Velocity",
    "bungeecord": "BungeeCord",
    "bungee": "BungeeCord",
    "waterfall": "Waterfall",
    "flamecord": "FlameCord",
    "viaversion": "ViaVersion",
    "viabackwards": "ViaBackwards",
    "viaforwarders": "ViaForwarders",
    "geyser": "Geyser",
    "wynnproxy": "WynnProxy",
}

# Minecraft 颜色码 → CSS 颜色值
MINECRAFT_COLOR_MAP = {
    '0': '#000000', '1': '#0000aa', '2': '#00aa00', '3': '#00aaaa',
    '4': '#aa0000', '5': '#aa00aa', '6': '#ffaa00', '7': '#aaaaaa',
    '8': '#555555', '9': '#5555ff', 'a': '#55ff55', 'b': '#55ffff',
    'c': '#ff5555', 'd': '#ff55ff', 'e': '#ffff55', 'f': '#ffffff',
}

# 版本号正则（匹配 1.x.y 或 1.x）
_VERSION_RE = re.compile(r'(\d+\.\d+(?:\.\d+)?)')

# 版本名 → 协议号 反向查找表（从 PROTOCOL_VERSION_MAP 构建）
_VERSION_TO_PROTOCOL: Dict[str, int] = {}


def _build_version_to_protocol():
    """从 PROTOCOL_VERSION_MAP 构建版本名到协议号的反向映射"""
    for proto, (ver_str, _major) in PROTOCOL_VERSION_MAP.items():
        parts = ver_str.split("-")
        start = parts[0]
        end = parts[-1] if len(parts) > 1 else start

        start_m = _VERSION_RE.search(start)
        end_m = _VERSION_RE.search(end)
        if not start_m or not end_m:
            continue

        start_parts = [int(x) for x in start_m.group(1).split(".")]
        end_parts = [int(x) for x in end_m.group(1).split(".")]

        # 补齐到三段
        while len(start_parts) < 3:
            start_parts.append(0)
        while len(end_parts) < 3:
            end_parts.append(0)

        if start_parts == end_parts:
            _VERSION_TO_PROTOCOL[".".join(str(x) for x in start_parts)] = proto
        else:
            # 范围映射：首尾版本都指向同一个协议号
            for ver in [
                ".".join(str(x) for x in start_parts),
                ".".join(str(x) for x in end_parts),
            ]:
                _VERSION_TO_PROTOCOL[ver] = proto

    # 特殊映射：某些版本复用了其他版本的协议号
    _VERSION_TO_PROTOCOL["1.21.7"] = 767  # 1.21.7 复用了 1.21-1.21.1 的协议号


_build_version_to_protocol()


def _lookup_protocol_from_name(version_name: str) -> Optional[int]:
    """从版本名中提取最高版本号，反查协议号。未找到返回 None。"""
    if not version_name:
        return None
    matches = _VERSION_RE.findall(version_name)
    if not matches:
        return None
    # 取最高版本号
    best = max(matches, key=lambda v: [int(x) for x in v.split(".") if x.isdigit()])
    # 先精确查找，再尝试补齐到三段查找（如 1.8 → 1.8.0）
    result = _VERSION_TO_PROTOCOL.get(best)
    if result is not None:
        return result
    parts = best.split(".")
    if len(parts) == 2:
        result = _VERSION_TO_PROTOCOL.get(f"{best}.0")
        if result is not None:
            return result
    return None


def _html_escape(text: str) -> str:
    """HTML 转义"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


async def query_java_server_api(host: str, port: int = JAVA_DEFAULT_PORT) -> Dict[str, Any]:
    """使用第三方 API 查询 Java 版服务器状态"""
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.mcstatus.io/v2/status/java/{host}:{port}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("online"):
                        version_data = data.get("version", {})
                        # Bug Fix: API 返回的是 name_raw/name_clean，不是 name
                        version_name_raw = version_data.get("name_raw") or version_data.get("name_clean") or version_data.get("name", "")
                        protocol_raw = version_data.get("protocol")

                        logger.info(f"[MOTD] API 返回原始数据: version.name_raw='{version_data.get('name_raw')}', "
                                    f"version.name_clean='{version_data.get('name_clean')}', "
                                    f"version.name='{version_data.get('name')}', "
                                    f"version.protocol={protocol_raw}")

                        # API 返回 protocol: null 时，尝试从版本名反查协议号
                        if protocol_raw is None:
                            logger.info(f"[MOTD] API 返回 protocol=null，尝试从版本名反查")
                            protocol_raw = _lookup_protocol_from_name(version_name_raw)
                            if protocol_raw is not None:
                                logger.info(f"[MOTD] 从版本名 '{version_name_raw}' 反查到协议 {protocol_raw}")
                            else:
                                # 反查失败，尝试直连查询补全协议号
                                logger.info(f"[MOTD] 版本名反查失败，尝试直连查询补全")
                                direct = await query_java_server_direct(host, port)
                                if "error" not in direct and "version" in direct:
                                    protocol_raw = direct["version"].get("protocol")
                                    if protocol_raw is not None:
                                        logger.info(f"[MOTD] 直连查询补全协议号: {protocol_raw}")
                                    else:
                                        logger.info(f"[MOTD] 直连查询也未返回协议号")

                        players_data = data.get("players", {})
                        sample = []
                        for player in players_data.get("list", []) or []:
                            if isinstance(player, dict):
                                name = player.get("name_clean") or player.get("name_raw") or player.get("name")
                                if name:
                                    entry = {"name": name}
                                    player_id = player.get("uuid") or player.get("id")
                                    if player_id:
                                        entry["id"] = str(player_id)
                                    sample.append(entry)

                        return {
                            "version": {"name": version_name_raw, "protocol": protocol_raw if protocol_raw is not None else 0},
                            "players": {
                                "online": players_data.get("online", 0),
                                "max": players_data.get("max", 0),
                                "sample": sample,
                            },
                            "description": data.get("motd", {}).get("clean", "无描述"),
                            "favicon": data.get("icon") or data.get("favicon", ""),
                        }
                    else:
                        return {"error": "服务器离线或无法访问"}
                else:
                    return {"error": f"API 请求失败: {resp.status}"}
    except asyncio.TimeoutError:
        return {"error": "API 请求超时"}
    except Exception as e:
        return {"error": f"API 查询失败: {str(e)}"}


async def query_java_server_direct(host: str, port: int = JAVA_DEFAULT_PORT, timeout: int = 5) -> Dict[str, Any]:
    """直接查询 Java 版服务器状态"""
    
    def _pack_varint(value: int) -> bytes:
        if value < 0:
            value &= 0xFFFFFFFF
        result = b""
        while True:
            byte = value & 0x7F
            value >>= 7
            if value:
                result += struct.pack("B", byte | 0x80)
            else:
                result += struct.pack("B", byte)
                break
        return result
    
    def _unpack_varint(sock: socket.socket) -> int:
        result = 0
        for i in range(5):
            byte = sock.recv(1)
            if not byte:
                raise ConnectionError("连接中断")
            byte = byte[0]
            result |= (byte & 0x7F) << (7 * i)
            if not (byte & 0x80):
                return result
        raise ValueError("变长整数过大")
    
    def _pack_data(data: bytes) -> bytes:
        return _pack_varint(len(data)) + data
    
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        start_time = time.perf_counter()
        sock.connect((host, port))
        
        # 发送握手包
        handshake_data = (
            _pack_varint(-1) +  # 协议版本 -1
            _pack_data(host.encode('utf-8')) +
            struct.pack('>H', port) +
            _pack_varint(1)  # 状态请求
        )
        packet = _pack_varint(0) + handshake_data
        sock.sendall(_pack_data(packet))
        
        # 发送状态请求
        sock.sendall(_pack_data(_pack_varint(0)))
        
        # 读取响应长度
        length = _unpack_varint(sock)
        packet_id = _unpack_varint(sock)
        
        # 读取 JSON 长度
        json_length = _unpack_varint(sock)
        
        # 读取 JSON 数据
        json_data = b""
        while len(json_data) < json_length:
            chunk = sock.recv(min(4096, json_length - len(json_data)))
            if not chunk:
                break
            json_data += chunk
        
        sock.close()
        
        response = json.loads(json_data.decode('utf-8'))
        response["latency_ms"] = int(round((time.perf_counter() - start_time) * 1000))
        return response
        
    except socket.timeout:
        return {"error": "连接超时，请检查服务器地址和端口是否正确"}
    except socket.gaierror:
        return {"error": "无法解析服务器地址，请检查主机名是否正确"}
    except ConnectionRefusedError:
        return {"error": "连接被拒绝，服务器可能未运行或端口不正确"}
    except Exception as e:
        return {"error": f"查询失败: {str(e)}"}


async def query_java_server(host: str, port: int = JAVA_DEFAULT_PORT, timeout: int = 5, use_api: bool = True) -> Dict[str, Any]:
    """查询 Java 版服务器状态，优先使用 API"""
    if use_api:
        # 先尝试 API
        result = await query_java_server_api(host, port)
        if "error" not in result:
            direct_result = await query_java_server_direct(host, port, timeout)
            if "error" not in direct_result:
                if direct_result.get("latency_ms") is not None:
                    result["latency_ms"] = direct_result.get("latency_ms")
                if direct_result.get("favicon") and not result.get("favicon"):
                    result["favicon"] = direct_result.get("favicon")

                direct_players = direct_result.get("players", {})
                result_players = result.setdefault("players", {})
                if direct_players.get("sample") and not result_players.get("sample"):
                    result_players["sample"] = direct_players.get("sample")
                elif direct_players.get("sample") and result_players.get("sample"):
                    direct_by_name = {
                        str(player.get("name", "")).lower(): player
                        for player in direct_players.get("sample", [])
                        if isinstance(player, dict) and player.get("name")
                    }
                    for player in result_players.get("sample", []):
                        if not isinstance(player, dict) or player.get("id") or player.get("uuid"):
                            continue
                        direct_player = direct_by_name.get(str(player.get("name", "")).lower())
                        if direct_player and (direct_player.get("id") or direct_player.get("uuid")):
                            player["id"] = str(direct_player.get("id") or direct_player.get("uuid"))

                direct_version = direct_result.get("version", {})
                if direct_version.get("protocol") and not result.get("version", {}).get("protocol"):
                    result.setdefault("version", {})["protocol"] = direct_version.get("protocol")
            else:
                logger.info(f"[MOTD] 直连补充延迟/图标失败: {direct_result.get('error')}")
            return result
        logger.info(f"[MOTD] API 查询失败，尝试直接查询: {result.get('error')}")

    # API 失败或禁用 API，使用直接查询
    result = await query_java_server_direct(host, port, timeout)

    # 直连返回协议号 ≤ 0（代理/多版本服务器），补查 API 获取带范围的版本名
    if "error" not in result:
        proto = result.get("version", {}).get("protocol", 0)
        version_name = result.get("version", {}).get("name", "")
        logger.info(f"[MOTD] 直连查询结果: protocol={proto}, name='{version_name}'")
        if proto is not None and proto <= 0:
            logger.info(f"[MOTD] 直连协议号 {proto} ≤ 0，补查 API 获取版本范围")
            api_result = await query_java_server_api(host, port)
            if "error" not in api_result:
                api_name = api_result.get("version", {}).get("name", "")
                if api_name and api_name != version_name:
                    result["version"]["name"] = api_name
                    logger.info(f"[MOTD] API 补全版本名: '{version_name}' -> '{api_name}'")
                else:
                    logger.info(f"[MOTD] API 版本名相同或为空，无需补全")

    return result


async def query_bedrock_server(host: str, port: int = BEDROCK_DEFAULT_PORT, timeout: int = 5) -> Dict[str, Any]:
    """异步查询基岩版服务器状态"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        
        # 发送 Unconnected Ping
        ping_data = b'\x01' + struct.pack('>Q', int(time.time() * 1000)) + b'\x00\x00\x00\x00\x00\x00\x00\x00'
        sock.sendto(ping_data, (host, port))
        
        # 接收响应
        data, addr = sock.recvfrom(2048)
        sock.close()
        
        # 解析响应
        if len(data) < 35:
            return {"error": "响应数据无效"}
        
        # 跳过头部
        server_info = data[35:].decode('utf-8', errors='ignore').split(';')
        
        if len(server_info) >= 6:
            return {
                "edition": server_info[0],
                "motd": server_info[1],
                "protocol": server_info[2],
                "version": server_info[3],
                "online_players": server_info[4],
                "max_players": server_info[5],
                "server_id": server_info[6] if len(server_info) > 6 else "未知"
            }
        else:
            return {"error": "无法解析服务器响应"}
            
    except socket.timeout:
        return {"error": "连接超时，请检查服务器地址和端口是否正确"}
    except Exception as e:
        return {"error": f"查询失败: {str(e)}"}


# ============================================================
# HTML 模板：MOTD 单服务器状态卡片
# 随机壁纸 + 深色玻璃信息层，保持 /motd <地址> 的单服查询结构
# ============================================================
MOTD_HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=1280, initial-scale=1">
<style>
:root {
    --canvas: #08111d;
    --surface: rgba(4, 9, 16, 0.22);
    --surface-strong: rgba(3, 8, 15, 0.30);
    --surface-soft: rgba(4, 10, 18, 0.24);
    --text: #f7fafc;
    --muted: #c8d2df;
    --subtle: #9eacbc;
    --border: rgba(255, 255, 255, 0.22);
    --border-soft: rgba(255, 255, 255, 0.15);
    --success: #42d392;
    --warning: #fbbf24;
    --danger: #ff6b6b;
    --info: #7dd3fc;
}
html,
body {
    margin: 0;
    padding: 0;
    width: 1280px;
    height: 720px;
    min-width: 1280px;
    min-height: 720px;
    max-width: 1280px;
    max-height: 720px;
}
body {
    position: relative;
    font-family: "HarmonyOS Sans SC", "Noto Sans CJK SC", "Microsoft YaHei", "PingFang SC", sans-serif;
    color: var(--text);
    background: var(--canvas);
    overflow: hidden;
    letter-spacing: 0;
}
.wallpaper {
    position: absolute;
    inset: 0;
    width: 1280px;
    height: 720px;
    background-position: center 45%;
    background-repeat: no-repeat;
    background-size: cover;
    filter: saturate(0.98) contrast(1.02);
}
.wallpaper-fallback {
    background:
        radial-gradient(circle at 18% 12%, rgba(58, 116, 150, 0.38), transparent 32%),
        linear-gradient(145deg, #17283b 0%, #13222e 44%, #09111b 100%);
}
.shade {
    position: absolute;
    inset: 0;
    width: 1280px;
    height: 720px;
    background:
        radial-gradient(ellipse at center, transparent 52%, rgba(1, 5, 10, 0.18) 100%),
        linear-gradient(180deg, rgba(3, 7, 13, 0.26) 0%, rgba(3, 7, 13, 0.34) 54%, rgba(2, 6, 11, 0.50) 100%);
}
.motd-card {
    position: relative;
    box-sizing: border-box;
    z-index: 1;
    width: 1280px;
    height: 720px;
    padding: 40px 48px;
}
.hero {
    height: 116px;
    display: flex;
    align-items: center;
    gap: 20px;
}
.server-icon {
    width: 76px;
    height: 76px;
    flex: 0 0 76px;
    border-radius: 8px;
    border: 1px solid rgba(255, 255, 255, 0.24);
    box-shadow: 0 12px 28px rgba(0, 0, 0, 0.30);
    object-fit: cover;
    image-rendering: pixelated;
    background: rgba(8, 16, 26, 0.88);
}
.server-icon-placeholder {
    display: grid;
    place-items: center;
    font-size: 20px;
    font-weight: 800;
    color: var(--text);
    background:
        linear-gradient(135deg, rgba(125, 211, 252, 0.35), rgba(66, 211, 146, 0.16)),
        rgba(8, 16, 26, 0.92);
}
.server-copy {
    min-width: 0;
    flex: 1 1 auto;
}
.eyebrow {
    margin-bottom: 7px;
    color: #dbe7f3;
    font-size: 14px;
    font-weight: 800;
    line-height: 1;
}
.server-main {
    max-width: 880px;
    font-size: 40px;
    line-height: 1.12;
    font-weight: 800;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    text-shadow: 0 2px 12px rgba(0, 0, 0, 0.48);
}
.server-sub {
    margin-top: 8px;
    font-size: 15px;
    font-weight: 600;
    color: var(--muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.meta-dot {
    display: inline-block;
    width: 6px;
    height: 6px;
    margin: 0 9px 2px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.42);
}
.status-card {
    box-sizing: border-box;
    width: 168px;
    height: 112px;
    flex: 0 0 168px;
    padding: 18px 20px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: rgba(5, 11, 19, 0.28);
    box-shadow: 0 16px 36px rgba(0, 0, 0, 0.26);
    backdrop-filter: none;
}
.status-label {
    color: var(--subtle);
    font-size: 14px;
    font-weight: 700;
}
.status-value {
    display: flex;
    align-items: center;
    gap: 9px;
    margin-top: 10px;
    font-size: 30px;
    line-height: 1;
    font-weight: 800;
    color: var(--success);
}
.status-value::before {
    content: "";
    width: 10px;
    height: 10px;
    border-radius: 999px;
    background: currentColor;
    box-shadow: 0 0 14px currentColor;
}
.status-value.offline {
    color: var(--danger);
}
.server-panel {
    box-sizing: border-box;
    height: 504px;
    margin-top: 20px;
    padding: 24px;
    display: grid;
    grid-template-rows: 96px 132px 1fr;
    gap: 14px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface);
    box-shadow: 0 18px 48px rgba(0, 0, 0, 0.22), inset 0 1px 0 rgba(255, 255, 255, 0.08);
    backdrop-filter: none;
}
.stats-grid {
    display: grid;
    grid-template-columns: 0.9fr 1.35fr 0.9fr;
    gap: 14px;
}
.stat-tile {
    box-sizing: border-box;
    height: 96px;
    padding: 16px 18px;
    border: 1px solid var(--border-soft);
    border-radius: 8px;
    background: var(--surface-soft);
}
.stat-label {
    font-size: 14px;
    line-height: 1;
    font-weight: 700;
    color: var(--subtle);
    text-shadow: 0 1px 8px rgba(0, 0, 0, 0.92);
}
.stat-value {
    margin-top: 12px;
    font-size: 27px;
    line-height: 1.05;
    font-weight: 800;
    color: var(--text);
    font-feature-settings: "tnum";
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    text-shadow: 0 2px 10px rgba(0, 0, 0, 0.82);
}
.stat-value.latency-good { color: var(--success); }
.stat-value.latency-warning { color: var(--warning); }
.stat-value.latency-danger { color: var(--danger); }
.info-block,
.players-block {
    box-sizing: border-box;
    padding: 18px 20px;
    border: 1px solid var(--border-soft);
    border-radius: 8px;
    background: var(--surface-strong);
    overflow: hidden;
}
.info-label {
    color: var(--muted);
    font-size: 15px;
    line-height: 1;
    font-weight: 800;
    text-shadow: 0 1px 8px rgba(0, 0, 0, 0.92);
}
.motd-content {
    margin-top: 13px;
    max-height: 69px;
    overflow: hidden;
    font-size: 22px;
    line-height: 1.55;
    font-weight: 650;
    color: var(--text);
    word-break: break-word;
    text-shadow: 0 2px 10px rgba(0, 0, 0, 0.46);
}
.motd-content span {
    text-shadow: 0 1px 8px rgba(0, 0, 0, 0.72);
}
.players-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
}
.players-count {
    color: var(--subtle);
    font-size: 14px;
    font-weight: 700;
    font-feature-settings: "tnum";
    text-shadow: 0 1px 8px rgba(0, 0, 0, 0.92);
}
.player-list {
    display: flex;
    align-content: flex-start;
    flex-wrap: wrap;
    gap: 10px;
    max-height: 98px;
    margin-top: 16px;
    overflow: hidden;
}
.player-chip {
    box-sizing: border-box;
    height: 44px;
    max-width: 190px;
    padding: 5px 12px 5px 5px;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    border: 1px solid rgba(255, 255, 255, 0.16);
    border-radius: 8px;
    background: rgba(4, 10, 18, 0.38);
    color: #edf4fb;
    font-size: 15px;
    line-height: 18px;
    font-weight: 700;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    text-shadow: 0 1px 7px rgba(0, 0, 0, 0.90);
}
.player-avatar,
.player-avatar-placeholder {
    width: 32px;
    height: 32px;
    flex: 0 0 32px;
    box-sizing: border-box;
    border: 1px solid rgba(255, 255, 255, 0.20);
    border-radius: 6px;
}
.player-avatar {
    object-fit: cover;
    image-rendering: pixelated;
}
.player-avatar-placeholder {
    display: grid;
    place-items: center;
    background:
        linear-gradient(135deg, rgba(66, 211, 146, 0.55), rgba(125, 211, 252, 0.32)),
        rgba(5, 12, 20, 0.82);
    color: #f7fafc;
    font-size: 10px;
    font-weight: 800;
}
.player-name {
    min-width: 0;
    max-width: 135px;
    overflow: hidden;
    text-overflow: ellipsis;
}
.player-more {
    min-width: 66px;
    justify-content: center;
    padding-left: 12px;
    color: var(--info);
}
.player-empty {
    box-sizing: border-box;
    height: 44px;
    padding: 12px 14px;
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 8px;
    background: rgba(4, 10, 18, 0.26);
    color: var(--muted);
    font-size: 15px;
    line-height: 18px;
    font-weight: 700;
    text-shadow: 0 1px 7px rgba(0, 0, 0, 0.90);
}
.panel-footer {
    position: absolute;
    right: 72px;
    bottom: 50px;
    color: rgba(225, 235, 244, 0.56);
    font-size: 12px;
    font-weight: 600;
}
.via-tag {
    margin-left: 8px;
    color: var(--info);
}
</style>
</head>
<body>

{% if wallpaper_src %}
<div class="wallpaper" style="background-image: url('{{ wallpaper_src }}')"></div>
{% else %}
<div class="wallpaper wallpaper-fallback"></div>
{% endif %}
<div class="shade"></div>
<div class="motd-card">
  <div class="hero">
    {% if server_icon %}
    <img class="server-icon" src="{{ server_icon }}" />
    {% else %}
    <div class="server-icon server-icon-placeholder">MC</div>
    {% endif %}
    <div class="server-copy">
      <div class="eyebrow">MINECRAFT SERVER</div>
      <div class="server-main">{{ server_display }}</div>
      <div class="server-sub">
        {{ edition_label }} Edition<span class="meta-dot"></span>单服务器实时查询<span class="meta-dot"></span>{{ queried_at }}
      </div>
    </div>
    <div class="status-card">
      <div class="status-label">服务器状态</div>
      <div class="status-value {{ status_class }}">{{ status_label }}</div>
    </div>
  </div>

  <div class="server-panel">
    <div class="stats-grid">
      <div class="stat-tile">
        <div class="stat-label">在线人数</div>
        <div class="stat-value">{{ online_display }}</div>
      </div>
      <div class="stat-tile">
        <div class="stat-label">游戏版本</div>
        <div class="stat-value">{{ server_version }}</div>
      </div>
      <div class="stat-tile">
        <div class="stat-label">响应延迟</div>
        <div class="stat-value {{ latency_class }}">{{ latency_text }}</div>
      </div>
    </div>

    <div class="info-block">
      <div class="info-label">{% if is_error %}错误信息{% else %}服务器描述{% endif %}</div>
      <div class="motd-content">{{ motd_html }}</div>
    </div>

    <div class="players-block">
      <div class="players-head">
        <div class="info-label">在线玩家{% if via_hint %}<span class="via-tag">{{ via_hint }}</span>{% endif %}</div>
        <div class="players-count">{{ online_display }}{% if client_version and client_version != server_version %} · 客户端 {{ client_version }}{% endif %}</div>
      </div>
      <div class="player-list">
        {% if player_list %}
          {% for player in player_list %}
          <div class="player-chip">
            {% if player.avatar_src %}<img class="player-avatar" src="{{ player.avatar_src }}" />{% else %}<span class="player-avatar-placeholder">MC</span>{% endif %}
            <span class="player-name">{{ player.name }}</span>
          </div>
          {% endfor %}
          {% if extra_count > 0 %}<div class="player-chip player-more">+{{ extra_count }}</div>{% endif %}
        {% else %}
          <div class="player-empty">{{ player_line }}</div>
        {% endif %}
      </div>
    </div>
  </div>
  <div class="panel-footer">WALLPAPER · t.alcy.cc</div>
</div>
</body>
</html>
'''


MOTD_STATUS_TEMPLATE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=1280, initial-scale=1.0" />
<style>
:root {
    --text: #f7fbff;
    --muted: #b8c7d8;
    --subtle: #92a6ba;
    --surface: rgba(7, 15, 25, 0.82);
    --surface-soft: rgba(12, 24, 38, 0.76);
    --border: rgba(255, 255, 255, 0.18);
    --border-soft: rgba(255, 255, 255, 0.11);
    --accent: #7dd3fc;
    --success: #55e6a5;
    --warning: #f8cf66;
    --danger: #ff7d87;
}
* { box-sizing: border-box; }
html, body {
    margin: 0;
    width: 1280px;
    height: 720px;
    min-width: 1280px;
    min-height: 720px;
    max-width: 1280px;
    max-height: 720px;
    overflow: hidden;
}
body {
    position: relative;
    color: var(--text);
    background: #0b1420;
    font-family: "HarmonyOS Sans SC", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
    letter-spacing: 0;
}
.wallpaper {
    position: absolute;
    inset: 0;
    width: 1280px;
    height: 720px;
    background-position: center 45%;
    background-repeat: no-repeat;
    background-size: cover;
    filter: saturate(.92) contrast(1.04);
}
.wallpaper-fallback {
    background: linear-gradient(145deg, #193247 0%, #102232 48%, #08111c 100%);
}
.shade {
    position: absolute;
    inset: 0;
    width: 1280px;
    height: 720px;
    background: linear-gradient(180deg, rgba(3, 8, 14, .18), rgba(3, 8, 14, .34));
}
.board {
    position: relative;
    z-index: 1;
    width: 1280px;
    height: 720px;
    padding: 22px 42px 28px;
}
.topbar {
    height: 64px;
    display: flex;
    align-items: center;
    justify-content: space-between;
}
.brand-kicker {
    color: var(--accent);
    font-size: 13px;
    line-height: 1;
    font-weight: 800;
}
.board-title {
    margin-top: 7px;
    font-size: 30px;
    line-height: 1;
    font-weight: 800;
    text-shadow: 0 2px 14px rgba(0,0,0,.48);
}
.query-meta {
    text-align: right;
    color: var(--muted);
    font-size: 14px;
    line-height: 1.45;
    font-weight: 650;
    text-shadow: 0 1px 9px rgba(0,0,0,.72);
}
.query-count {
    color: var(--text);
    font-size: 20px;
    font-weight: 800;
    font-feature-settings: "tnum";
}
.server-grid {
    width: 1120px;
    height: 598px;
    margin: 8px auto 0;
    display: grid;
    gap: 14px;
}
.server-grid.dual {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: repeat(2, minmax(0, 1fr));
}
.server-grid.single {
    width: 1040px;
    height: 300px;
    margin-top: 112px;
    grid-template-columns: minmax(0, 1fr);
}
.server-card {
    position: relative;
    min-width: 0;
    height: 292px;
    padding: 17px 20px;
    display: grid;
    grid-template-columns: minmax(0, .92fr) minmax(0, 1.08fr);
    grid-template-rows: 64px 76px minmax(0, 1fr);
    column-gap: 18px;
    row-gap: 11px;
    border: 1px solid var(--border);
    border-top: 3px solid var(--success);
    border-radius: 8px;
    background: rgba(7, 15, 25, .56);
    box-shadow: 0 12px 32px rgba(0,0,0,.22);
    overflow: hidden;
}
.single .server-card { height: 300px; }
.server-card.offline { border-top-color: var(--danger); }
.card-head {
    min-width: 0;
    min-height: 64px;
    display: flex;
    align-items: center;
    gap: 14px;
    grid-column: 1;
    grid-row: 1;
}
.server-icon,
.server-icon-placeholder {
    width: 54px;
    height: 54px;
    flex: 0 0 54px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: #101e2d;
}
.server-icon {
    object-fit: cover;
    image-rendering: pixelated;
}
.server-icon-placeholder {
    display: grid;
    place-items: center;
    color: var(--accent);
    font-size: 16px;
    font-weight: 900;
}
.server-copy { min-width: 0; flex: 1 1 auto; }
.server-index {
    color: var(--subtle);
    font-size: 12px;
    line-height: 1;
    font-weight: 800;
}
.server-address {
    margin-top: 6px;
    color: var(--text);
    font-size: 21px;
    line-height: 1.15;
    font-weight: 800;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.single .server-address { font-size: 24px; }
.edition {
    margin-top: 5px;
    color: var(--muted);
    font-size: 13px;
    font-weight: 650;
}
.status-pill {
    flex: 0 0 auto;
    padding: 7px 10px;
    display: flex;
    align-items: center;
    gap: 7px;
    border: 1px solid rgba(85, 230, 165, .32);
    border-radius: 999px;
    color: var(--success);
    background: rgba(85, 230, 165, .10);
    font-size: 13px;
    font-weight: 800;
}
.status-pill.offline {
    color: var(--danger);
    border-color: rgba(255, 125, 135, .34);
    background: rgba(255, 125, 135, .10);
}
.status-dot {
    width: 7px;
    height: 7px;
    border-radius: 999px;
    background: currentColor;
    box-shadow: 0 0 10px currentColor;
}
.metrics {
    margin-top: 0;
    display: grid;
    grid-template-columns: .85fr 1.25fr .85fr;
    gap: 9px;
    grid-column: 2;
    grid-row: 1 / span 2;
    align-self: center;
}
.metric {
    min-width: 0;
    height: 76px;
    padding: 12px 13px;
    border: 1px solid var(--border-soft);
    border-radius: 7px;
    background: var(--surface-soft);
}
.metric-label,
.section-label {
    color: var(--subtle);
    font-size: 12px;
    line-height: 1;
    font-weight: 800;
}
.metric-value {
    margin-top: 10px;
    color: var(--text);
    font-size: 22px;
    line-height: 1;
    font-weight: 850;
    font-feature-settings: "tnum";
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.metric-value.latency-good { color: var(--success); }
.metric-value.latency-warning { color: var(--warning); }
.metric-value.latency-danger { color: var(--danger); }
.content-section {
    margin-top: 0;
    padding: 13px 15px;
    border: 1px solid var(--border-soft);
    border-radius: 7px;
    background: rgba(6, 14, 23, .46);
    overflow: hidden;
}
.motd-section {
    min-height: 0;
    grid-column: 1;
    grid-row: 2 / span 2;
}
.motd-text {
    margin-top: 10px;
    max-height: 72px;
    color: var(--text);
    font-size: 17px;
    line-height: 1.48;
    font-weight: 650;
    word-break: break-word;
    overflow: hidden;
    text-shadow: 0 1px 8px rgba(0,0,0,.55);
}
.players-section {
    min-height: 0;
    grid-column: 2;
    grid-row: 3;
}
.players-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
}
.players-count {
    color: var(--muted);
    font-size: 12px;
    font-weight: 700;
}
.via-tag { margin-left: 6px; color: var(--accent); }
.player-list {
    margin-top: 10px;
    display: flex;
    flex-wrap: wrap;
    align-content: flex-start;
    gap: 8px;
    max-height: 48px;
    overflow: hidden;
}
.player-chip,
.player-empty {
    height: 39px;
    border: 1px solid var(--border-soft);
    border-radius: 7px;
    background: rgba(13, 27, 42, .72);
    color: #edf5fc;
    font-size: 13px;
    font-weight: 700;
}
.player-chip {
    max-width: 164px;
    padding: 4px 10px 4px 4px;
    display: inline-flex;
    align-items: center;
    gap: 7px;
}
.player-avatar,
.player-avatar-placeholder {
    width: 29px;
    height: 29px;
    flex: 0 0 29px;
    border-radius: 5px;
}
.player-avatar { object-fit: cover; image-rendering: pixelated; }
.player-avatar-placeholder {
    display: grid;
    place-items: center;
    color: var(--accent);
    background: #173047;
    font-size: 9px;
    font-weight: 900;
}
.player-name {
    min-width: 0;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
}
.player-more { min-width: 52px; justify-content: center; padding-left: 10px; color: var(--accent); }
.player-empty { padding: 11px 13px; color: var(--muted); }
.footer {
    position: absolute;
    right: 45px;
    bottom: 12px;
    color: rgba(220, 232, 243, .54);
    font-size: 11px;
    font-weight: 650;
}
</style>
</head>
<body>
{% if wallpaper_src %}
<div class="wallpaper" style="background-image: url('{{ wallpaper_src }}')"></div>
{% else %}
<div class="wallpaper wallpaper-fallback"></div>
{% endif %}
<div class="shade"></div>
<main class="board">
  <header class="topbar">
    <div>
      <div class="brand-kicker">MINECRAFT STATUS</div>
      <div class="board-title">服务器状态</div>
    </div>
    <div class="query-meta">
      <div class="query-count">{{ server_count }} 台服务器</div>
      <div>{{ query_mode }} · {{ queried_at }}</div>
    </div>
  </header>
  <div class="server-grid {{ layout_class }}">
    {% for server in servers %}
    <section class="server-card {{ server.status_class }}">
      <div class="card-head">
        {% if server.server_icon %}
        <img class="server-icon" src="{{ server.server_icon }}" />
        {% else %}
        <div class="server-icon-placeholder">MC</div>
        {% endif %}
        <div class="server-copy">
          <div class="server-index">SERVER {{ server.index }}</div>
          <div class="server-address">{{ server.server_display }}</div>
          <div class="edition">{{ server.edition_label }} Edition{% if server.via_hint %}<span class="via-tag">{{ server.via_hint }}</span>{% endif %}</div>
        </div>
        <div class="status-pill {{ server.status_class }}"><span class="status-dot"></span>{{ server.status_label }}</div>
      </div>
      <div class="metrics">
        <div class="metric"><div class="metric-label">在线人数</div><div class="metric-value">{{ server.online_display }}</div></div>
        <div class="metric"><div class="metric-label">游戏版本</div><div class="metric-value">{{ server.server_version }}</div></div>
        <div class="metric"><div class="metric-label">响应延迟</div><div class="metric-value {{ server.latency_class }}">{{ server.latency_text }}</div></div>
      </div>
      <div class="content-section motd-section">
        <div class="section-label">{% if server.is_error %}错误信息{% else %}服务器描述{% endif %}</div>
        <div class="motd-text">{{ server.motd_html }}</div>
      </div>
      <div class="content-section players-section">
        <div class="players-head">
          <div class="section-label">在线玩家</div>
          <div class="players-count">{{ server.online_display }}{% if server.client_version and server.client_version != server.server_version %} · 客户端 {{ server.client_version }}{% endif %}</div>
        </div>
        <div class="player-list">
          {% if server.player_list %}
            {% for player in server.player_list %}
            <div class="player-chip">
              {% if player.avatar_src %}<img class="player-avatar" src="{{ player.avatar_src }}" />{% else %}<span class="player-avatar-placeholder">MC</span>{% endif %}
              <span class="player-name">{{ player.name }}</span>
            </div>
            {% endfor %}
            {% if server.extra_count > 0 %}<div class="player-chip player-more">+{{ server.extra_count }}</div>{% endif %}
          {% else %}
            <div class="player-empty">{{ server.player_line }}</div>
          {% endif %}
        </div>
      </div>
    </section>
    {% endfor %}
  </div>
  <div class="footer">LIVE QUERY · WALLPAPER CACHE</div>
</main>
</body>
</html>
'''


MOTD_DUAL_TEMPLATE = '''<<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<style>
:root {
    --canvas: #08111d;
    --surface: rgba(4, 9, 16, .28);
    --surface-strong: rgba(3, 8, 15, .36);
    --surface-soft: rgba(4, 10, 18, .30);
    --text: #f7fafc;
    --muted: #c8d2df;
    --subtle: #9eacbc;
    --border: rgba(255, 255, 255, .22);
    --border-soft: rgba(255, 255, 255, .15);
    --success: #42d392;
    --warning: #fbbf24;
    --danger: #ff6b6b;
    --info: #7dd3fc;
}
* { box-sizing: border-box; }
html, body {
    margin: 0;
    width: 1280px;
    min-width: 1280px;
    max-width: 1280px;
    overflow: hidden;
}
body {
    position: relative;
    color: var(--text);
    background: var(--canvas);
    font-family: "HarmonyOS Sans SC", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
    letter-spacing: 0;
}
.wallpaper {
    position: absolute;
    inset: 0;
    width: 1280px;
    background-position: center 45%;
    background-repeat: no-repeat;
    background-size: cover;
    filter: saturate(.98) contrast(1.02);
}
.wallpaper-fallback {
    background:
        radial-gradient(circle at 18% 12%, rgba(58, 116, 150, .38), transparent 32%),
        linear-gradient(145deg, #17283b 0%, #13222e 44%, #09111b 100%);
}
.shade {
    position: absolute;
    inset: 0;
    width: 1280px;
    background:
        radial-gradient(ellipse at center, transparent 52%, rgba(1, 5, 10, .18) 100%),
        linear-gradient(180deg, rgba(3, 7, 13, .24) 0%, rgba(3, 7, 13, .30) 54%, rgba(2, 6, 11, .44) 100%);
}
.dashboard {
    position: relative;
    z-index: 1;
    width: 1280px;
}

{% if server_count >= 3 %}
html, body, .wallpaper, .shade, .dashboard {
    height: 960px;
    min-height: 960px;
    max-height: 960px;
}
.dashboard {
    padding: 26px 48px 24px;
}
.header {
    height: 96px;
    display: flex;
    align-items: center;
    justify-content: space-between;
}
.title {
    margin-top: 8px;
    color: var(--text);
    font-size: 38px;
    line-height: 1;
    font-weight: 850;
    text-shadow: 0 2px 15px rgba(0,0,0,.46);
}
.fleet-card {
    width: 218px;
    height: 88px;
    padding: 13px 18px;
    display: grid;
    grid-template-columns: 1fr auto;
    align-items: center;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface-strong);
    box-shadow: 0 14px 32px rgba(0,0,0,.22);
}
.fleet-value {
    margin-top: 6px;
    color: var(--success);
    font-size: 28px;
    line-height: 1;
    font-weight: 850;
    font-feature-settings: "tnum";
}
.server-list {
    margin-top: 14px;
    display: grid;
    grid-template-rows: repeat(3, 236px);
    gap: 12px;
}
.server-row {
    min-width: 0;
    height: 236px;
    padding: 15px 20px;
    border: 1px solid var(--border);
    border-left: 4px solid var(--success);
    border-radius: 8px;
    background: var(--surface);
    box-shadow: 0 12px 30px rgba(0,0,0,.20);
    overflow: hidden;
}
.row-head {
    height: 58px;
    display: flex;
    align-items: center;
    gap: 14px;
}
.server-icon,
.server-icon-placeholder {
    width: 56px;
    height: 56px;
    flex: 0 0 56px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: rgba(7, 18, 29, .72);
}
.server-address {
    margin-top: 4px;
    max-width: 760px;
    color: var(--text);
    font-size: 25px;
    line-height: 1.12;
    font-weight: 850;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    text-shadow: 0 2px 10px rgba(0,0,0,.48);
}
.online-badge {
    width: 114px;
    height: 56px;
    flex: 0 0 114px;
    padding: 8px 12px;
    border: 1px solid rgba(85, 230, 165, .30);
    border-radius: 7px;
    background: rgba(5, 17, 25, .48);
    text-align: right;
}
.badge-value {
    margin-top: 6px;
    color: var(--success);
    font-size: 20px;
    line-height: 1;
    font-weight: 850;
    font-feature-settings: "tnum";
}
.row-body {
    height: 130px;
    margin-top: 9px;
    display: grid;
    grid-template-columns: 470px minmax(0, 1fr);
    gap: 14px;
}
{% else %}
html, body, .wallpaper, .shade, .dashboard {
    height: 720px;
    min-height: 720px;
    max-height: 720px;
}
.dashboard {
    padding: 34px 48px 30px;
}
.header {
    height: 108px;
    display: flex;
    align-items: center;
    justify-content: space-between;
}
.title {
    margin-top: 10px;
    color: var(--text);
    font-size: 42px;
    line-height: 1;
    font-weight: 850;
    text-shadow: 0 2px 15px rgba(0,0,0,.46);
}
.fleet-card {
    width: 218px;
    height: 96px;
    padding: 15px 18px;
    display: grid;
    grid-template-columns: 1fr auto;
    align-items: center;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface-strong);
    box-shadow: 0 14px 32px rgba(0,0,0,.22);
}
.fleet-value {
    margin-top: 8px;
    color: var(--success);
    font-size: 31px;
    line-height: 1;
    font-weight: 850;
    font-feature-settings: "tnum";
}
.server-list {
    margin-top: 14px;
    display: grid;
    grid-template-rows: repeat(2, 256px);
    gap: 14px;
}
.server-row {
    min-width: 0;
    height: 256px;
    padding: 16px 22px;
    border: 1px solid var(--border);
    border-left: 4px solid var(--success);
    border-radius: 10px;
    background: var(--surface);
    box-shadow: 0 12px 30px rgba(0,0,0,.20);
    overflow: hidden;
}
.row-head {
    height: 64px;
    display: flex;
    align-items: center;
    gap: 14px;
}
.server-icon,
.server-icon-placeholder {
    width: 58px;
    height: 58px;
    flex: 0 0 58px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: rgba(7, 18, 29, .72);
}
.server-address {
    margin-top: 5px;
    max-width: 760px;
    color: var(--text);
    font-size: 27px;
    line-height: 1.12;
    font-weight: 850;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    text-shadow: 0 2px 10px rgba(0,0,0,.48);
}
.online-badge {
    width: 116px;
    height: 58px;
    flex: 0 0 116px;
    padding: 9px 12px;
    border: 1px solid rgba(85, 230, 165, .30);
    border-radius: 7px;
    background: rgba(5, 17, 25, .48);
    text-align: right;
}
.badge-value {
    margin-top: 8px;
    color: var(--success);
    font-size: 21px;
    line-height: 1;
    font-weight: 850;
    font-feature-settings: "tnum";
}
.row-body {
    height: 154px;
    margin-top: 10px;
    display: grid;
    grid-template-columns: 375px minmax(0, 1fr);
    gap: 16px;
}
{% endif %}

.eyebrow {
    color: var(--muted);
    font-size: 14px;
    line-height: 1;
    font-weight: 800;
    text-shadow: 0 1px 8px rgba(0,0,0,.7);
}
.header-meta {
    margin-top: 11px;
    color: var(--muted);
    font-size: 14px;
    font-weight: 650;
    text-shadow: 0 1px 8px rgba(0,0,0,.7);
}
.fleet-label {
    color: var(--muted);
    font-size: 13px;
    font-weight: 750;
}
.fleet-players {
    color: var(--text);
    text-align: right;
    font-size: 18px;
    line-height: 1.25;
    font-weight: 800;
    font-feature-settings: "tnum";
}
.fleet-players small {
    display: block;
    color: var(--subtle);
    font-size: 11px;
    font-weight: 700;
}
.server-row.offline { border-left-color: var(--danger); }
.server-icon { object-fit: cover; image-rendering: pixelated; }
.server-icon-placeholder {
    display: grid;
    place-items: center;
    color: var(--info);
    font-size: 15px;
    font-weight: 900;
}
.server-copy { min-width: 0; flex: 1 1 auto; }
.server-index {
    color: var(--subtle);
    font-size: 11px;
    line-height: 1;
    font-weight: 800;
}
.server-edition {
    margin-top: 4px;
    color: var(--muted);
    font-size: 12px;
    font-weight: 700;
}
.via-tag { margin-left: 7px; color: var(--info); }
.online-badge.offline { border-color: rgba(255, 107, 107, .32); }
.badge-label {
    color: var(--subtle);
    font-size: 11px;
    line-height: 1;
    font-weight: 750;
}
.online-badge.offline .badge-value { color: var(--danger); }
.server-facts {
    min-width: 0;
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    grid-template-rows: 62px 82px;
    gap: 10px;
}
.fact,
.description-line,
.players-panel {
    border: 1px solid var(--border-soft);
    border-radius: 7px;
    background: var(--surface-soft);
}
.fact { padding: 11px 13px; }
.fact-label,
.players-label {
    color: var(--subtle);
    font-size: 11px;
    line-height: 1;
    font-weight: 800;
}
.fact-value {
    margin-top: 10px;
    color: var(--text);
    font-size: 21px;
    line-height: 1;
    font-weight: 850;
    font-feature-settings: "tnum";
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.fact-value.latency-good { color: var(--success); }
.fact-value.latency-warning { color: var(--warning); }
.fact-value.latency-danger { color: var(--danger); }
.description-line {
    min-width: 0;
    grid-column: 1 / -1;
    padding: 9px 12px;
    display: flex;
    align-items: center;
    gap: 10px;
}
.description-label {
    flex: 0 0 auto;
    color: var(--subtle);
    font-size: 11px;
    font-weight: 800;
}
.description-text {
    min-width: 0;
    color: var(--text);
    font-size: 15px;
    line-height: 1.2;
    font-weight: 700;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    text-shadow: 0 1px 7px rgba(0,0,0,.55);
}
.players-panel {
    min-width: 0;
    padding: 12px 14px;
    overflow: hidden;
}
.players-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
}
.players-count {
    color: var(--muted);
    font-size: 11px;
    font-weight: 750;
}
.player-list {
    margin-top: 8px;
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 6px;
    max-height: 118px;
    overflow: hidden;
}
.player-chip {
    padding: 3px 6px 3px 4px;
    gap: 6px;
    font-size: 13px;
}
.player-avatar, .player-avatar-placeholder {
    width: 20px;
    height: 20px;
    flex: 0 0 20px;
}
.player-name {
    font-size: 13px;
}
.player-chip,
.player-empty {
    min-width: 0;
    height: 38px;
    border: 1px solid var(--border-soft);
    border-radius: 6px;
    background: rgba(6, 15, 24, .46);
    color: #edf5fc;
    font-size: 12px;
    font-weight: 750;
}
.player-chip {
    padding: 4px 8px 4px 4px;
    display: flex;
    align-items: center;
    gap: 7px;
}
.player-avatar,
.player-avatar-placeholder {
    width: 28px;
    height: 28px;
    flex: 0 0 28px;
    border-radius: 5px;
}
.player-avatar { object-fit: cover; image-rendering: pixelated; }
.player-avatar-placeholder {
    display: grid;
    place-items: center;
    color: var(--info);
    background: rgba(22, 51, 75, .78);
    font-size: 8px;
    font-weight: 900;
}
.player-name {
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.player-more { justify-content: center; color: var(--info); padding-left: 8px; }
.player-empty {
    grid-column: 1 / -1;
    padding: 11px 12px;
    color: var(--muted);
}
.footer {
    position: absolute;
    right: 50px;
    bottom: 12px;
    color: rgba(225, 235, 244, .60);
    font-size: 11px;
    font-weight: 650;
}
</style>
</head>
<body>
{% if wallpaper_src %}
<div class="wallpaper" style="background-image: url('{{ wallpaper_src }}')"></div>
{% else %}
<div class="wallpaper wallpaper-fallback"></div>
{% endif %}
<div class="shade"></div>
<main class="dashboard">
  <header class="header">
    <div>
      <div class="eyebrow">MINECRAFT SERVER</div>
      <div class="title">服务器在线列表</div>
      <div class="header-meta">{{ server_count }} 台默认服务器 · {{ queried_at }} 更新</div>
    </div>
    <div class="fleet-card">
      <div>
        <div class="fleet-label">在线服务器</div>
        <div class="fleet-value">{{ online_server_count }}/{{ server_count }}</div>
      </div>
      <div class="fleet-players"><small>在线玩家</small>{{ total_online }}</div>
    </div>
  </header>
  <div class="server-list">
    {% for server in servers %}
    <section class="server-row {{ server.status_class }}">
      <div class="row-head">
        {% if server.server_icon %}
        <img class="server-icon" src="{{ server.server_icon }}" />
        {% else %}
        <div class="server-icon-placeholder">MC</div>
        {% endif %}
        <div class="server-copy">
          <div class="server-index">SERVER {{ server.index }}</div>
          <div class="server-address">{{ server.server_display }}</div>
          <div class="server-edition">{{ server.edition_label }} Edition{% if server.via_hint %}<span class="via-tag">{{ server.via_hint }}</span>{% endif %}</div>
        </div>
        <div class="online-badge {{ server.status_class }}">
          <div class="badge-label">{% if server.is_error %}服务器状态{% else %}在线人数{% endif %}</div>
          <div class="badge-value">{% if server.is_error %}离线{% else %}{{ server.online_display }}{% endif %}</div>
        </div>
      </div>
      <div class="row-body">
        <div class="server-facts">
          <div class="fact"><div class="fact-label">游戏版本</div><div class="fact-value">{{ server.server_version }}</div></div>
          <div class="fact"><div class="fact-label">响应延迟</div><div class="fact-value {{ server.latency_class }}">{{ server.latency_text }}</div></div>
          <div class="description-line">
            <span class="description-label">{% if server.is_error %}错误{% else %}描述{% endif %}</span>
            <span class="description-text">{{ server.motd_html }}</span>
          </div>
        </div>
        <div class="players-panel">
          <div class="players-head"><div class="players-label">在线玩家</div><div class="players-count">{{ server.online_display }}</div></div>
          <div class="player-list">
            {% if server.player_list %}
              {% for player in server.player_list %}
              <div class="player-chip">
                {% if player.avatar_src %}<img class="player-avatar" src="{{ player.avatar_src }}" />{% else %}<span class="player-avatar-placeholder">MC</span>{% endif %}
                <span class="player-name">{{ player.name }}</span>
              </div>
              {% endfor %}
              {% if server.extra_count > 0 %}
              <div class="player-chip player-more">+{{ server.extra_count }} 更多</div>
              {% endif %}
            {% else %}
              <div class="player-empty">{{ server.player_line }}</div>
            {% endif %}
          </div>
        </div>
      </div>
    </section>
    {% endfor %}
  </div>
  <div class="footer">MC-MOTD-V1.8.0</div>
</main>
</body>
</html>
'''


@register(PLUGIN_NAME, "MOTD查询", "查询 Minecraft 服务器状态的 AstrBot 插件，支持 ViaVersion/Velocity/BungeeCord 多版本兼容", PLUGIN_VERSION)
class MOTDPlugin(Star):
    """MOTD 查询插件主类"""
    
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._last_wallpaper_src = ""
        self._last_wallpaper_path = ""
        self._avatar_cache: Dict[str, str] = {}
        self._wallpaper_prefetch_task: Optional[asyncio.Task] = None
        self._wallpaper_refill_lock = asyncio.Lock()
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.wallpaper_cache_root = self.data_dir / "wallpaper_cache"
        self.wallpaper_cache_root.mkdir(parents=True, exist_ok=True)
        self._load_config()
        logger.info(f"[MOTD] 插件初始化完成，版本 {PLUGIN_VERSION}")
    
    def _load_config(self):
        """加载插件配置"""
        self.default_server = self.config.get("default_server", "")
        self.default_port = self.config.get("default_port", JAVA_DEFAULT_PORT)
        raw_default_servers = self.config.get("default_servers", [])
        if isinstance(raw_default_servers, str):
            raw_default_servers = [raw_default_servers]
        self.default_servers = []
        if isinstance(raw_default_servers, list):
            for raw_address in raw_default_servers:
                address = str(raw_address or "").strip()
                if not address:
                    continue
                host, port = self._parse_server_address(address, is_java=True)
                if host and 1 <= port <= 65535:
                    normalized = self._join_server_address(host, port)
                    if normalized not in self.default_servers:
                        self.default_servers.append(normalized)
                if len(self.default_servers) >= 3:
                    break
        if not self.default_servers and self.default_server:
            self.default_servers = [
                self._join_server_address(str(self.default_server).strip(), int(self.default_port))
            ]
        if self.default_servers:
            self.default_server, self.default_port = self._parse_server_address(
                self.default_servers[0], is_java=True
            )
        self.enabled_sessions = self.config.get("enabled_sessions", [])
        self.enable_all_sessions = self.config.get("enable_all_sessions", True)
        self.admin_only_config = self.config.get("admin_only_config", True)
        self.query_timeout = self.config.get("query_timeout", 5)
        self.use_api = self.config.get("use_api", True)
        self.enable_random_wallpaper = self.config.get("enable_random_wallpaper", True)
        self.enable_player_avatars = self.config.get("enable_player_avatars", True)
        category = str(self.config.get("wallpaper_category", "moe") or "moe").strip().lower()
        self.wallpaper_category = category if category in {"moe", "pc", "fj", "bd", "ys"} else "moe"
        try:
            timeout = int(self.config.get("wallpaper_timeout", 12))
        except (TypeError, ValueError):
            timeout = 12
        self.wallpaper_timeout = max(5, min(timeout, 20))
        try:
            cache_target = int(self.config.get("wallpaper_cache_count", 100))
        except (TypeError, ValueError):
            cache_target = 100
        self.wallpaper_cache_target = max(10, min(cache_target, 200))
        try:
            concurrency = int(self.config.get("wallpaper_prefetch_concurrency", 3))
        except (TypeError, ValueError):
            concurrency = 3
        self.wallpaper_prefetch_concurrency = max(1, min(concurrency, 6))
        try:
            render_timeout = int(self.config.get("render_timeout", 20))
        except (TypeError, ValueError):
            render_timeout = 20
        self.render_timeout = max(5, min(render_timeout, 60))
        self.wallpaper_cache_dir = self.wallpaper_cache_root / self.wallpaper_category
        self.wallpaper_cache_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"[MOTD] 配置加载: default_servers={len(self.default_servers)}")
        logger.info(f"[MOTD] 使用 API 查询: {self.use_api}")
        logger.info(f"[MOTD] 在线玩家头像: {self.enable_player_avatars}")
        logger.info(
            f"[MOTD] 随机壁纸: {self.enable_random_wallpaper}, "
            f"category={self.wallpaper_category}, cache={self.wallpaper_cache_target}, "
            f"prefetch_concurrency={self.wallpaper_prefetch_concurrency}, "
            f"download_timeout={self.wallpaper_timeout}s"
        )
        logger.info(f"[MOTD] HTML 渲染硬超时: {self.render_timeout}s")
    
    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """检查用户是否为管理员"""
        try:
            astrbot_config = self.context.get_config()
            if astrbot_config:
                admins = astrbot_config.get("admins_id", [])
                sender_id = event.get_sender_id()
                return sender_id in admins
        except Exception as e:
            logger.error(f"[MOTD] 管理员检查失败: {e}")
        return False
    
    def _check_session_allowed(self, event: AstrMessageEvent) -> bool:
        """检查会话是否允许使用插件"""
        if self.enable_all_sessions:
            return True
        session_id = event.unified_msg_origin
        return session_id in self.enabled_sessions
    
    def _parse_server_address(self, address: str, is_java: bool = True) -> tuple:
        """
        解析服务器地址和端口
        如果用户指定了地址但不带端口，使用标准端口（Java 25565 / 基岩 19132）
        """
        if ':' in address:
            host, port_str = address.rsplit(':', 1)
            try:
                port = int(port_str)
            except ValueError:
                port = JAVA_DEFAULT_PORT if is_java else BEDROCK_DEFAULT_PORT
            host = host.strip('[]')
        else:
            host = address
            port = JAVA_DEFAULT_PORT if is_java else BEDROCK_DEFAULT_PORT
        return host, port

    def _join_server_address(self, host: str, port: int) -> str:
        """规范化服务器地址，并为 IPv6 地址补上方括号。"""
        clean_host = str(host or "").strip().strip("[]")
        display_host = f"[{clean_host}]" if ":" in clean_host else clean_host
        return f"{display_host}:{int(port)}"
    
    def _format_motd(self, motd_data: Any) -> str:
        """格式化 MOTD 文本"""
        if isinstance(motd_data, str):
            return motd_data
        elif isinstance(motd_data, dict):
            text = motd_data.get("text", "")
            extra = motd_data.get("extra", [])
            for item in extra:
                if isinstance(item, dict):
                    text += item.get("text", "")
                elif isinstance(item, str):
                    text += item
            return text
        elif isinstance(motd_data, list):
            text = ""
            for item in motd_data:
                if isinstance(item, dict):
                    text += item.get("text", "")
                elif isinstance(item, str):
                    text += item
            return text
        return str(motd_data)
    
    def _parse_version(self, version_info: Dict[str, Any]) -> Tuple[str, str, str]:
        """
        解析版本信息，支持 ViaVersion/Velocity/BungeeCord 等多版本兼容模式
        返回: (服务器版本, 支持的客户端版本, 代理/多版本提示)
        """
        version_name = (version_info.get("name") or "").strip()

        # 确保 protocol 是整数
        protocol_raw = version_info.get("protocol", 0)
        try:
            protocol = int(protocol_raw) if protocol_raw is not None else 0
        except (ValueError, TypeError):
            protocol = 0

        logger.info(f"[MOTD] 版本解析输入: name='{version_name}', protocol_raw={protocol_raw}, protocol={protocol}")

        # ── 1. 从版本名提取版本号 ──
        version_in_name = None
        if version_name:
            m = _VERSION_RE.search(version_name)
            if m:
                version_in_name = m.group(1)
                logger.info(f"[MOTD] 从版本名提取版本号: '{version_in_name}'")

        # ── 1.5 协议号无效时，从版本名反查 ──
        if protocol <= 0 and version_name:
            looked_up = _lookup_protocol_from_name(version_name)
            if looked_up is not None:
                logger.info(f"[MOTD] 协议号无效({protocol})，从版本名 '{version_name}' 反查到协议 {looked_up}")
                protocol = looked_up
            else:
                logger.info(f"[MOTD] 协议号无效({protocol})，从版本名 '{version_name}' 反查失败")

        # ── 2. 用协议号查服务器实际版本 ──
        proto_ver_display, proto_major = PROTOCOL_VERSION_MAP.get(protocol, ("", ""))
        if protocol > 0:
            logger.info(f"[MOTD] 协议号映射: {protocol} -> display='{proto_ver_display}', major='{proto_major}'")

        # ── 3. 代理/多版本检测 ──
        proxy_name = ""
        is_multi_version = False
        detect_reason = ""

        name_lower = version_name.lower()

        # 3a. 版本名包含已知代理软件名
        for kw, display_name in _PROXY_KEYWORDS.items():
            if kw in name_lower:
                proxy_name = display_name
                is_multi_version = True
                detect_reason = f"关键词匹配: '{kw}'"
                logger.info(f"[MOTD] 代理检测命中: '{kw}' -> {display_name}")
                break

        # 3b. 版本名包含范围格式（如 "1.7.2-1.21.11"、"1.8 - 26.1"、"1.8 / 1.21"）
        if not is_multi_version and version_name:
            range_match = re.search(r'(\d+\.\d+[\w.]*)\s*[-~–/]\s*(\d+\.\d+)', version_name)
            if range_match:
                is_multi_version = True
                detect_reason = f"范围格式: '{range_match.group(0)}'"
                logger.info(f"[MOTD] 多版本检测命中范围格式: '{range_match.group(0)}'")

        # 3c. 版本名列出多个版本（如 "1.7.x, 1.8.x, ..., 1.21.x"）
        if not is_multi_version and version_name:
            version_matches = _VERSION_RE.findall(version_name)
            if len(version_matches) >= 4:
                is_multi_version = True
                detect_reason = f"多版本列举: {len(version_matches)}个版本"
                logger.info(f"[MOTD] 多版本检测命中列举: {version_matches}")

        # 3d. 协议号 47 + 版本名提及高版本 → BungeeCord/Velocity
        if not is_multi_version and protocol == 47 and version_in_name:
            try:
                ver_parts = [int(x) for x in version_in_name.split('.')]
                if len(ver_parts) >= 2 and (ver_parts[0] > 1 or (ver_parts[0] == 1 and ver_parts[1] > 8)):
                    is_multi_version = True
                    detect_reason = f"协议47+高版本名: '{version_in_name}'"
                    logger.info(f"[MOTD] 多版本检测命中协议47+高版本: proto=47, version='{version_in_name}'")
            except (ValueError, IndexError):
                pass

        if not is_multi_version:
            logger.info(f"[MOTD] 未检测到多版本/代理")

        # ── 4. 从版本名解析支持范围 ──
        min_supported_version = ""
        max_supported_version = ""
        if is_multi_version and version_name:
            all_versions = _VERSION_RE.findall(version_name)
            # 过滤掉明显不是 Minecraft 版本的数字（如代理版本号 3.4.0）
            mc_versions = []
            for v in all_versions:
                parts = v.split('.')
                if len(parts) >= 2 and parts[0] == '1' and parts[1].isdigit():
                    mc_versions.append(v)
                elif len(parts) >= 2:
                    # Minecraft 新版本命名（26.x 起改用年份.版本格式）
                    try:
                        major = int(parts[0])
                        if major >= 26:
                            mc_versions.append(v)
                    except ValueError:
                        pass
            logger.info(f"[MOTD] 版本范围解析: all_versions={all_versions}, mc_versions={mc_versions}")
            if mc_versions:
                max_supported_version = max(mc_versions, key=lambda v: [int(x) for x in v.split('.') if x.isdigit()])
                min_supported_version = min(mc_versions, key=lambda v: [int(x) for x in v.split('.') if x.isdigit()])

        # ── 5. 构建显示结果 ──
        if is_multi_version:
            # 多版本兼容服务器：服务器版本用协议号映射，客户端版本显示支持范围
            server_version = proto_ver_display or version_in_name or "未知"
            if min_supported_version and max_supported_version and min_supported_version != max_supported_version:
                client_version = f"{min_supported_version} ~ {max_supported_version}"
            elif max_supported_version:
                client_version = f"≤ {max_supported_version}"
            else:
                client_version = version_name if version_name else "未知"
        elif version_name and version_name not in ("", "未知", "Unknown"):
            # 普通服务器，有版本名
            server_version = version_name
            client_version = version_name
        elif protocol > 0:
            # 用协议号推断
            server_version = proto_ver_display or "未知"
            client_version = server_version
        else:
            server_version = "未知"
            client_version = "未知"

        # ── 6. 代理/多版本提示 ──
        if is_multi_version:
            if proxy_name:
                via_hint = f"检测到: {proxy_name} 代理"
            else:
                via_hint = "支持多版本客户端连接"
        else:
            via_hint = ""

        logger.info(f"[MOTD] 版本解析输出: server='{server_version}', client='{client_version}', "
                     f"via_hint='{via_hint}', is_multi_version={is_multi_version}, detect_reason='{detect_reason}'")

        return server_version, client_version, via_hint

    def _motd_to_html(self, motd_data: Any) -> str:
        """将 MOTD 数据转换为带颜色的 HTML，支持 § 颜色码和 JSON 格式"""
        parts = []
        current_color = '#ffffff'

        def _flush(text: str):
            if text:
                escaped = _html_escape(text)
                if current_color != '#ffffff':
                    parts.append(f'<span style="color:{current_color}">{escaped}</span>')
                else:
                    parts.append(escaped)

        def _walk(data, color):
            nonlocal current_color
            if isinstance(data, str):
                # 处理 § 颜色码
                segments = re.split(r'(§[0-9a-fk-or])', data)
                for seg in segments:
                    m = re.match(r'§([0-9a-f])', seg, re.IGNORECASE)
                    if m:
                        code = m.group(1).lower()
                        _flush('')  # 保存当前颜色段
                        current_color = MINECRAFT_COLOR_MAP.get(code, current_color)
                    elif seg:
                        _flush(seg)
            elif isinstance(data, dict):
                c = color
                if 'color' in data:
                    c = data['color']
                if c:
                    current_color = c
                _walk(data.get('text', ''), c)
                for item in data.get('extra', []):
                    _walk(item, c)
            elif isinstance(data, list):
                for item in data:
                    _walk(item, color)

        _walk(motd_data, current_color)
        _flush('')  # flush remaining
        return ''.join(parts) if parts else _html_escape(str(motd_data))

    def _format_latency(self, result: Dict[str, Any]) -> str:
        """格式化延迟文本，兼容 API 和直连查询字段。"""
        latency = result.get("latency_ms") or result.get("latency")
        if isinstance(latency, (int, float)):
            return f"{int(round(latency))}ms"
        if isinstance(latency, str) and latency.strip():
            value = latency.strip()
            return value if value.lower().endswith("ms") else f"{value}ms"
        return "未知"

    def _latency_class(self, result: Dict[str, Any]) -> str:
        """按延迟区间返回语义色，未知值保持普通文字。"""
        latency = result.get("latency_ms") or result.get("latency")
        if isinstance(latency, str):
            match = re.search(r"\d+(?:\.\d+)?", latency)
            latency = float(match.group(0)) if match else None
        if not isinstance(latency, (int, float)):
            return ""
        if latency >= 500:
            return "latency-danger"
        if latency >= 200:
            return "latency-warning"
        return "latency-good"

    async def _fetch_random_wallpaper_bytes(
        self, session: aiohttp.ClientSession
    ) -> bytes:
        """从受信任的壁纸接口下载一张原图，供后台缓存任务处理。"""
        api_url = f"{WALLPAPER_API_BASE}?{self.wallpaper_category}"
        async with session.get(api_url, allow_redirects=True) as resp:
            if resp.status != 200:
                raise RuntimeError(f"壁纸选择接口返回 HTTP {resp.status}")
            if (
                resp.url.scheme != "https"
                or (resp.url.host or "").lower() not in WALLPAPER_ALLOWED_HOSTS
                or resp.url.port not in (None, 443)
            ):
                raise RuntimeError("壁纸选择接口跳转到了未允许的域名")
            payload = await resp.json(content_type=None)

        data = payload.get("data") if isinstance(payload, dict) else None
        link = data.get("link") if isinstance(data, dict) else None
        if not isinstance(link, str) or not link.strip():
            raise RuntimeError("壁纸选择接口未返回图片地址")

        parsed = urlparse(link.strip())
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").lower() not in WALLPAPER_ALLOWED_HOSTS
            or parsed.port not in (None, 443)
        ):
            raise RuntimeError("壁纸图片地址未通过 HTTPS/域名校验")

        async with session.get(link.strip(), allow_redirects=True) as resp:
            if resp.status != 200:
                raise RuntimeError(f"壁纸图片返回 HTTP {resp.status}")
            if (
                (resp.url.host or "").lower() not in WALLPAPER_ALLOWED_HOSTS
                or resp.url.port not in (None, 443)
            ):
                raise RuntimeError("壁纸图片跳转到了未允许的域名")

            content_type = resp.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if content_type not in WALLPAPER_ALLOWED_TYPES:
                raise RuntimeError(f"壁纸响应类型不是图片: {content_type or 'unknown'}")

            declared_size = resp.headers.get("Content-Length")
            if declared_size:
                try:
                    if int(declared_size) > WALLPAPER_MAX_BYTES:
                        raise RuntimeError("壁纸图片超过 6 MiB 限制")
                except ValueError:
                    pass
            chunks = []
            received = 0
            async for chunk in resp.content.iter_chunked(64 * 1024):
                received += len(chunk)
                if received > WALLPAPER_MAX_BYTES:
                    raise RuntimeError("壁纸图片超过 6 MiB 限制")
                chunks.append(chunk)
            image_bytes = b"".join(chunks)
            if not image_bytes:
                raise RuntimeError("壁纸图片内容为空")
        return image_bytes

    @staticmethod
    def _normalize_wallpaper_bytes(image_bytes: bytes) -> bytes:
        """裁切并压缩为 1280×720 JPEG，减少缓存占用和渲染请求体积。"""
        with PILImage.open(BytesIO(image_bytes)) as source:
            source = ImageOps.exif_transpose(source)
            normalized = ImageOps.fit(
                source.convert("RGB"),
                (1280, 720),
                method=PILImage.Resampling.LANCZOS,
            )
            output = BytesIO()
            normalized.save(
                output,
                format="JPEG",
                quality=85,
                optimize=True,
                progressive=True,
            )
        return output.getvalue()

    def _cached_wallpaper_paths(self) -> list[Path]:
        self.wallpaper_cache_dir.mkdir(parents=True, exist_ok=True)
        return sorted(
            path
            for path in self.wallpaper_cache_dir.glob("*.jpg")
            if path.is_file() and 0 < path.stat().st_size <= WALLPAPER_MAX_BYTES
        )

    @staticmethod
    def _write_wallpaper_file(path: Path, image_bytes: bytes) -> bool:
        if path.exists():
            return False
        temp_path = path.with_suffix(".tmp")
        temp_path.write_bytes(image_bytes)
        temp_path.replace(path)
        return True

    async def _prefetch_wallpaper_cache(self) -> None:
        """在后台补足壁纸缓存；查询流程从不等待这个网络任务。"""
        if not self.enable_random_wallpaper:
            return
        async with self._wallpaper_refill_lock:
            paths = await asyncio.to_thread(self._cached_wallpaper_paths)
            if len(paths) >= self.wallpaper_cache_target:
                return

            started = time.monotonic()
            initial_count = len(paths)
            attempts = 0
            consecutive_empty_batches = 0
            max_attempts = max(self.wallpaper_cache_target * 5, 50)
            headers = {
                "Accept": "application/json, image/*;q=0.9",
                "User-Agent": f"AstrBot-MOTD/{PLUGIN_VERSION}",
            }
            request_timeout = aiohttp.ClientTimeout(
                total=self.wallpaper_timeout,
                connect=min(5, self.wallpaper_timeout),
            )
            logger.info(
                f"[MOTD] 开始后台预取壁纸: cached={initial_count}, "
                f"target={self.wallpaper_cache_target}"
            )

            async with aiohttp.ClientSession(
                headers=headers, timeout=request_timeout
            ) as session:
                while len(paths) < self.wallpaper_cache_target and attempts < max_attempts:
                    batch_size = min(
                        self.wallpaper_prefetch_concurrency,
                        self.wallpaper_cache_target - len(paths),
                    )
                    attempts += batch_size
                    results = await asyncio.gather(
                        *(self._fetch_random_wallpaper_bytes(session) for _ in range(batch_size)),
                        return_exceptions=True,
                    )
                    added = 0
                    for result in results:
                        if not isinstance(result, bytes):
                            continue
                        try:
                            normalized = await asyncio.to_thread(
                                self._normalize_wallpaper_bytes, result
                            )
                            digest = hashlib.sha256(normalized).hexdigest()
                            cache_path = self.wallpaper_cache_dir / f"{digest}.jpg"
                            if await asyncio.to_thread(
                                self._write_wallpaper_file, cache_path, normalized
                            ):
                                added += 1
                        except Exception as exc:
                            logger.info(f"[MOTD] 跳过无效壁纸: {type(exc).__name__}")

                    paths = await asyncio.to_thread(self._cached_wallpaper_paths)
                    if added:
                        consecutive_empty_batches = 0
                        if len(paths) % 10 < added or len(paths) >= self.wallpaper_cache_target:
                            logger.info(
                                f"[MOTD] 壁纸缓存进度: "
                                f"{len(paths)}/{self.wallpaper_cache_target}"
                            )
                    else:
                        consecutive_empty_batches += 1
                        if consecutive_empty_batches >= 5:
                            logger.warning(
                                "[MOTD] 壁纸预取连续失败，60 秒后自动重试"
                            )
                            break
                    await asyncio.sleep(0.25)

            logger.info(
                f"[MOTD] 壁纸后台预取结束: cached={len(paths)}/"
                f"{self.wallpaper_cache_target}, added={len(paths) - initial_count}, "
                f"elapsed={time.monotonic() - started:.1f}s"
            )

    async def _wallpaper_prefetch_runner(self) -> None:
        """预取失败时低频重试，直到本地缓存达到目标数量。"""
        while self.enable_random_wallpaper:
            try:
                await self._prefetch_wallpaper_cache()
                cached = len(await asyncio.to_thread(self._cached_wallpaper_paths))
                if cached >= self.wallpaper_cache_target:
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[MOTD] 壁纸后台预取异常，60 秒后重试: {exc}")
            await asyncio.sleep(60)

    def _ensure_wallpaper_prefetch(self) -> None:
        if not self.enable_random_wallpaper:
            return
        task = self._wallpaper_prefetch_task
        if task is not None and not task.done():
            return
        self._wallpaper_prefetch_task = asyncio.create_task(
            self._wallpaper_prefetch_runner()
        )

    async def _get_random_wallpaper_src(self) -> str:
        """只从本地缓存随机读取壁纸，绝不在 MOTD 查询路径请求外网。"""
        if not self.enable_random_wallpaper:
            return ""
        self._ensure_wallpaper_prefetch()
        try:
            paths = await asyncio.to_thread(self._cached_wallpaper_paths)
            if not paths:
                return self._last_wallpaper_src
            alternatives = [
                path for path in paths if str(path) != self._last_wallpaper_path
            ]
            selected = random.choice(alternatives or paths)
            image_bytes = await asyncio.to_thread(selected.read_bytes)
            wallpaper_src = (
                "data:image/jpeg;base64,"
                + base64.b64encode(image_bytes).decode("ascii")
            )
            self._last_wallpaper_path = str(selected)
            self._last_wallpaper_src = wallpaper_src
            return wallpaper_src
        except Exception as exc:
            logger.warning(f"[MOTD] 本地壁纸缓存读取失败，使用回退背景: {exc}")
            return self._last_wallpaper_src

    def _player_avatar_identity(self, player: Dict[str, Any]) -> Tuple[str, str]:
        """返回经过严格校验的头像标识和缓存键，UUID 优先。"""
        raw_id = str(player.get("uuid") or player.get("id") or "").strip()
        uuid = re.sub(r"-", "", raw_id).lower()
        if re.fullmatch(r"[0-9a-f]{32}", uuid):
            return uuid, f"uuid:{uuid}"

        raw_name = re.sub(r"§[0-9A-FK-ORa-fk-or]", "", str(player.get("name") or "")).strip()
        if re.fullmatch(r"[A-Za-z0-9_]{1,16}", raw_name):
            return raw_name, f"name:{raw_name.lower()}"
        return "", ""

    async def _fetch_player_avatar(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        player: Dict[str, Any],
    ) -> str:
        """使用固定头像源下载玩家头像，失败返回空字符串交给 CSS 占位。"""
        identifier, cache_key = self._player_avatar_identity(player)
        if not identifier:
            return ""
        cached = self._avatar_cache.get(cache_key)
        if cached:
            return cached

        encoded = quote(identifier, safe="")
        urls = [
            f"https://mc-heads.net/avatar/{encoded}/64.png",
            f"https://minotar.net/helm/{encoded}/64.png",
        ]

        async with semaphore:
            for url in urls:
                try:
                    async with session.get(url, allow_redirects=True) as resp:
                        if resp.status < 200 or resp.status >= 300:
                            continue
                        if (
                            resp.url.scheme != "https"
                            or (resp.url.host or "").lower() not in AVATAR_ALLOWED_HOSTS
                            or resp.url.port not in (None, 443)
                        ):
                            continue
                        if resp.headers.get("X-Account-Valid", "").lower() == "false":
                            continue

                        content_type = resp.headers.get("Content-Type", "").split(";", 1)[0].lower()
                        if content_type != "image/png":
                            continue
                        declared_size = resp.headers.get("Content-Length")
                        if declared_size and int(declared_size) > AVATAR_MAX_BYTES:
                            continue

                        chunks = []
                        received = 0
                        async for chunk in resp.content.iter_chunked(16 * 1024):
                            received += len(chunk)
                            if received > AVATAR_MAX_BYTES:
                                chunks = []
                                break
                            chunks.append(chunk)
                        if not chunks:
                            continue

                        avatar_src = f"data:image/png;base64,{base64.b64encode(b''.join(chunks)).decode('ascii')}"
                        if len(self._avatar_cache) >= 128:
                            self._avatar_cache.pop(next(iter(self._avatar_cache)))
                        self._avatar_cache[cache_key] = avatar_src
                        return avatar_src
                except Exception as e:
                    logger.info(f"[MOTD] 玩家头像源请求失败: provider={urlparse(url).hostname}, error={e}")
        return ""

    async def _hydrate_player_avatars(self, result: Dict[str, Any], avatar_limit: int = AVATAR_DISPLAY_LIMIT) -> None:
        """并发预取将要展示的玩家头像，任何失败都不影响状态卡。"""
        if not self.enable_player_avatars or "error" in result:
            return
        sample = result.get("players", {}).get("sample", []) or []
        players = [player for player in sample if isinstance(player, dict)]
        if not players:
            return
        display_count = avatar_limit - 1 if len(players) > avatar_limit else avatar_limit
        targets = players[:display_count]
        timeout = aiohttp.ClientTimeout(total=5, connect=3)
        headers = {
            "Accept": "image/png",
            "User-Agent": f"AstrBot-MOTD/{PLUGIN_VERSION}",
        }
        semaphore = asyncio.Semaphore(4)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            avatars = await asyncio.wait_for(
                asyncio.gather(
                    *(self._fetch_player_avatar(session, semaphore, player) for player in targets),
                    return_exceptions=True,
                ),
                timeout=11,
            )
        for player, avatar in zip(targets, avatars):
            if isinstance(avatar, str) and avatar.startswith("data:image/png;base64,"):
                player["_avatar_src"] = avatar

    def _server_icon_src(self, result: Dict[str, Any]) -> str:
        """提取服务器图标，返回可放入 img src 的字符串。"""
        icon = result.get("favicon") or result.get("icon") or ""
        if not isinstance(icon, str):
            return ""
        icon = icon.strip()
        if not icon:
            return ""
        if icon.startswith(("http://", "https://", "data:image/")):
            return icon
        if icon.startswith("base64://"):
            icon = icon.removeprefix("base64://")
        elif "base64," in icon:
            icon = icon.split("base64,", 1)[1]
        return f"data:image/png;base64,{icon}"

    def _format_response(self, result: Dict[str, Any], server_address: str, is_java: bool = True, avatar_limit: int = AVATAR_DISPLAY_LIMIT) -> Dict[str, Any]:
        """格式化查询结果为 HTML 模板上下文字典"""
        if "error" in result:
            logger.info(f"[MOTD] 格式化错误结果: server='{server_address}', error='{result['error']}'")
            return {
                "is_error": True, "is_java": is_java,
                "server_address": server_address,
                "server_display": _html_escape(server_address),
                "error_msg": result["error"],
                "edition_label": "Java" if is_java else "Bedrock",
                "status_label": "离线",
                "status_class": "offline",
                "online": 0,
                "max_players": 0,
                "online_display": "—",
                "server_version": "—",
                "client_version": "",
                "via_hint": "",
                "latency_text": "—",
                "latency_class": "",
                "motd_html": _html_escape(result["error"]),
                "player_list": [],
                "extra_count": 0,
                "player_line": "服务器暂时无法响应",
                "server_icon": "",
            }

        if is_java:
            version_info = result.get("version", {})
            players_info = result.get("players", {})
            description = result.get("description", "无描述")

            logger.info(
                f"[MOTD] Java 版原始数据: version={version_info}, "
                f"players={players_info.get('online', 0)}/"
                f"{players_info.get('max', 0)}, "
                f"sample_count={len(players_info.get('sample', []) or [])}"
            )

            server_version, client_version, via_hint = self._parse_version(version_info)
            motd_html = self._motd_to_html(description)
            online = players_info.get("online", 0)
            max_players = players_info.get("max", 0)
            sample = players_info.get("sample", [])
            player_entries = [
                p for p in sample
                if isinstance(p, dict) and str(p.get("name") or "").strip()
            ]
            display_count = avatar_limit - 1 if len(player_entries) > avatar_limit else avatar_limit
            visible_players = player_entries[:display_count]
            player_list = []
            for player in visible_players:
                clean_name = re.sub(
                    r"§[0-9A-FK-ORa-fk-or]",
                    "",
                    str(player.get("name") or "未知"),
                ).strip() or "未知"
                avatar_src = str(player.get("_avatar_src") or "")
                if not avatar_src.startswith("data:image/png;base64,"):
                    avatar_src = ""
                player_list.append({
                    "name": _html_escape(clean_name),
                    "avatar_src": avatar_src,
                })
            extra_count = max(0, len(player_entries) - len(player_list))
            if player_list:
                player_line = ", ".join(player["name"] for player in player_list)
                if extra_count > 0:
                    player_line += f" 等 {len(player_entries)} 人"
            else:
                player_line = "无" if int(online or 0) == 0 else "未公开"

            logger.info(f"[MOTD] 格式化结果: server_version='{server_version}', client_version='{client_version}', "
                        f"players={online}/{max_players}, via_hint='{via_hint}'")

            return {
                "is_error": False, "is_java": True,
                "server_address": server_address,
                "server_display": _html_escape(server_address),
                "edition_label": "Java",
                "status_label": "在线",
                "status_class": "online",
                "server_version": _html_escape(str(server_version)),
                "client_version": _html_escape(str(client_version)),
                "via_hint": _html_escape(str(via_hint)),
                "online": online, "max_players": max_players,
                "online_display": f"{online}/{max_players}",
                "player_list": player_list, "extra_count": extra_count,
                "player_line": _html_escape(player_line),
                "latency_text": self._format_latency(result),
                "latency_class": self._latency_class(result),
                "server_icon": self._server_icon_src(result),
                "motd_html": motd_html,
            }
        else:
            motd_html = self._motd_to_html(result.get("motd", "无描述"))
            server_version = result.get("version", "未知")
            online = result.get("online_players", 0)
            max_players = result.get("max_players", 0)
            player_line = "无" if int(online or 0) == 0 else "未公开"

            logger.info(f"[MOTD] 基岩版原始数据: {result}")
            logger.info(f"[MOTD] 基岩版格式化结果: version='{server_version}', players={online}/{max_players}")

            return {
                "is_error": False, "is_java": False,
                "server_address": server_address,
                "server_display": _html_escape(server_address),
                "edition_label": "Bedrock",
                "status_label": "在线",
                "status_class": "online",
                "server_version": _html_escape(str(server_version)),
                "client_version": _html_escape(str(server_version)),
                "via_hint": "",
                "online": online,
                "max_players": max_players,
                "online_display": f"{online}/{max_players}",
                "player_list": [], "extra_count": 0,
                "player_line": _html_escape(player_line),
                "latency_text": self._format_latency(result),
                "latency_class": self._latency_class(result),
                "server_icon": self._server_icon_src(result),
                "motd_html": motd_html,
            }

    async def _do_motd_query(self, event: AstrMessageEvent, server: str = "", is_java: bool = True):
        """执行 MOTD 查询的核心逻辑"""
        flow_started = time.monotonic()
        logger.info(f"[MOTD] 开始查询: server='{server}', is_java={is_java}")
        
        # 检查会话权限
        if not self._check_session_allowed(event):
            logger.info("[MOTD] 会话不在白名单中")
            return
        
        # 确定查询的服务器
        if not server or server.strip() == "":
            # 使用默认服务器
            if not self.default_server:
                await event.send(event.plain_result(
                    "❌ 未设置默认服务器\n"
                    "请使用: motd <服务器地址:端口> 查询指定服务器\n"
                    "或联系管理员使用 /motdconfig default <地址:端口> 设置"
                ))
                return
            server = self.default_server
            port = self.default_port
            logger.info(f"[MOTD] 使用默认服务器: {server}:{port}")
        else:
            # 用户指定了服务器，解析地址和端口
            server, port = self._parse_server_address(server.strip(), is_java=is_java)
            logger.info(f"[MOTD] 使用指定服务器: {server}:{port}")
        
        server_address = f"{server}:{port}"
        
        # 发送查询中提示
        await event.send(event.plain_result(f"🔍 正在查询服务器 {server_address} ..."))

        # 执行查询
        query_started = time.monotonic()
        logger.info(f"[MOTD] 开始执行查询，超时={self.query_timeout}秒")
        try:
            if is_java:
                result = await asyncio.wait_for(
                    query_java_server(server, port, self.query_timeout, self.use_api),
                    timeout=self.query_timeout + 5
                )
            else:
                result = await asyncio.wait_for(
                    query_bedrock_server(server, port, self.query_timeout),
                    timeout=self.query_timeout + 5
                )
            logger.info(f"[MOTD] 查询完成，结果: {result}")
        except asyncio.TimeoutError:
            logger.error("[MOTD] 查询超时")
            result = {"error": "查询超时，服务器响应时间过长"}
        except Exception as e:
            logger.error(f"[MOTD] 查询异常: {e}")
            result = {"error": f"查询异常: {str(e)}"}
        logger.info(
            f"[MOTD] 阶段耗时: server_query={time.monotonic() - query_started:.2f}s"
        )

        if is_java and "error" not in result:
            avatar_started = time.monotonic()
            try:
                await self._hydrate_player_avatars(result)
            except Exception as e:
                logger.warning(f"[MOTD] 玩家头像预取失败，使用占位头像: {e}")
            logger.info(
                f"[MOTD] 阶段耗时: player_avatars="
                f"{time.monotonic() - avatar_started:.2f}s"
            )

        wallpaper_started = time.monotonic()
        wallpaper_src = await self._get_random_wallpaper_src()
        logger.info(
            f"[MOTD] 阶段耗时: local_wallpaper="
            f"{time.monotonic() - wallpaper_started:.3f}s, "
            f"cache_hit={bool(wallpaper_src)}"
        )

        # 格式化并渲染为图片
        context = self._format_response(result, server_address, is_java=is_java)
        context["wallpaper_src"] = wallpaper_src
        context["queried_at"] = time.strftime("%H:%M:%S")
        render_started = time.monotonic()
        url = None
        # AstrBot's network renderer selects an endpoint per call. Do not let one
        # stalled endpoint consume the whole render budget: cancel it early and
        # give a fresh call a chance to select another healthy endpoint.
        if self.render_timeout >= 10:
            first_attempt_timeout = min(8, self.render_timeout - 5)
            render_attempt_timeouts = [first_attempt_timeout, self.render_timeout - first_attempt_timeout]
        else:
            render_attempt_timeouts = [self.render_timeout]

        for attempt, attempt_timeout in enumerate(render_attempt_timeouts, start=1):
            try:
                url = await asyncio.wait_for(
                    self.html_render(
                        MOTD_HTML_TEMPLATE,
                        context,
                        options={"full_page": True},
                    ),
                    timeout=attempt_timeout,
                )
                logger.info(
                    f"[MOTD] 阶段耗时: html_render="
                    f"{time.monotonic() - render_started:.2f}s, "
                    f"attempt={attempt}/{len(render_attempt_timeouts)}"
                )
                break
            except asyncio.TimeoutError:
                logger.warning(
                    f"[MOTD] HTML 渲染尝试 {attempt}/{len(render_attempt_timeouts)} "
                    f"超过 {attempt_timeout}s，切换渲染节点重试"
                )
            except Exception as exc:
                logger.warning(
                    f"[MOTD] HTML 渲染尝试 {attempt}/{len(render_attempt_timeouts)} 失败，"
                    f"切换渲染节点重试: {exc}"
                )

        if url:
            send_started = time.monotonic()
            await event.send(event.image_result(url))
            logger.info(
                f"[MOTD] 阶段耗时: image_send="
                f"{time.monotonic() - send_started:.2f}s"
            )
        else:
            logger.error(
                f"[MOTD] 图片渲染在 {self.render_timeout}s 内未完成，跳过结果发送"
            )
        logger.info(
            f"[MOTD] 查询流程完成: total={time.monotonic() - flow_started:.2f}s"
        )

    async def _query_server_context(self, server_address: str, is_java: bool, avatar_limit: int = AVATAR_DISPLAY_LIMIT) -> Dict[str, Any]:
        """查询单台服务器并返回模板上下文。"""
        host, port = self._parse_server_address(server_address.strip(), is_java=is_java)
        normalized = self._join_server_address(host, port)
        query_started = time.monotonic()
        try:
            if is_java:
                result = await asyncio.wait_for(
                    query_java_server(host, port, self.query_timeout, self.use_api),
                    timeout=self.query_timeout + 5,
                )
            else:
                result = await asyncio.wait_for(
                    query_bedrock_server(host, port, self.query_timeout),
                    timeout=self.query_timeout + 5,
                )
        except asyncio.TimeoutError:
            result = {"error": "查询超时，服务器响应时间过长"}
        except Exception as exc:
            result = {"error": f"查询异常: {exc}"}

        logger.info(
            f"[MOTD] 单服查询完成: server='{normalized}', "
            f"ok={'error' not in result}, elapsed={time.monotonic() - query_started:.2f}s"
        )
        if is_java and "error" not in result:
            try:
                await self._hydrate_player_avatars(result, avatar_limit=avatar_limit)
            except Exception as exc:
                logger.warning(f"[MOTD] 玩家头像预取失败，使用占位头像: {exc}")
        return self._format_response(result, normalized, is_java=is_java, avatar_limit=avatar_limit)

    async def _do_motd_query_v2(
        self,
        event: AstrMessageEvent,
        server: str = "",
        is_java: bool = True,
    ):
        """显式地址渲染单服；无地址时并发查询最多两个默认服务器。"""
        if not self._check_session_allowed(event):
            logger.info("[MOTD] 会话不在白名单中")
            return

        explicit_server = str(server or "").strip()
        if explicit_server:
            host, port = self._parse_server_address(explicit_server, is_java=is_java)
            addresses = [self._join_server_address(host, port)]
        else:
            addresses = self.default_servers[:3] if is_java else self.default_servers[:1]
            if not addresses:
                await event.send(event.plain_result(
                    "❌ 未设置默认服务器\n"
                    "请让管理员使用 /motd set1 <地址>、/motd set2 <地址> 和 /motd set3 <地址> 设置"
                ))
                return

        if len(addresses) == 1:
            await event.send(event.plain_result(f"🔍 正在查询服务器 {addresses[0]} ..."))
        else:
            await event.send(event.plain_result(f"🔍 正在并发查询 {len(addresses)} 个默认服务器 ..."))

        flow_started = time.monotonic()
        contexts = await asyncio.gather(
            *(self._query_server_context(address, is_java, avatar_limit=16) for address in addresses)
        )
        for index, context in enumerate(contexts, start=1):
            context["index"] = f"{index:02d}"

        wallpaper_src = await self._get_random_wallpaper_src()
        queried_at = time.strftime("%H:%M:%S")
        if len(contexts) == 1:
            render_template = MOTD_HTML_TEMPLATE
            render_context = dict(contexts[0])
            render_context["queried_at"] = queried_at
            render_context["wallpaper_src"] = wallpaper_src
        else:
            render_template = MOTD_DUAL_TEMPLATE
            render_context = {
                "servers": contexts,
                "server_count": len(contexts),
                "online_server_count": sum(1 for item in contexts if not item["is_error"]),
                "total_online": sum(int(item.get("online") or 0) for item in contexts),
                "queried_at": queried_at,
                "wallpaper_src": wallpaper_src,
            }

        render_started = time.monotonic()
        url = None
        if self.render_timeout >= 10:
            first_attempt_timeout = min(8, self.render_timeout - 5)
            render_attempt_timeouts = [first_attempt_timeout, self.render_timeout - first_attempt_timeout]
        else:
            render_attempt_timeouts = [self.render_timeout]

        for attempt, attempt_timeout in enumerate(render_attempt_timeouts, start=1):
            try:
                url = await asyncio.wait_for(
                    self.html_render(
                        render_template,
                        render_context,
                        options={"full_page": True},
                    ),
                    timeout=attempt_timeout,
                )
                logger.info(
                    f"[MOTD] 组合卡片渲染完成: servers={len(contexts)}, "
                    f"elapsed={time.monotonic() - render_started:.2f}s, "
                    f"attempt={attempt}/{len(render_attempt_timeouts)}"
                )
                break
            except asyncio.TimeoutError:
                logger.warning(
                    f"[MOTD] HTML 渲染尝试 {attempt}/{len(render_attempt_timeouts)} "
                    f"超过 {attempt_timeout}s，切换渲染节点重试"
                )
            except Exception as exc:
                logger.warning(
                    f"[MOTD] HTML 渲染尝试 {attempt}/{len(render_attempt_timeouts)} 失败: {exc}"
                )

        if url:
            await event.send(event.image_result(url))
        else:
            logger.error(f"[MOTD] 图片渲染在 {self.render_timeout}s 内未完成，跳过结果发送")
        logger.info(
            f"[MOTD] 查询流程完成: servers={len(contexts)}, "
            f"total={time.monotonic() - flow_started:.2f}s"
        )

    async def _handle_motd_config(self, event: AstrMessageEvent, action: str, value: str = ""):
        """处理 /motd set1、/motd set2 与 /motd get。"""
        if self.admin_only_config and not self._is_admin(event):
            await event.send(event.plain_result("❌ 只有管理员才能使用此指令"))
            return

        action = str(action or "").strip().lower()
        value = str(value or "").strip()
        if action == "get":
            if value:
                await event.send(event.plain_result("❌ 用法: /motd get"))
                return
            lines = [f"  {index}. {address}" for index, address in enumerate(self.default_servers, start=1)]
            defaults = "\n".join(lines) if lines else "  未设置"
            await event.send(event.plain_result(
                "📋 MOTD 默认服务器\n"
                f"{defaults}\n"
                f"生效范围: {'所有会话' if self.enable_all_sessions else f'{len(self.enabled_sessions)} 个会话'}\n"
                f"查询超时: {self.query_timeout} 秒"
            ))
            return

        if action in {"remove", "rm", "del", "delete"}:
            slot_key = value.lower().strip()
            slot_map = {
                "set1": 0, "1": 0, "一": 0, "服1": 0, "1服": 0, "一服": 0,
                "set2": 1, "2": 1, "二": 1, "服2": 1, "2服": 1, "二服": 1,
                "set3": 2, "3": 2, "三": 2, "服3": 2, "3服": 2, "三服": 2,
            }
            if not slot_key or slot_key not in slot_map:
                await event.send(event.plain_result(
                    "❌ 请指定要清除的默认服务器序号\n"
                    "用法: /motd remove <set1|set2|set3> 或 /motd remove <1|2|3>\n"
                    "例如: /motd remove set1"
                ))
                return

            slot = slot_map[slot_key]
            if slot >= len(self.default_servers):
                await event.send(event.plain_result(
                    f"❌ 默认服务器 {slot + 1} 当前未设置，无需清除"
                ))
                return

            removed_address = self.default_servers[slot]
            updated_servers = list(self.default_servers)
            updated_servers.pop(slot)

            previous_servers = list(self.default_servers)
            previous_server = self.default_server
            previous_port = self.default_port
            previous_config_servers = self.config.get("default_servers", None)

            self.default_servers = updated_servers
            if updated_servers:
                self.default_server, self.default_port = self._parse_server_address(
                    updated_servers[0], is_java=True
                )
            else:
                self.default_server = ""
                self.default_port = JAVA_DEFAULT_PORT

            self.config["default_servers"] = list(updated_servers)
            self.config["default_server"] = self.default_server
            self.config["default_port"] = self.default_port

            try:
                self.config.save_config()
            except Exception as exc:
                self.default_servers = previous_servers
                self.default_server = previous_server
                self.default_port = previous_port
                if previous_config_servers is None:
                    self.config.pop("default_servers", None)
                else:
                    self.config["default_servers"] = previous_config_servers
                self.config["default_server"] = previous_server
                self.config["default_port"] = previous_port
                logger.error(f"[MOTD] 清除默认服务器失败: {exc}")
                await event.send(event.plain_result("❌ 配置保存失败，请查看 AstrBot 日志"))
                return

            lines = [f"  {index}. {address}" for index, address in enumerate(self.default_servers, start=1)]
            defaults = "\n".join(lines) if lines else "  未设置（当前无默认服务器）"
            await event.send(event.plain_result(
                f"✅ 已清除默认服务器 {slot + 1} ({removed_address})\n"
                f"当前默认服务器列表 ({len(self.default_servers)}/3):\n"
                f"{defaults}"
            ))
            return

        if action not in {"set1", "set2", "set3"}:
            await event.send(event.plain_result(
                "❓ 可用指令\n"
                "  /motd set1 <地址>\n"
                "  /motd set2 <地址>\n"
                "  /motd set3 <地址>\n"
                "  /motd remove <set1|set2|set3>\n"
                "  /motd get"
            ))
            return

        candidates = [part for part in re.split(r"[\s,，;；]+", value) if part]
        if len(candidates) != 1:
            await event.send(event.plain_result(
                "❌ 请提供一个服务器地址\n"
                f"用法: /motd {action} <地址>"
            ))
            return

        normalized = []
        for candidate in candidates:
            host, port = self._parse_server_address(candidate, is_java=True)
            if not host or not 1 <= int(port) <= 65535:
                await event.send(event.plain_result(f"❌ 无效服务器地址: {candidate}"))
                return
            address = self._join_server_address(host, port)
            if address not in normalized:
                normalized.append(address)
        slot = 0 if action == "set1" else (1 if action == "set2" else 2)
        updated_servers = list(self.default_servers[:3])
        if slot == 1 and not updated_servers:
            await event.send(event.plain_result("❌ 请先使用 /motd set1 <地址> 设置第一个默认服务器"))
            return
        if slot == 2 and len(updated_servers) < 2:
            await event.send(event.plain_result("❌ 请先设置前两个默认服务器后再设置第三个"))
            return
        if slot < len(updated_servers):
            updated_servers[slot] = normalized[0]
        else:
            updated_servers.append(normalized[0])
        if len(set(updated_servers)) != len(updated_servers):
            await event.send(event.plain_result("❌ 默认服务器地址不能重复"))
            return

        await event.send(event.plain_result(f"🔍 正在验证默认服务器 {slot + 1} ..."))

        async def _validate(address: str):
            host, port = self._parse_server_address(address, True)
            return await asyncio.wait_for(
                query_java_server(host, port, self.query_timeout, self.use_api),
                timeout=self.query_timeout + 5,
            )

        validation = await asyncio.gather(_validate(normalized[0]), return_exceptions=True)
        failures = []
        for address, result in zip(normalized, validation):
            if isinstance(result, Exception):
                failures.append(f"{address}: {result}")
            elif "error" in result:
                failures.append(f"{address}: {result['error']}")
        if failures:
            await event.send(event.plain_result(
                "❌ 以下服务器验证失败，未修改配置\n" + "\n".join(failures)
            ))
            return

        previous_servers = list(self.default_servers)
        previous_server = self.default_server
        previous_port = self.default_port
        previous_config_servers = self.config.get("default_servers", None)
        self.default_servers = updated_servers
        self.default_server, self.default_port = self._parse_server_address(updated_servers[0], True)
        self.config["default_servers"] = list(updated_servers)
        self.config["default_server"] = self.default_server
        self.config["default_port"] = self.default_port
        try:
            self.config.save_config()
        except Exception as exc:
            self.default_servers = previous_servers
            self.default_server = previous_server
            self.default_port = previous_port
            if previous_config_servers is None:
                self.config.pop("default_servers", None)
            else:
                self.config["default_servers"] = previous_config_servers
            self.config["default_server"] = previous_server
            self.config["default_port"] = previous_port
            logger.error(f"[MOTD] 保存默认服务器失败: {exc}")
            await event.send(event.plain_result("❌ 配置保存失败，请查看 AstrBot 日志"))
            return
        await event.send(event.plain_result(
            f"✅ 默认服务器 {slot + 1} 已更新\n  {normalized[0]}"
        ))

    async def _dispatch_motd(self, event: AstrMessageEvent, command: str, argument: str = ""):
        command = str(command or "").strip().lower()
        argument = str(argument or "").strip()
        if command == "motd":
            action, _, value = argument.partition(" ")
            if action.lower() in {"set", "set1", "set2", "set3", "get", "remove", "rm", "del", "delete"}:
                await self._handle_motd_config(event, action, value)
            else:
                await self._do_motd_query_v2(event, argument, is_java=True)
        elif command == "motd-bedrock":
            await self._do_motd_query_v2(event, argument, is_java=False)
        else:
            await self._do_motd_query_v2(event, argument, is_java=True)
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息，让群聊中的 MOTD 指令不依赖唤醒前缀。"""
        message = event.message_str.strip()
        if not re.match(r'^/?(?:motd|在线)', message, re.IGNORECASE):
            return

        motd_pattern = r'^/?(motd(?:-bedrock)?|在线)(?:\s+(.+))?$'
        match = re.match(motd_pattern, message, re.IGNORECASE)
        if match:
            logger.info(f"[MOTD] 匹配到 motd 指令: {message}")
            await self._dispatch_motd(event, match.group(1), match.group(2) or "")

    @filter.command("motd", alias={"/motd"})
    async def motd_query_cmd(self, event: AstrMessageEvent, argument: GreedyStr = GreedyStr):
        """Java 查询以及 set/get 配置入口。"""
        raw_argument = "" if argument is GreedyStr else str(argument or "")
        await self._dispatch_motd(event, "motd", raw_argument)

    @filter.command("在线", alias={"/在线"})
    async def online_query_cmd(self, event: AstrMessageEvent, server: GreedyStr = GreedyStr):
        """兼容旧的中文查询入口。"""
        raw_server = "" if server is GreedyStr else str(server or "")
        await self._dispatch_motd(event, "在线", raw_server)

    @filter.command("motd-bedrock", alias={"/motd-bedrock"})
    async def motd_bedrock_query_cmd(self, event: AstrMessageEvent, server: GreedyStr = GreedyStr):
        """基岩版 MOTD 查询指令。"""
        raw_server = "" if server is GreedyStr else str(server or "")
        await self._dispatch_motd(event, "motd-bedrock", raw_server)

    async def _legacy_motd_config_cmd(self, event: AstrMessageEvent, action: str = "", value: str = ""):
        """MOTD 插件配置指令"""
        # 获取原始消息，检查是否以 / 开头
        logger.info(f"[MOTD] 收到 /motdconfig 指令: action={action}, value={value}")
        
        # 检查管理员权限
        if not self._is_admin(event):
            yield event.plain_result("❌ 只有管理员才能使用此指令")
            return
        
        action = action.lower().strip()
        
        if action == "default":
            if not value:
                yield event.plain_result("❌ 请提供服务器地址\n用法: /motdconfig default <服务器地址:端口>")
                return
            
            server, port = self._parse_server_address(value, is_java=True)
            
            # 验证服务器是否可连接
            yield event.plain_result(f"🔍 正在验证服务器 {server}:{port} ...")
            
            try:
                result = await asyncio.wait_for(
                    query_java_server(server, port, self.query_timeout, self.use_api),
                    timeout=self.query_timeout + 5
                )
            except asyncio.TimeoutError:
                result = {"error": "验证超时"}
            except Exception as e:
                result = {"error": str(e)}
            
            if "error" in result:
                yield event.plain_result(
                    f"❌ 无法连接到服务器\n"
                    f"地址: {server}:{port}\n"
                    f"错误: {result['error']}\n"
                    f"请检查地址是否正确，或服务器是否在线"
                )
                return
            
            # 更新配置
            self.default_server = server
            self.default_port = port
            self.config["default_server"] = server
            self.config["default_port"] = port
            
            # 保存配置
            try:
                self.config.save_config()
                logger.info(f"[MOTD] 配置已保存: {server}:{port}")
            except Exception as e:
                logger.error(f"[MOTD] 保存配置失败: {e}")
            
            yield event.plain_result(
                f"✅ 默认服务器设置成功\n"
                f"地址: {server}:{port}\n"
                f"MOTD: {self._format_motd(result.get('description', '无描述'))}"
            )
        
        elif action == "get":
            default = f"{self.default_server}:{self.default_port}" if self.default_server else "未设置"
            sessions = "所有会话" if self.enable_all_sessions else f"{len(self.enabled_sessions)} 个会话"
            
            yield event.plain_result(
                f"📋 MOTD 插件配置\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🖥️ 默认服务器: {default}\n"
                f"💬 生效范围: {sessions}\n"
                f"🔒 仅管理员配置: {'是' if self.admin_only_config else '否'}\n"
                f"⏱️ 查询超时: {self.query_timeout}秒\n"
                f"🌐 使用 API 查询: {'是' if self.use_api else '否'}"
            )
        
        else:
            yield event.plain_result(
                "❓ 未知操作\n"
                "可用操作:\n"
                "  default <地址:端口> - 设置默认服务器\n"
                "  get - 查看当前配置"
            )

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """Bot 初始化完成时"""
        cached = len(await asyncio.to_thread(self._cached_wallpaper_paths))
        self._ensure_wallpaper_prefetch()
        logger.info("=" * 50)
        logger.info(f"[MOTD] 插件已加载 v{PLUGIN_VERSION}")
        logger.info("[MOTD] 支持 ViaVersion/Velocity/BungeeCord 多版本兼容")
        logger.info(f"[MOTD] 默认服务器数量: {len(self.default_servers)}")
        logger.info(f"[MOTD] 对所有会话生效: {self.enable_all_sessions}")
        logger.info(f"[MOTD] 使用 API 查询: {self.use_api}")
        logger.info(
            f"[MOTD] 本地壁纸缓存: {cached}/{self.wallpaper_cache_target}, "
            f"path={self.wallpaper_cache_dir}"
        )
        logger.info("=" * 50)

    async def terminate(self):
        """插件停用时取消后台壁纸预取。"""
        task = self._wallpaper_prefetch_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._wallpaper_prefetch_task = None
