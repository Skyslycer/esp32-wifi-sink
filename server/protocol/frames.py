"""Bounded framing and playback timeline metadata."""
import asyncio
from dataclasses import dataclass
from enum import IntEnum
import struct

HEADER = struct.Struct('!4sBBHII')
AUDIO = struct.Struct('!16sIQQI')
MAX_PAYLOAD = 8192
UNSCHEDULED = (1 << 64) - 1


class MessageType(IntEnum):
    HELLO = 1
    COMMAND = 2
    ACK = 3
    EVENT = 4
    STATUS = 5
    FLOW = 7
    HEARTBEAT = 8
    ERROR = 9
    SBC = 11


def parse_header(data: bytes):
    if len(data) != HEADER.size:
        raise ValueError('truncated header')
    magic, version, kind, flags, sequence, length = HEADER.unpack(data)
    if magic != b'WSNK' or version != 1 or flags or not sequence or length > MAX_PAYLOAD:
        raise ValueError('invalid envelope')
    # Unknown type values are preserved so the connection handler can reject them.
    return kind, sequence, length


@dataclass(frozen=True)
class Frame:
    kind: int
    sequence: int
    payload: bytes

    def encode(self) -> bytes:
        header = HEADER.pack(b'WSNK', 1, self.kind, 0, self.sequence, len(self.payload))
        parse_header(header)
        return header + self.payload


async def read_frame(reader: asyncio.StreamReader, timeout: float = 15) -> Frame:
    async def receive():
        kind, sequence, length = parse_header(await reader.readexactly(HEADER.size))
        return Frame(kind, sequence, await reader.readexactly(length))
    return await asyncio.wait_for(receive(), timeout)


