"""Unit tests for BMP TCP server message framing."""
from __future__ import annotations

import asyncio
import struct
from unittest.mock import MagicMock, patch

import pytest

from api.bmp_server import BMP_HEADER_SIZE, BMP_VERSION, BMPConnectionHandler, BMPServer
from tests.fixtures.bgp_telemetry_generator import MockBGPTelemetryGenerator


class _MockStreamReader:
    """Minimal asyncio StreamReader stand-in for unit tests."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    async def readexactly(self, n: int) -> bytes:
        if self._pos + n > len(self._data):
            raise asyncio.IncompleteReadError(
                partial=self._data[self._pos :], expected=n
            )
        chunk = self._data[self._pos : self._pos + n]
        self._pos += n
        return chunk


class _MockStreamWriter:
    def __init__(self) -> None:
        self.closed = False

    def get_extra_info(self, key: str, default=None):
        if key == "peername":
            return ("192.168.1.100", 54321)
        return default

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


@pytest.fixture
def bmp_message() -> bytes:
    return MockBGPTelemetryGenerator().generate_update("10.0.0.0/24", 65001)


@pytest.mark.asyncio
async def test_read_message_returns_complete_bmp(bmp_message: bytes):
    reader = _MockStreamReader(bmp_message)
    writer = _MockStreamWriter()
    handler = BMPConnectionHandler(reader, writer, BMPServer())

    result = await handler._read_message()
    assert result == bmp_message
    assert result[0] == BMP_VERSION
    assert struct.unpack(">I", result[1:5])[0] == len(result)


@pytest.mark.asyncio
async def test_read_message_returns_none_on_eof():
    reader = _MockStreamReader(b"")
    writer = _MockStreamWriter()
    handler = BMPConnectionHandler(reader, writer, BMPServer())

    result = await handler._read_message()
    assert result is None


@pytest.mark.asyncio
async def test_read_message_rejects_bad_version():
    bad = bytes([2, 0, 0, 0, 6, 0])
    reader = _MockStreamReader(bad)
    writer = _MockStreamWriter()
    handler = BMPConnectionHandler(reader, writer, BMPServer())

    with pytest.raises(ValueError, match="version"):
        await handler._read_message()


@pytest.mark.asyncio
async def test_read_message_header_size_constant():
    assert BMP_HEADER_SIZE == 6


@pytest.mark.asyncio
async def test_handle_dispatches_celery_task(bmp_message: bytes):
    reader = _MockStreamReader(bmp_message)
    writer = _MockStreamWriter()
    server = BMPServer()
    handler = BMPConnectionHandler(reader, writer, server)

    with patch("tasks.ingestion.parse_bmp_message_task") as mock_task:
        mock_task.delay = MagicMock()
        await handler.handle()

    mock_task.delay.assert_called_once_with(bmp_message.hex())
    assert handler.messages_received == 1


@pytest.mark.asyncio
async def test_on_connect_updates_speaker_status(db_session, mock_speaker):
    mock_speaker.status = "DISCONNECTED"
    db_session.commit()

    server = BMPServer()
    server.on_connect("192.168.1.1")

    db_session.refresh(mock_speaker)
    assert mock_speaker.status == "CONNECTED"
    assert mock_speaker.last_seen is not None


@pytest.mark.asyncio
async def test_on_disconnect_updates_speaker_status(db_session, mock_speaker):
    mock_speaker.status = "CONNECTED"
    db_session.commit()

    server = BMPServer()
    server.on_disconnect("192.168.1.1")

    db_session.refresh(mock_speaker)
    assert mock_speaker.status == "DISCONNECTED"
