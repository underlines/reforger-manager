"""Small asyncio client for the BattlEye UDP RCON protocol.

This is intentionally not compatible with Source RCON.  BattlEye's protocol
uses a UDP ``BE``/CRC32 envelope and a one-byte command sequence number.
"""

from __future__ import annotations

import asyncio
import re
import struct
import time
import zlib


_HEADER = b"BE"
_SENTINEL = b"\xff"
_LOGIN = 0x00
_COMMAND = 0x01
_SERVER_MESSAGE = 0x02
_KEEPALIVE_SECONDS = 40.0
_AUTH_TIMEOUT_SECONDS = 3.0
_RESPONSE_TIMEOUT_SECONDS = 5.0
_PACKET_TIMEOUT_SECONDS = 1.0
_MAX_MULTIPART_PACKETS = 32
_MAX_RESPONSE_BYTES = 64 * 1024


class RconError(RuntimeError):
    """Base exception for BattlEye RCON failures."""


class RconAuthenticationError(RconError):
    """The server rejected the configured RCON password."""


class RconTimeoutError(RconError):
    """The server did not produce a complete response in time."""


class RconProtocolError(RconError):
    """A packet was malformed, corrupt, or inconsistent."""


def encode_packet(payload: bytes) -> bytes:
    """Wrap a BattlEye payload in its documented UDP frame."""
    body = _SENTINEL + payload
    return _HEADER + struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF) + body


def decode_packet(packet: bytes) -> bytes:
    """Validate and unwrap a BattlEye UDP frame into its payload."""
    if len(packet) < 8 or packet[:2] != _HEADER or packet[6:7] != _SENTINEL:
        raise RconProtocolError("invalid BattlEye packet framing")
    expected = struct.unpack("<I", packet[2:6])[0]
    actual = zlib.crc32(packet[6:]) & 0xFFFFFFFF
    if actual != expected:
        raise RconProtocolError("BattlEye packet CRC32 mismatch")
    return packet[7:]


class _DatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None
        self.packets: asyncio.Queue[bytes] = asyncio.Queue()
        self.error: Exception | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            payload = decode_packet(data)
        except RconProtocolError:
            return
        # Server console messages must be acknowledged promptly or the server
        # drops the authenticated client after repeated delivery attempts.
        if len(payload) >= 2 and payload[0] == _SERVER_MESSAGE and self.transport:
            self.transport.sendto(encode_packet(bytes((_SERVER_MESSAGE, payload[1]))))
            return
        self.packets.put_nowait(payload)

    def error_received(self, exc: Exception) -> None:
        self.error = exc


