import asyncio

import pytest

from app.rcon.client import (
    RconAuthenticationError,
    RconClient,
    decode_packet,
    encode_packet,
    parse_bans,
    parse_players,
)


class BattleyeServer(asyncio.DatagramProtocol):
    def __init__(self, handler):
        self.handler = handler
        self.transport = None
        self.received = []

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        payload = decode_packet(data)
        self.received.append(payload)
        for reply in self.handler(payload):
            self.transport.sendto(encode_packet(reply), addr)


async def start_server(handler):
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: BattleyeServer(handler), local_addr=("127.0.0.1", 0)
    )
    return transport, protocol, transport.get_extra_info("sockname")[1]


def test_packet_codec_rejects_corruption():
    packet = encode_packet(b"\x01\x07#players")
    assert packet[:2] == b"BE"
    assert packet[6] == 0xFF
    assert decode_packet(packet) == b"\x01\x07#players"
    with pytest.raises(Exception):
        decode_packet(packet[:-1] + b"x")


@pytest.mark.asyncio
async def test_authentication_and_command_response():
    def handler(payload):
        if payload == b"\x00secret":
            return [b"\x00\x01"]
        if payload[:2] == b"\x01\x00":
            return [b"\x01\x00ok"]
        if payload[:2] == b"\x01\x01":  # @logout sent on close
            return [b"\x01\x01ok"]
        return []

    transport, server, port = await start_server(handler)
    try:
        async with RconClient() as client:
            await client.connect("127.0.0.1", port, "secret")
            assert await client.command("#restart") == "ok"
        # A best-effort @logout frees the server-side RCON slot on close.
        assert server.received == [b"\x00secret", b"\x01\x00#restart", b"\x01\x01@logout"]
    finally:
        transport.close()


@pytest.mark.asyncio
async def test_authentication_failure_closes_client():
    transport, _, port = await start_server(lambda payload: [b"\x00\x00"])
    try:
        with pytest.raises(RconAuthenticationError):
            await RconClient().connect("127.0.0.1", port, "bad")
    finally:
        transport.close()


@pytest.mark.asyncio
async def test_multipart_response_is_reassembled_out_of_order():
    def handler(payload):
        if payload[0] == 0:
            return [b"\x00\x01"]
        sequence = payload[1]
        return [
            bytes((1, sequence, 0, 2, 1)) + b"world",
            bytes((1, sequence, 0, 2, 0)) + b"hello ",
        ]

    transport, _, port = await start_server(handler)
    try:
        async with RconClient() as client:
            await client.connect("127.0.0.1", port, "secret")
            assert await client.command("#say hello") == "hello world"
    finally:
        transport.close()


REFORGER_PLAYERS = (
    "Players on server:\n"
    "0 ; 1a2b3c4d-5e6f-7081-92a3-b4c5d6e7f809 ; underlines\n"
    "1 ; 9f8e7d6c-5b4a-3210-fedc-ba9876543210 ; Some Player\n"
)


def _fields(record):
    return {key: record[key] for key in ("id", "name", "uid", "ip", "ping")}


def test_players_parser_reads_reforger_semicolon_rows():
    records = parse_players(REFORGER_PLAYERS)
    assert [_fields(record) for record in records] == [
        {
            "id": 0,
            "name": "underlines",
            "uid": "1a2b3c4d-5e6f-7081-92a3-b4c5d6e7f809",
            "ip": None,
            "ping": None,
        },
        {
            "id": 1,
            "name": "Some Player",
            "uid": "9f8e7d6c-5b4a-3210-fedc-ba9876543210",
            "ip": None,
            "ping": None,
        },
    ]


def test_players_parser_skips_header_echo_and_count_lines():
    response = (
        "processing command: players\n"
        "Players on server:\n"
        "0 ; 1a2b3c4d-5e6f-7081-92a3-b4c5d6e7f809 ; underlines\n"
        "2 players\n"
    )
    assert [_fields(record) for record in parse_players(response)] == [
        {
            "id": 0,
            "name": "underlines",
            "uid": "1a2b3c4d-5e6f-7081-92a3-b4c5d6e7f809",
            "ip": None,
            "ping": None,
        }
    ]


def test_players_parser_falls_back_to_legacy_space_separated_rows():
    records = parse_players("#1 76561198000000000 Alice Example 192.168.1.50:2304 42\n")
    assert _fields(records[0]) == {
        "id": 1,
        "name": "Alice Example",
        "uid": None,
        "ip": "192.168.1.50",
        "ping": 42,
    }


def test_players_parser_returns_empty_for_empty_and_unknown_command_replies():
    assert parse_players("") == []
    assert parse_players("Unknown command") == []


def test_validate_command_accepts_new_ban_forms():
    for command in (
        "#ban create 1a2b3c4d-5e6f-7081-92a3-b4c5d6e7f809 3600",
        "#ban create 76561198000000000 0",
        "#ban create Alice 86400 going on holiday",
        "#ban remove 1a2b3c4d-5e6f-7081-92a3-b4c5d6e7f809",
        "#ban list",
        "#ban list 2",
        "@logout",
    ):
        RconClient._validate_command(command)


def test_validate_command_rejects_unsafe_forms():
    for command in (
        "#ban wipe",
        "#ban create x -1",
        "#ban create a b",
        "#ban create Alice Example 3600",  # multi-word name is not addressable
        "rm -rf /",
        "@login",
    ):
        try:
            RconClient._validate_command(command)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {command!r}")


BANS_REPLY = (
    "processing command: bans\n"
    "Ban list (page 1):\n"
    "0 ; 1a2b3c4d-5e6f-7081-92a3-b4c5d6e7f809 ; 3600\n"
    "1 ; 9f8e7d6c-5b4a-3210-fedc-ba9876543210 ; permanent\n"
    "2 bans\n"
)


def test_bans_parser_reads_rows_and_skips_header_echo_and_count():
    assert parse_bans(BANS_REPLY) == [
        {
            "ban_id": "0",
            "uid": "1a2b3c4d-5e6f-7081-92a3-b4c5d6e7f809",
            "duration": "3600",
            "raw": "0 ; 1a2b3c4d-5e6f-7081-92a3-b4c5d6e7f809 ; 3600",
        },
        {
            "ban_id": "1",
            "uid": "9f8e7d6c-5b4a-3210-fedc-ba9876543210",
            "duration": "permanent",
            "raw": "1 ; 9f8e7d6c-5b4a-3210-fedc-ba9876543210 ; permanent",
        },
    ]


def test_bans_parser_returns_empty_for_empty_and_unknown_command_replies():
    assert parse_bans("") == []
    assert parse_bans("Unknown command") == []
