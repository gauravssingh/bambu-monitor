"""LAN Discovery for Bambu Lab 3D printers via UDP broadcast and SSDP."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
from dataclasses import dataclass
from typing import Dict, List, Optional

from bambu_monitor.bambu.protocol import (
    BAMBU_DISCOVERY_PORT,
    BAMBU_MQTT_PORT,
    SSDP_MULTICAST_ADDR,
    SSDP_PORT,
)

logger = logging.getLogger(__name__)


@dataclass
class DiscoveredPrinter:
    serial: str
    ip: str
    model: str = "A1"
    name: str = "Bambu Printer"
    port: int = BAMBU_MQTT_PORT
    connect_type: str = "lan"


def parse_discovery_packet(data: bytes, sender_ip: str) -> Optional[DiscoveredPrinter]:
    """Parse raw UDP packet data (JSON or SSDP text format)."""
    text = data.decode("utf-8", errors="ignore").strip()

    # 1. JSON broadcast payload format
    if text.startswith("{") and text.endswith("}"):
        try:
            payload = json.loads(text)
            serial = payload.get("dev_id") or payload.get("dev_ip") or payload.get("serial")
            if not serial:
                return None
            return DiscoveredPrinter(
                serial=str(serial),
                ip=payload.get("dev_ip") or sender_ip,
                model=payload.get("dev_model_name") or payload.get("model") or "A1",
                name=payload.get("dev_name") or payload.get("name") or "Bambu Printer",
                connect_type=payload.get("dev_connect_type") or "lan",
            )
        except Exception:
            pass

    # 2. Key-value / SSDP NOTIFY format
    lines = text.splitlines()
    fields: Dict[str, str] = {}
    for line in lines:
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip().lower()] = v.strip()

    serial = fields.get("devid") or fields.get("serial") or fields.get("usn")
    if serial:
        # Strip uuid: prefix if present
        serial = re.sub(r"^uuid:", "", serial)
        ip = fields.get("location") or fields.get("ip") or sender_ip
        # Location may be ip:port or http://ip:port
        ip = re.sub(r"^http[s]?://", "", ip).split(":")[0]
        model = fields.get("devmodel") or fields.get("model") or "A1"
        name = fields.get("devname") or fields.get("name") or "Bambu Printer"
        return DiscoveredPrinter(
            serial=serial,
            ip=ip,
            model=model,
            name=name,
        )

    return None


class DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_device_found):
        self.on_device_found = on_device_found
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        printer = parse_discovery_packet(data, addr[0])
        if printer:
            self.on_device_found(printer)


async def discover_printers(timeout_seconds: float = 3.0) -> List[DiscoveredPrinter]:
    """Scan local network for Bambu Lab printers by listening on port 2021 and sending probes."""
    loop = asyncio.get_running_loop()
    discovered: Dict[str, DiscoveredPrinter] = {}

    def on_device(printer: DiscoveredPrinter):
        if printer.serial not in discovered:
            logger.info("Discovered Bambu printer: %s (%s) at %s", printer.name, printer.serial, printer.ip)
            discovered[printer.serial] = printer

    # Create UDP socket with broadcast and reuseaddr
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except Exception:
            pass
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setblocking(False)

    try:
        sock.bind(("0.0.0.0", BAMBU_DISCOVERY_PORT))
    except Exception as e:
        logger.debug("Port 2021 in use or permission denied (%s); using ephemeral port", e)
        sock.bind(("0.0.0.0", 0))

    transport, protocol = await loop.create_datagram_endpoint(
        lambda: DiscoveryProtocol(on_device),
        sock=sock,
    )

    try:
        # Send active discovery probe queries
        probe_msg = b"M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 1\r\nST: urn:bambulab-com:device:3dprinter:1\r\n\r\n"
        bcast_msg = json.dumps({"command": "discover"}).encode("utf-8")

        for _ in range(2):
            try:
                transport.sendto(probe_msg, (SSDP_MULTICAST_ADDR, SSDP_PORT))
                transport.sendto(bcast_msg, ("255.255.255.255", BAMBU_DISCOVERY_PORT))
            except Exception:
                pass
            await asyncio.sleep(0.5)

        # Wait remaining scan window
        remaining = max(0.1, timeout_seconds - 1.0)
        await asyncio.sleep(remaining)
    finally:
        transport.close()

    return list(discovered.values())
