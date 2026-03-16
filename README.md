# Hue Entertainment Bridge

A Home Assistant integration that emulates a Philips Hue Bridge's **entertainment mode**, allowing a Hue-compatible TV (Ambilight) to control Zigbee lights managed by ZHA.

Your TV thinks it's talking to a real Hue Bridge. Your Zigbee bulbs change colour in sync with what's on screen.

## How it works

```
TV (Ambilight) ──mDNS──> discovers bridge
              ──HTTP───> pairs via Hue API
              ──DTLS───> streams colour frames at 25fps
                              │
                    Hue Entertainment Bridge
                              │
              ──ZHA────> updates Zigbee lights (adaptive rate)
```

The integration:

1. Advertises a Hue Bridge via mDNS (`_hue._tcp.local`)
2. Serves the Hue v1 REST API for pairing and configuration
3. Accepts DTLS-PSK connections for real-time colour streaming
4. Parses HueStream frames (v1 XY and RGB, v2 RGB)
5. Dispatches colour updates to HA lights via an adaptive drain loop that matches Zigbee throughput

## Features

- **Zero-config pairing** — config flow walks you through light selection and TV pairing
- **Adaptive rate control** — round-robin drain loop with per-light coalescing ensures the Zigbee radio is never overloaded
- **Dynamic transitions** — fade duration automatically matches the update interval for smooth colour changes
- **State snapshot/restore** — lights return to their previous state when entertainment mode ends
- **Watchdog** — auto-stops if the TV disconnects or stops sending frames

## Requirements

- Home Assistant 2024.2+
- ZHA integration with colour-capable Zigbee lights
- A Philips TV with Ambilight (or any device that speaks the Hue Entertainment API)
- Port 80 (HTTP) and port 2100 (UDP/DTLS) available on the HA host

## Installation

### HACS (recommended)

1. Add this repository as a custom repository in HACS
2. Search for "Hue Entertainment Bridge" and install
3. Restart Home Assistant

### Manual

1. Copy `custom_components/hue_entertainment/` to your HA config directory
2. Restart Home Assistant

## Setup

1. Go to **Settings > Devices & Services > Add Integration**
2. Search for **Hue Entertainment Bridge**
3. Select the lights you want to use for entertainment mode
4. The pairing wizard will start a temporary bridge — trigger a Hue search on your TV within 60 seconds
5. Once paired, the integration is ready

## Configuration

Use **Options** on the integration to:
- Change the selected lights
- Re-pair your TV (if needed)

Default ports are 80 (HTTP) and 2100 (DTLS). These can be changed in the config entry data if needed.

## Network requirements

- The TV must be able to reach the HA host on ports 80 (TCP) and 2100 (UDP)
- mDNS must work between the TV and HA (if on different VLANs, enable mDNS relay)
- The TV hardcodes port 80 for HTTP — this port must be free on the HA host

## How the adaptive drain loop works

Zigbee radios can only send one command per light at a time (~150-200ms round-trip). At 25fps input with 4 lights, naive dispatch creates a massive backlog.

Instead, each frame writes its colour into a per-light slot (newest wins). A background loop round-robins through the lights, sending one blocking `light.turn_on` call at a time. The transition duration is set dynamically to match the measured interval between updates, so lights fade smoothly instead of stepping.

With 4 lights on a typical Zigbee coordinator, expect ~5-6 commands/second (~1.5 updates per light per second) with smooth fading.

## Tested with

- Philips 55OLED806/12 (Ambilight, v1 XY frames at 25fps)
- SLZB-06Mg24 Zigbee coordinator (TCP, via ZHA)
- Various Zigbee colour bulbs

## Troubleshooting

**TV doesn't find the bridge:**
- Check that port 80 is free on the HA host (`ss -tlnp | grep :80`)
- Verify mDNS works between VLANs (if applicable)
- Check HA logs for mDNS registration

**Lights don't change colour:**
- Verify the lights are ZHA colour-capable entities
- Check that port 2100 (UDP) is reachable from the TV
- Enable debug logging: `logger: logs: custom_components.hue_entertainment: debug`

**Colours are out of sync:**
- This is normal for Zigbee — the adaptive drain loop minimises lag but Zigbee throughput is the limiting factor (~1.5 updates/light/sec with 4 lights)

## License

MIT
