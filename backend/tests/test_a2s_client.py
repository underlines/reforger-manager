"""Synthetic UDP coverage for the Source A2S_INFO client."""

from __future__ import annotations

import asyncio
import struct
import unittest

from app.a2s import A2SProtocolError, query

HEADER = b"\xff\xff\xff\xff"
INFO_REQUEST = HEADER + b"\x54Source Engine Query\x00"


def info_packet(*, name: str = "Badis Reforger", map_name: str = "Everon", players: int = 3, max_players: int = 32) -> bytes:
    return b"".join(
        [
            HEADER, b"\x49\x11", name.encode() + b"\x00", map_name.encode() + b"\x00",
            b"reforger\x00Arma Reforger\x00", struct.pack("<H", 1234),
            bytes([players, max_players, 0]), b"d", b"l", b"\x00\x01", b"1.8.0.10\x00",
            b"\x80", struct.pack("<H", 17777),
        ]
    )


class Responder(asyncio.DatagramProtocol):
    def __init__(self, replies: list[bytes]) -> None:
        self.replies = replies
        self.requests: list[bytes] = []
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: object) -> None:
        self.requests.append(data)
        if self.replies:
            self.transport.sendto(self.replies.pop(0), addr)  # type: ignore[union-attr]


class A2SClientTests(unittest.IsolatedAsyncioTestCase):
    async def start_responder(self, replies: list[bytes]) -> tuple[asyncio.DatagramTransport, Responder, int]:
        transport, protocol = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: Responder(replies), local_addr=("127.0.0.1", 0)
        )
        return transport, protocol, transport.get_extra_info("sockname")[1]

    async def test_direct_info_reply_parses_fields(self) -> None:
        transport, responder, port = await self.start_responder([info_packet()])
        try:
            info = await query("127.0.0.1", port)
        finally:
            transport.close()

        self.assertEqual(responder.requests, [INFO_REQUEST])
        self.assertEqual((info.name, info.map_name, info.players, info.max_players), ("Badis Reforger", "Everon", 3, 32))
        self.assertGreaterEqual(info.ping_ms, 0)

    async def test_challenge_then_info_reply(self) -> None:
        challenge = b"\x12\x34\x56\x78"
        transport, responder, port = await self.start_responder([HEADER + b"\x41" + challenge, info_packet(players=7)])
        try:
            info = await query("127.0.0.1", port)
        finally:
            transport.close()

        self.assertEqual(responder.requests, [INFO_REQUEST, INFO_REQUEST + challenge])
        self.assertEqual(info.players, 7)

    async def test_malformed_and_truncated_replies_raise_protocol_error(self) -> None:
        for packet in (
            b"not-a2s",
            HEADER + b"\x49\x11broken",
            HEADER + b"\x41\x12\x34\x56\x78unexpected",
        ):
            with self.subTest(packet=packet):
                transport, _responder, port = await self.start_responder([packet])
                try:
                    with self.assertRaises(A2SProtocolError):
                        await query("127.0.0.1", port)
                finally:
                    transport.close()


if __name__ == "__main__":
    unittest.main()
