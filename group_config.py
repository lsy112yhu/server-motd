"""Group-specific default server lists; intentionally reuses the plugin address parser."""
import ipaddress
import logging
import re

_LOG = logging.getLogger("astrbot")
GROUP_RE = re.compile(r"^[0-9]+$")


def valid_group_id(raw):
    group_id = str(raw or "").strip()
    return group_id if GROUP_RE.fullmatch(group_id) else ""


def parse_addresses(value, parser, joiner, logger=None):
    """Validate syntax strictly before using the existing server parser."""
    logger = logger or _LOG
    result = []
    for raw in re.split(r"[,，]", str(value or "")):
        address = raw.strip()
        if not address:
            continue
        try:
            if (any(ch.isspace() for ch in address)
                    or any(ch in address for ch in "/\\@?#%=;")
                    or len(address) > 255):
                raise ValueError("unsafe address characters")
            if address.startswith("["):
                match = re.fullmatch(r"\[([^\]]+)\](?::([0-9]+))?", address)
                if not match:
                    raise ValueError("invalid IPv6 address")
                ipaddress.IPv6Address(match.group(1))
            else:
                if address.count(":") > 1:
                    raise ValueError("IPv6 literals require brackets")
                if ":" in address and not re.fullmatch(r"[^:]+:[0-9]+", address):
                    raise ValueError("invalid explicit port")
            host, port = parser(address, is_java=True)
            if not host or not 1 <= int(port) <= 65535:
                raise ValueError("invalid host or port")
            normalized = joiner(host, port)
            if normalized not in result:
                result.append(normalized)
            if len(result) == 3:
                break
        except (ValueError, TypeError, OverflowError) as exc:
            logger.warning("[MOTD] 跳过无效的分群服务器地址: %s (%s)", address, exc)
    return result


def parse_group_server_rows(rows, parser, joiner, logger=None):
    logger = logger or _LOG
    if isinstance(rows, str):
        rows = [rows]
    if not isinstance(rows, list):
        logger.warning("[MOTD] group_servers 必须是列表")
        return {}
    result = {}
    for raw in rows:
        line = str(raw or "").strip()
        if "=" not in line:
            logger.warning("[MOTD] 跳过无效的分群配置（缺少 =）: %s", line)
            continue
        raw_group, address_list = line.split("=", 1)
        group_id = valid_group_id(raw_group)
        if not group_id:
            logger.warning("[MOTD] 跳过无效群号: %s", raw_group)
            continue
        addresses = parse_addresses(address_list, parser, joiner, logger)
        if not addresses:
            logger.warning("[MOTD] 跳过没有有效服务器的群配置: %s", group_id)
            continue
        if group_id in result:
            logger.warning("[MOTD] 群 %s 存在重复有效配置，以最后一条有效配置为准", group_id)
        result[group_id] = addresses
    return result


def serialize_group_servers(groups):
    return [
        f"{group_id}={','.join(addresses)}"
        for group_id, addresses in groups.items()
    ]
