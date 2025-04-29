"""Asyncio TCP server that accepts BMP streams from BGP routers."""
from __future__ import annotations

import asyncio
import struct
from datetime import datetime, timezone
from typing import Dict, Optional

import structlog

from api.database import SessionLocal
from api.models import BGPSpeaker
from core.config import settings

logger = structlog.get_logger(__name__)

BMP_HEADER_SIZE = 6
BMP_VERSION = 3


class BMPConnectionHandler:
    """Handle a single router's BMP TCP connection."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        speaker_registry: "BMPServer",
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.server = speaker_registry
        peer = writer.get_extra_info("peername")
        self.peer_ip: str = peer[0] if peer else "unknown"
        self.peer_port: int = peer[1] if peer else 0
        self.messages_received: int = 0
        self._running: bool = False

    async def handle(self) -> None:
        """Main connection loop — read BMP messages until connection closes."""
        logger.info("bmp_connection_opened", peer=self.peer_ip, port=self.peer_port)
        self.server.on_connect(self.peer_ip)
        self._running = True
        try:
            while self._running:
                msg = await self._read_message()
                if msg is None:
                    break
                from tasks.ingestion import parse_bmp_message_task

                parse_bmp_message_task.delay(msg.hex())
                self.messages_received += 1
                from api.middleware import BMP_MESSAGES_INGESTED

                BMP_MESSAGES_INGESTED.labels(message_type="tcp_raw").inc()
        except asyncio.IncompleteReadError:
            logger.info(
                "bmp_connection_closed",
                peer=self.peer_ip,
                messages=self.messages_received,
            )
        except Exception as exc:
            logger.error("bmp_connection_error", peer=self.peer_ip, error=str(exc))
        finally:
            self._running = False
            self.server.on_disconnect(self.peer_ip)
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass

    async def _read_message(self) -> Optional[bytes]:
        """Read exactly one BMP message from the stream."""
        try:
            header = await self.reader.readexactly(BMP_HEADER_SIZE)
        except asyncio.IncompleteReadError:
            return None

        version = header[0]
        if version != BMP_VERSION:
            raise ValueError(f"Unexpected BMP version {version}")

        total_length = struct.unpack_from(">I", header, 1)[0]
        remaining = total_length - BMP_HEADER_SIZE
        if remaining < 0:
            raise ValueError(f"Invalid BMP message length {total_length}")

        body = await self.reader.readexactly(remaining) if remaining > 0 else b""
        return header + body


class BMPServer:
    """Asyncio TCP server for receiving BMP streams."""

    def __init__(
        self,
        host: str = settings.BMP_LISTEN_HOST,
        port: int = settings.BMP_LISTEN_PORT,
    ) -> None:
        self.host = host
        self.port = port
        self._server: Optional[asyncio.Server] = None
        self._connections: Dict[str, BMPConnectionHandler] = {}

    async def start(self) -> None:
        """Start the TCP server and listen for incoming BMP connections."""
        self._server = await asyncio.start_server(
            self._handle_connection,
            host=self.host,
            port=self.port,
        )
        async with self._server:
            logger.info("bmp_server_started", host=self.host, port=self.port)
            await self._server.serve_forever()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        handler = BMPConnectionHandler(reader, writer, self)
        peer = handler.peer_ip
        self._connections[peer] = handler
        await handler.handle()

    def on_connect(self, peer_ip: str) -> None:
        """Update BGPSpeaker status when a router connects."""
        logger.info("router_connected", peer_ip=peer_ip)
        db = SessionLocal()
        try:
            speaker = (
                db.query(BGPSpeaker)
                .filter(BGPSpeaker.bmp_listen_address.contains(peer_ip))
                .first()
            )
            if speaker:
                speaker.status = "CONNECTED"
                speaker.last_seen = datetime.now(timezone.utc)
                db.commit()
        finally:
            db.close()

    def on_disconnect(self, peer_ip: str) -> None:
        """Update BGPSpeaker status when a router disconnects."""
        logger.info("router_disconnected", peer_ip=peer_ip)
        self._connections.pop(peer_ip, None)
        db = SessionLocal()
        try:
            speaker = (
                db.query(BGPSpeaker)
                .filter(BGPSpeaker.bmp_listen_address.contains(peer_ip))
                .first()
            )
            if speaker:
                speaker.status = "DISCONNECTED"
                db.commit()
        finally:
            db.close()

    @property
    def active_connections(self) -> int:
        return len(self._connections)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("bmp_server_stopped")
