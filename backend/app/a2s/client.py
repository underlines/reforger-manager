"""Small async Source A2S_INFO UDP client for Reforger live stats."""

from __future__ import annotations

import asyncio
import struct
import time
from dataclasses import dataclass

_HEADER = b"\xff\xff\xff\xff"
_INFO_REQUEST = _HEADER + b"\x54Source Engine Query\x00"
_INFO_RESPONSE = 0x49
_CHALLENGE_RESPONSE = 0x41
_QUERY_TIMEOUT_SECONDS = 1.5


class A2SError(RuntimeError):
    """Base exception for A2S query failures."""


class A2STimeoutError(A2SError, TimeoutError):
    """The server did not answer the A2S query within the bounded timeout."""


class A2SConnectionError(A2SError):
    """The UDP socket could not be opened or used."""


class A2SProtocolError(A2SError):
    """The server returned an unsupported or malformed A2S packet."""


@dataclass(frozen=True, slots=True)
class A2SInfo:
    name: str
    map_name: str
    players: int
    max_players: int
    ping_ms: float


class _ResponseProtocol(asyncio.DatagramProtocol):
    def __init__(self, request: bytes, response: asyncio.Future[bytes]) -> None:
        self._request = request
        self._response = response
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]
        self._transport.sendto(self._request)

    def datagram_received(self, data: bytes, _addr: object) -> None:
        if not self._response.done():
            self._response.set_result(data)

    def error_received(self, exc: Exception) -> None:
        if not self._response.done():
            self._response.set_exception(A2SConnectionError(f"UDP query failed: {exc}"))

    def connection_lost(self, exc: Exception | None) -> None:
        if exc is not None and not self._response.done():
            self._response.set_exception(A2SConnectionError(f"UDP connection closed: {exc}"))


async def _exchange(host: str, port: int, request: bytes, timeout: float) -> bytes:
    loop = asyncio.get_running_loop()
    response: asyncio.Future[bytes] = loop.create_future()
    transport: asyncio.DatagramTransport | None = None
    try:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _ResponseProtocol(request, response), remote_addr=(host, port)
        )
        return await asyncio.wait_for(response, timeout)
    except asyncio.TimeoutError as exc:
        raise A2STimeoutError(f"A2S query to {host}:{port} timed out") from exc
    except A2SError:
        raise
    except OSError as exc:
        raise A2SConnectionError(f"could not query {host}:{port}: {exc}") from exc
    finally:
        if transport is not None:
            transport.close()


def _read_cstring(packet: bytes, offset: int, field: str) -> tuple[str, int]:
    end = packet.find(b"\x00", offset)
    if end < 0:
        raise A2SProtocolError(f"truncated A2S_INFO {field} string")
    try:
        return packet[offset:end].decode("utf-8"), end + 1
    except UnicodeDecodeError as exc:
        raise A2SProtocolError(f"invalid UTF-8 in A2S_INFO {field} string") from exc


def _read_integer(packet: bytes, offset: int, format_: str, field: str) -> tuple[int, int]:
    size = struct.calcsize(format_)
    if len(packet) < offset + size:
        raise A2SProtocolError(f"truncated A2S_INFO {field}")
    return struct.unpack_from(format_, packet, offset)[0], offset + size


def _parse_info(packet: bytes) -> tuple[str, str, int, int]:
    if len(packet) < 6 or packet[:4] != _HEADER:
        raise A2SProtocolError("invalid A2S response header")
    if packet[4] != _INFO_RESPONSE:
        raise A2SProtocolError(f"expected A2S_INFO response, got type 0x{packet[4]:02x}")

    offset = 6  # response type plus the protocol version byte
    name, offset = _read_cstring(packet, offset, "name")
    map_name, offset = _read_cstring(packet, offset, "map")
    _, offset = _read_cstring(packet, offset, "folder")
    _, offset = _read_cstring(packet, offset, "game")
    _, offset = _read_integer(packet, offset, "<H", "app id")
    players, offset = _read_integer(packet, offset, "<B", "player count")
    max_players, offset = _read_integer(packet, offset, "<B", "max player count")
    _, offset = _read_integer(packet, offset, "<B", "bot count")
    _, offset = _read_integer(packet, offset, "<B", "server type")
    _, offset = _read_integer(packet, offset, "<B", "environment")
    _, offset = _read_integer(packet, offset, "<B", "visibility")
    _, offset = _read_integer(packet, offset, "<B", "VAC flag")
    _, offset = _read_cstring(packet, offset, "version")

    if offset == len(packet):
        return name, map_name, players, max_players

    edf, offset = _read_integer(packet, offset, "<B", "EDF")
    if edf & 0x80:
        _, offset = _read_integer(packet, offset, "<H", "port")
    if edf & 0x10:
        _, offset = _read_integer(packet, offset, "<Q", "SteamID")
    if edf & 0x40:
        _, offset = _read_integer(packet, offset, "<H", "SourceTV port")
        _, offset = _read_cstring(packet, offset, "SourceTV name")
    if edf & 0x20:
        _, offset = _read_cstring(packet, offset, "keywords")
    if edf & 0x01:
        _, offset = _read_integer(packet, offset, "<Q", "game ID")
    if offset != len(packet):
        raise A2SProtocolError("unexpected trailing bytes in A2S_INFO response")
    return name, map_name, players, max_players


async def query(host: str, port: int) -> A2SInfo:
    """Return live A2S_INFO fields, performing at most one challenge exchange."""
    started = time.monotonic()
    deadline = started + _QUERY_TIMEOUT_SECONDS

    async def exchange(request: bytes) -> bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise A2STimeoutError(f"A2S query to {host}:{port} timed out")
        return await _exchange(host, port, request, remaining)

    response = await exchange(_INFO_REQUEST)
    if response[:5] == _HEADER + bytes([_CHALLENGE_RESPONSE]):
        if len(response) != 9:
            raise A2SProtocolError("malformed A2S challenge response")
        challenge = response[5:9]
        response = await exchange(_INFO_REQUEST + challenge)

    name, map_name, players, max_players = _parse_info(response)
    return A2SInfo(
        name=name,
        map_name=map_name,
        players=players,
        max_players=max_players,
        ping_ms=(time.monotonic() - started) * 1000,
    )
