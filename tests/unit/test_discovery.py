"""Unit tests for LAN Discovery packet parsing and protocol."""

from __future__ import annotations

import json
from bambu_monitor.bambu.discovery import (
    DiscoveredPrinter,
    DiscoveryProtocol,
    parse_discovery_packet,
)


def test_parse_json_broadcast_packet():
    raw_json = json.dumps({
        "dev_name": "Living Room A1",
        "dev_id": "01P00A123456789",
        "dev_model_name": "A1",
        "dev_ip": "192.168.1.42",
        "dev_connect_type": "lan",
    }).encode("utf-8")

    printer = parse_discovery_packet(raw_json, "192.168.1.42")
    assert printer is not None
    assert printer.serial == "01P00A123456789"
    assert printer.name == "Living Room A1"
    assert printer.model == "A1"
    assert printer.ip == "192.168.1.42"
    assert printer.connect_type == "lan"


def test_parse_ssdp_notify_packet():
    ssdp_text = (
        "NOTIFY * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        "Location: 192.168.1.55:8883\r\n"
        "DevId: 01P00B987654321\r\n"
        "DevModel: A1 Mini\r\n"
        "DevName: Workshop Printer\r\n"
    ).encode("utf-8")

    printer = parse_discovery_packet(ssdp_text, "192.168.1.55")
    assert printer is not None
    assert printer.serial == "01P00B987654321"
    assert printer.model == "A1 Mini"
    assert printer.name == "Workshop Printer"
    assert printer.ip == "192.168.1.55"


def test_parse_invalid_packet():
    assert parse_discovery_packet(b"hello random udp payload", "192.168.1.99") is None
    assert parse_discovery_packet(b"{}", "192.168.1.99") is None


def test_discovery_protocol_callback():
    found_printers = []
    protocol = DiscoveryProtocol(on_device_found=lambda p: found_printers.append(p))

    raw_json = json.dumps({
        "dev_id": "01P00TESTSER",
        "dev_ip": "10.0.0.5",
        "dev_model_name": "P1S",
        "dev_name": "Office P1S",
    }).encode("utf-8")

    protocol.datagram_received(raw_json, ("10.0.0.5", 2021))
    assert len(found_printers) == 1
    assert found_printers[0].serial == "01P00TESTSER"
    assert found_printers[0].ip == "10.0.0.5"
