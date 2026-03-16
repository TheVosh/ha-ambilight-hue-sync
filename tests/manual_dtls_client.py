"""Test DTLS-PSK client — simulates a Hue TV connecting to the bridge.

Usage:
    python tests/test_dtls_client.py [host] [port]

This pairs with the API, starts entertainment mode, connects via DTLS,
and sends a few HueStream frames.
"""

from __future__ import annotations

import json
import ssl as stdlib_ssl
import struct
import sys
import time
import urllib.request

# Defaults
HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
API_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 443
DTLS_PORT = 2100


def api_request(
    method: str, path: str, body: dict | None = None, headers: dict | None = None
) -> dict:
    """Make an HTTP request to the Hue API."""
    url = f"https://{HOST}:{API_PORT}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    req.add_header("Content-Type", "application/json")

    # Disable cert verification (self-signed)
    ctx = stdlib_ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = stdlib_ssl.CERT_NONE

    with urllib.request.urlopen(req, context=ctx) as resp:
        return json.loads(resp.read())


def build_huestream_frame(config_id: str, channels: list[tuple[int, int, int, int]]) -> bytes:
    """Build a HueStream v2 frame.

    channels: list of (channel_id, r, g, b) with 16-bit values.
    """
    header = bytearray()
    header.extend(b"HueStream")  # 9 bytes
    header.extend(b"\x02\x00")  # API version 2.0
    header.append(0x01)  # sequence
    header.extend(b"\x00\x00")  # reserved
    header.append(0x00)  # color space: RGB
    header.append(0x00)  # reserved
    # Entertainment config UUID as 36-byte ASCII string
    header.extend(config_id.encode("ascii")[:36].ljust(36, b"\x00"))

    # Per-channel data
    for channel_id, r, g, b in channels:
        header.append(channel_id)
        header.extend(struct.pack(">HHH", r, g, b))

    return bytes(header)


def main():
    print("=== DTLS-PSK Test Client ===")
    print(f"Target: {HOST}:{API_PORT} (API), {HOST}:{DTLS_PORT} (DTLS)")
    print()

    # Step 1: Pair with the bridge
    print("[1] Pairing with bridge...")
    result = api_request(
        "POST",
        "/api",
        {
            "devicetype": "test_client#test",
            "generateclientkey": True,
        },
    )
    print(f"    Response: {result}")

    if isinstance(result, list):
        result = result[0]
    success = result.get("success", {})
    username = success["username"]
    clientkey = success["clientkey"]
    print(f"    Username: {username}")
    print(f"    Clientkey: {clientkey}")
    print()

    # Step 2: Get entertainment configuration
    print("[2] Getting entertainment configuration...")
    headers = {"hue-application-key": username}
    config = api_request("GET", "/clip/v2/resource/entertainment_configuration", headers=headers)
    print(f"    Configs: {json.dumps(config, indent=2)[:500]}")

    config_id = config["data"][0]["id"]
    channels = config["data"][0]["channels"]
    print(f"    Config ID: {config_id}")
    print(f"    Channels: {len(channels)}")
    print()

    # Step 3: Start entertainment mode
    print("[3] Starting entertainment mode...")
    result = api_request(
        "PUT",
        f"/clip/v2/resource/entertainment_configuration/{config_id}",
        {"action": "start"},
        headers=headers,
    )
    print(f"    Response: {result}")
    print()

    # Step 4: Connect via DTLS
    print("[4] Connecting via DTLS-PSK...")
    print(f"    PSK identity: {username}")
    print(f"    PSK key: {clientkey}")

    # Use openssl s_client for the DTLS connection test
    import subprocess

    proc = subprocess.Popen(
        [
            "openssl",
            "s_client",
            "-dtls",
            "-psk",
            clientkey,
            "-psk_identity",
            username,
            "-connect",
            f"{HOST}:{DTLS_PORT}",
            "-quiet",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    print("    DTLS connected, sending frames...")
    print()

    # Step 5: Send HueStream frames
    for i in range(30):
        # Cycle through colours
        phase = (i * 20) % 360
        r = int(65535 * (0.5 + 0.5 * (phase < 120 or phase > 300)))
        g = int(65535 * (0.5 + 0.5 * (60 < phase < 240)))
        b = int(65535 * (0.5 + 0.5 * (180 < phase < 360)))

        channel_data = [(ch["channel_id"], r, g, b) for ch in channels]
        frame = build_huestream_frame(config_id, channel_data)
        proc.stdin.write(frame)
        proc.stdin.flush()

        print(f"    Frame {i + 1:3d}: R={r >> 8:3d} G={g >> 8:3d} B={b >> 8:3d}")
        time.sleep(0.05)  # ~20fps

    print()
    print("[5] Stopping entertainment mode...")
    result = api_request(
        "PUT",
        f"/clip/v2/resource/entertainment_configuration/{config_id}",
        {"action": "stop"},
        headers=headers,
    )
    print(f"    Response: {result}")

    proc.terminate()
    print()
    print("=== Done ===")


if __name__ == "__main__":
    main()
