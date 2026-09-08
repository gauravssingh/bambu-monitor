"""Unit tests for Bambu MQTT protocol definitions and payload builders."""

from __future__ import annotations

from bambu_monitor.bambu.protocol import (
    BAMBU_LAN_USERNAME,
    BAMBU_MQTT_PORT,
    create_pause_payload,
    create_pushall_payload,
    create_resume_payload,
    create_stop_payload,
    get_report_topic,
    get_request_topic,
)


def test_protocol_constants():
    assert BAMBU_MQTT_PORT == 8883
    assert BAMBU_LAN_USERNAME == "bblp"


def test_topic_formatting():
    serial = "01P00A123456789"
    assert get_report_topic(serial) == "device/01P00A123456789/report"
    assert get_request_topic(serial) == "device/01P00A123456789/request"


def test_pushall_payload_builder():
    payload = create_pushall_payload(sequence_id="123")
    assert payload["pushing"]["sequence_id"] == "123"
    assert payload["pushing"]["command"] == "pushall"
    assert payload["pushing"]["version"] == 1


def test_print_control_payload_builders():
    pause_payload = create_pause_payload(sequence_id="1")
    assert pause_payload["print"]["command"] == "pause"

    resume_payload = create_resume_payload(sequence_id="2")
    assert resume_payload["print"]["command"] == "resume"

    stop_payload = create_stop_payload(sequence_id="3")
    assert stop_payload["print"]["command"] == "stop"
