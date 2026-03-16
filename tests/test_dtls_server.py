"""Tests for the DTLS-PSK server."""

from __future__ import annotations

import asyncio
import subprocess
import time

import pytest
from hue_entertainment.dtls_psk import DTLSPSKServer

TEST_PSK_IDENTITY = "test-user-abc123"
TEST_PSK_KEY = bytes.fromhex("deadbeefcafebabe1234567890abcdef")
TEST_PORT = 22100  # Use a high port to avoid permission issues


def psk_callback(identity: str) -> bytes | None:
    """Test PSK lookup."""
    if identity == TEST_PSK_IDENTITY:
        return TEST_PSK_KEY
    return None


@pytest.fixture
def event_loop():
    """Create an event loop for tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


class TestDTLSPSKServer:
    """Test DTLS server lifecycle and handshake."""

    @pytest.mark.asyncio
    async def test_start_stop(self):
        """Server starts and stops without errors."""
        frames = []
        server = DTLSPSKServer(
            host="127.0.0.1",
            port=TEST_PORT,
            psk_callback=psk_callback,
            frame_callback=frames.append,
        )

        await server.async_start()
        await asyncio.sleep(0.1)
        await server.async_stop()

    @pytest.mark.asyncio
    async def test_dtls_handshake_and_receive(self):
        """Server completes DTLS handshake and receives data."""
        frames: list[bytes] = []
        frame_event = asyncio.Event()

        def on_frame(data: bytes) -> None:
            frames.append(data)
            if len(frames) >= 3:
                frame_event.set()

        server = DTLSPSKServer(
            host="127.0.0.1",
            port=TEST_PORT + 1,
            psk_callback=psk_callback,
            frame_callback=on_frame,
        )

        await server.async_start()

        try:
            # Use openssl s_client as the DTLS client
            proc = subprocess.Popen(
                [
                    "openssl",
                    "s_client",
                    "-dtls",
                    "-psk",
                    TEST_PSK_KEY.hex(),
                    "-psk_identity",
                    TEST_PSK_IDENTITY,
                    "-connect",
                    f"127.0.0.1:{TEST_PORT + 1}",
                    "-quiet",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # Send test frames
            test_data = b"HueStream" + b"\x02\x00" + b"\x00" * 41 + b"\x00\xff\xff\x00\x00\x00\x00"
            for _ in range(5):
                proc.stdin.write(test_data)
                proc.stdin.flush()
                time.sleep(0.05)

            # Wait for frames to arrive
            try:
                await asyncio.wait_for(frame_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

            proc.terminate()
            proc.wait(timeout=5)

            assert len(frames) >= 1, f"Expected frames but got {len(frames)}"
            assert frames[0].startswith(b"HueStream")

        finally:
            await server.async_stop()

    @pytest.mark.asyncio
    async def test_wrong_psk_rejected(self):
        """Server rejects a client with the wrong PSK."""
        frames = []
        server = DTLSPSKServer(
            host="127.0.0.1",
            port=TEST_PORT + 2,
            psk_callback=psk_callback,
            frame_callback=frames.append,
        )

        await server.async_start()

        try:
            proc = subprocess.Popen(
                [
                    "openssl",
                    "s_client",
                    "-dtls",
                    "-psk",
                    "0000000000000000",  # wrong key
                    "-psk_identity",
                    TEST_PSK_IDENTITY,
                    "-connect",
                    f"127.0.0.1:{TEST_PORT + 2}",
                    "-quiet",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            proc.stdin.write(b"hello")
            proc.stdin.flush()

            await asyncio.sleep(1)

            proc.terminate()
            proc.wait(timeout=5)

            assert len(frames) == 0, "Should not receive frames with wrong PSK"

        finally:
            await server.async_stop()
