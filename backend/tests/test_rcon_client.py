import asyncio

import pytest

from app.rcon.client import RconAuthenticationError, RconClient, decode_packet, encode_packet, parse_players


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
        return []

    transport, server, port = await start_server(handler)
    try:
        async with RconClient() as client:
            await client.connect("127.0.0.1", port, "secret")
            assert await client.command("#restart") == "ok"
        assert server.received == [b"\x00secret", b"\x01\x00#restart"]
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


def test_players_parser_preserves_variable_fields_and_raw_lines():
    response = "Players on server:\n#1 76561198000000000 Alice Example 192.168.1.50:2304 42\n2 Bob\n"
    assert parse_players(response) == [
        {
            "id": 1,
            "name": "Alice Example",
            "ip": "192.168.1.50",
            "ping": 42,
            "raw": "#1 76561198000000000 Alice Example 192.168.1.50:2304 42",
        },
        {"id": 2, "name": "Bob", "ip": None, "ping": None, "raw": "2 Bob"},
    ]