class RconClient:
    """A serialized, authenticated BattlEye UDP RCON connection."""

    def __init__(self) -> None:
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _DatagramProtocol | None = None
        self._lock = asyncio.Lock()
        self._sequence = 0
        self._authenticated = False
        self._keepalive_task: asyncio.Task[None] | None = None
        self._last_command_at = 0.0

    async def __aenter__(self) -> "RconClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def connect(self, host: str, port: int, password: str) -> None:
        """Open and authenticate a connected UDP socket; no auth retries occur."""
        if self._transport is not None:
            raise RconError("RCON client is already connected")
        if not host or not 0 < port < 65536:
            raise ValueError("a host and UDP port in range 1-65535 are required")
        try:
            password_bytes = password.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("BattlEye RCON passwords must be ASCII") from exc

        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            _DatagramProtocol, remote_addr=(host, port)
        )
        self._transport = transport
        self._protocol = protocol
        try:
            self._send(bytes((_LOGIN,)) + password_bytes)
            response = await self._receive(_AUTH_TIMEOUT_SECONDS)
            if response != bytes((_LOGIN, 0x01)):
                raise RconAuthenticationError("BattlEye RCON authentication failed")
        except BaseException:
            await self.close()
            raise
        self._authenticated = True
        self._last_command_at = time.monotonic()
        self._keepalive_task = asyncio.create_task(self._keepalive(), name="battleye-rcon-keepalive")

    async def close(self) -> None:
        """Close the UDP socket and cancel its keepalive task."""
        task, self._keepalive_task = self._keepalive_task, None
        if task is not None:
            task.cancel()
            if task is not asyncio.current_task():
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        transport, self._transport = self._transport, None
        self._protocol = None
        self._authenticated = False
        if transport is not None:
            transport.close()

    async def command(self, command: str) -> str:
        """Issue one supported server command and return its complete response."""
        self._validate_command(command)
        async with self._lock:
            return await self._command(command)

    async def players(self) -> list[dict[str, object]]:
        """Return tolerant structured records from BattlEye's variable #players text."""
        return parse_players(await self.command("#players"))

    async def _command(self, command: str) -> str:
        if not self._authenticated:
            raise RconError("RCON client is not authenticated")
        sequence = self._sequence
        self._sequence = (sequence + 1) % 256
        try:
            command_bytes = command.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("BattlEye RCON commands must be ASCII") from exc
        self._send(bytes((_COMMAND, sequence)) + command_bytes)
        self._last_command_at = time.monotonic()
        return await self._collect_response(sequence)

    async def _keepalive(self) -> None:
        try:
            while True:
                await asyncio.sleep(_KEEPALIVE_SECONDS)
                if time.monotonic() - self._last_command_at < _KEEPALIVE_SECONDS:
                    continue
                async with self._lock:
                    if time.monotonic() - self._last_command_at >= _KEEPALIVE_SECONDS:
                        await self._command("")
        except asyncio.CancelledError:
            raise
        except RconError:
            # A later explicit command reports the failure; keepalive never retries.
            return

    def _send(self, payload: bytes) -> None:
        if self._transport is None:
            raise RconError("RCON socket is closed")
        self._transport.sendto(encode_packet(payload))

    async def _receive(self, timeout: float) -> bytes:
        if self._protocol is None:
            raise RconError("RCON socket is closed")
        if self._protocol.error is not None:
            raise RconError(f"RCON UDP error: {self._protocol.error}")
        try:
            return await asyncio.wait_for(self._protocol.packets.get(), timeout)
        except TimeoutError as exc:
            raise RconTimeoutError("BattlEye RCON response timed out") from exc

    async def _collect_response(self, sequence: int) -> str:
        deadline = time.monotonic() + _RESPONSE_TIMEOUT_SECONDS
        parts: dict[int, bytes] = {}
        total: int | None = None
        while True:
            remaining = min(_PACKET_TIMEOUT_SECONDS, deadline - time.monotonic())
            if remaining <= 0:
                raise RconTimeoutError("BattlEye RCON multipart response timed out")
            payload = await self._receive(remaining)
            if len(payload) < 2 or payload[0] != _COMMAND or payload[1] != sequence:
                continue  # Stale command replies are harmless while requests are serialized.
            response = payload[2:]
            if len(response) >= 3 and response[0] == 0:
                packet_total, packet_index = response[1], response[2]
                if not 0 < packet_total <= _MAX_MULTIPART_PACKETS or packet_index >= packet_total:
                    raise RconProtocolError("invalid BattlEye multipart response header")
                if total is not None and total != packet_total:
                    raise RconProtocolError("inconsistent BattlEye multipart response")
                total = packet_total
                parts[packet_index] = response[3:]
                if sum(map(len, parts.values())) > _MAX_RESPONSE_BYTES:
                    raise RconProtocolError("BattlEye response exceeds size limit")
                if len(parts) == total:
                    return b"".join(parts[index] for index in range(total)).decode("utf-8", "replace")
                continue
            if total is not None:
                raise RconProtocolError("mixed single and multipart BattlEye response")
            return response.decode("utf-8", "replace")

    @staticmethod
    def _validate_command(command: str) -> None:
        if command == "#players" or command in {"#restart", "#shutdown"}:
            return
        if re.fullmatch(r"#(?:kick|ban)\s+\d+", command):
            return
        if re.fullmatch(r"#say\s+\S.*", command):
            return
        raise ValueError("unsupported BattlEye RCON command")


_IP_RE = re.compile(r"(?<![\d.])(?P<ip>(?:\d{1,3}\.){3}\d{1,3})(?::\d+)?")
_PLAYER_RE = re.compile(r"^\s*#?(?P<id>\d+)\s+(?P<body>.+?)\s*$")
_PING_RE = re.compile(r"(?:\bping\s*[:=]?\s*|\s)(?P<ping>\d+)\s*$", re.IGNORECASE)


def parse_players(response: str) -> list[dict[str, object]]:
    """Parse common BattlEye #players layouts without discarding the original line."""
    players: list[dict[str, object]] = []
    for line in response.splitlines():
        match = _PLAYER_RE.match(line)
        if not match:
            continue
        body = match.group("body")
        ip_match = _IP_RE.search(body)
        ping_match = _PING_RE.search(body)
        name_end = min(
            (item.start() for item in (ip_match, ping_match) if item is not None), default=len(body)
        )
        name = body[:name_end].strip(" \t-|")
        # Some game versions include a long platform/identity number before the name.
        name = re.sub(r"^\d{8,}\s+", "", name).strip()
        players.append(
            {
                "id": int(match.group("id")),
                "name": name or None,
                "ip": ip_match.group("ip") if ip_match else None,
                "ping": int(ping_match.group("ping")) if ping_match else None,
                "raw": line,
            }
        )
    return players


__all__ = [
    "RconAuthenticationError",
    "RconClient",
    "RconError",
    "RconProtocolError",
    "RconTimeoutError",
    "decode_packet",
    "encode_packet",
    "parse_players",
]
