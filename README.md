# Hue Entertainment Bridge

[![Tests](https://github.com/83noit/ha-hue-entertainment/actions/workflows/tests.yaml/badge.svg)](https://github.com/83noit/ha-hue-entertainment/actions/workflows/tests.yaml)
[![HACS](https://github.com/83noit/ha-hue-entertainment/actions/workflows/hacs.yaml/badge.svg)](https://github.com/83noit/ha-hue-entertainment/actions/workflows/hacs.yaml)
[![Hassfest](https://github.com/83noit/ha-hue-entertainment/actions/workflows/hassfest.yaml/badge.svg)](https://github.com/83noit/ha-hue-entertainment/actions/workflows/hassfest.yaml)
[![Release](https://img.shields.io/github/v/release/83noit/ha-hue-entertainment?sort=semver)](https://github.com/83noit/ha-hue-entertainment/releases)
[![HACS Default](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/default)

A Home Assistant integration that emulates a Philips Hue Bridge's **entertainment mode** for an Ambilight TV. It can either drive Home Assistant lights (including [ZHA](https://www.home-assistant.io/integrations/zha/)) or forward the stream directly to a real Philips Hue Bridge.

Your TV thinks it's talking to a real Hue Bridge. Your Zigbee bulbs change colour in sync with what's on screen.

## How it works

```mermaid
flowchart TD
    TV["📺 Ambilight TV"]

    subgraph HA["Home Assistant"]
        subgraph Integration["Hue Entertainment Bridge"]
            API["Hue API\n:80 HTTP — standalone,\nor on HA's own server"]
            DTLS["DTLS Server\n:2100 UDP"]
            Engine["Entertainment Engine\nframe parser · throttle · coalesce"]
        end
        ZHA["ZHA"]
    end

    Lights["💡 Zigbee lights"]

    TV -->|"mDNS discovery + pairing"| API
    TV -->|"HueStream frames @ 25 fps"| DTLS
    DTLS --> Engine
    Engine -->|"light.turn_on · adaptive rate"| ZHA
    ZHA -->|Zigbee| Lights
```

The integration:

1. Advertises a Hue Bridge via mDNS (`_hue._tcp.local`)
2. Serves the Hue v1 REST API for pairing and configuration — on its own port-80 server, or
   through Home Assistant's web server when HA itself listens on port 80 (HA 2026.8+)
3. Accepts DTLS-PSK connections for real-time colour streaming
4. Parses HueStream frames (v1 XY and RGB, v2 RGB)
5. Dispatches colour updates to HA lights via an adaptive drain loop that matches Zigbee throughput
6. Also follows the TV's "classic" per-light commands (no streaming), at the same Zigbee-safe pace

## Features

### Output modes

**Mode A — Home Assistant / ZHA lights** keeps the original behaviour: the TV sends
HueStream to this virtual bridge and its adaptive, coalescing drain loop updates the
selected HA light entities. It is the default for existing configuration entries.

**Mode B — Philips Hue Bridge** is recommended when the lights belong to a physical
Hue Bridge. The setup flow pairs with that bridge, lists its Entertainment Areas, and
exposes the selected area's exact channels and 3-D positions to the TV. Incoming
virtual channels are mapped one-to-one to the selected area's native channel IDs and
sent over the Hue Entertainment DTLS stream. This avoids high-frequency
`light.turn_on` calls entirely and supports native streaming rates (up to the chosen
cap, 50 fps by default).

```mermaid
flowchart LR
  TV[Ambilight TV] -->|mDNS + Hue v1| Virtual[HA virtual Hue Bridge]
  TV -->|HueStream / DTLS UDP 2100| Virtual
  Virtual -->|Mode A: coalesced HA services| HA[HA / ZHA lights]
  Virtual -->|Mode B: native Hue Entertainment DTLS| Hue[Physical Hue Bridge]
  Hue --> Area[Selected Entertainment Area]
```

**Mode C — Philips JointSpace** supports newer TVs, including the Philips OLED909,
which no longer provide the legacy Ambilight+Hue pairing screen. Select *Philips
JointSpace Ambilight API*, provide the TV's Digest-auth HTTPS credentials, then select
the physical Hue Entertainment Area. The integration polls
`/6/ambilight/measured` (not `/processed`, which can be all-zero on newer TVs), derives
the TV-edge layout from `/6/ambilight/topology`, and maps each Hue channel to its nearest
measured zone. JointSpace is HTTP polling, so its default is a conservative 10 Hz.

- **Zero-config pairing** — config flow walks you through light selection and TV pairing
- **Adaptive rate control** — round-robin drain loop with per-light coalescing ensures the Zigbee radio is never overloaded
- **Dynamic transitions** — fade duration automatically matches the update interval for smooth colour changes
- **State snapshot/restore** — lights return to their previous state when entertainment mode ends
- **Watchdog** — auto-stops if the TV disconnects or stops sending frames
- **Classic mode fallback** — if the TV drives lights over plain REST instead of streaming, they still follow
- **No port juggling on HA 2026.8+** — when Home Assistant listens on port 80 the Hue API rides on HA's own web server
- **Resilient** — options apply without a restart, unavailable lights are skipped, bind failures are reported cleanly
- **Diagnostics** — downloadable diagnostics (credentials redacted) and a bridge device grouping the entities

## Requirements

- Home Assistant 2024.11+
- [ZHA](https://www.home-assistant.io/integrations/zha/) with colour-capable Zigbee lights
- A Philips TV with Ambilight (or any device that speaks the Hue Entertainment API)
- Port 80 (TCP) and port 2100 (UDP) reachable on the HA host from the TV — the TV hardcodes both

## Installation

### HACS (recommended)

1. Open HACS and go to **Integrations**
2. Search for **Hue Entertainment Bridge** and install
3. Restart Home Assistant

### Manual

1. Copy `custom_components/hue_entertainment/` into your HA config directory
2. Restart Home Assistant

## Setup

1. Go to **Settings > Devices & Services > Add Integration**
2. Search for **Hue Entertainment Bridge**
3. Choose **Home Assistant / ZHA lights** and select light entities, or choose
   **Philips Hue Bridge**, enter its local address, press its link button, and select
   an Entertainment Area
4. The TV pairing wizard starts a 60-second window — trigger a Hue bridge search on your TV
5. Once paired, the integration is ready

### Modern Philips TVs: JointSpace and native Hue Entertainment

Some newer Philips TVs no longer offer the old **Ambilight+Hue** pairing UI. Choose
**Philips JointSpace Ambilight API** as the input and **Philips Hue Bridge** as the
output instead. The integration reads `/ambilight/measured`, maps the TV edge zones,
and sends native DTLS Hue Entertainment frames to a physical Entertainment Area.

Requirements are mode-specific: JointSpace needs a reachable TV with JointSpace API
support, its API version and credentials. Many TVs use HTTPS/Digest authentication;
disable certificate verification only when the TV uses a self-signed certificate that
Home Assistant cannot verify. JointSpace mode does not need the virtual Hue HTTP or
DTLS ports.

Create the Entertainment Area in the Hue app first. Setup validates the TV, selects
the Hue Bridge (using official Home Assistant Hue metadata when available), performs a
one-time physical link-button authorization, selects the Area, maps its channels, and
saves. This authorization creates separate Entertainment credentials and a client key;
they are never shown in the UI. Hue authorization can be deferred and completed later.

Channel mappings are `auto`, `top`, `bottom`, `left_top`, `left_middle`,
`left_bottom`, `right_top`, `right_middle`, or `right_bottom`. Auto uses Hue channel
positions; a manual choice overrides it. Mappings can be changed later without
re-pairing.

## Configuration

Open **Configure** on the integration card to:

- **Change lights** — update which entities are in the entertainment area
- **Pair TV** — open a new 60-second pairing window
- **Bind IP address** — bind the bridge to a specific IP address (see [Port conflicts](#port-conflicts) below)
- **Hue API server** — *Automatic* (default), *Standalone server on port 80*, or *Home Assistant's
  web server*; see [Port conflicts](#port-conflicts) for when each applies

Changes apply immediately — no restart needed. **Download diagnostics** on the same card gives a
redacted snapshot (paired clients, engine counters, options) to attach to bug reports.

For JointSpace with a physical Hue Bridge, **Configure** is a management menu for
**TV / JointSpace**, **Philips Hue Bridge**, **Entertainment Area**, **Ambilight
mapping**, **Performance**, and **Re-authorize Hue Bridge**. Opening Configure never
re-pairs the bridge; reauthorization is explicit.

## Troubleshooting

Enable sanitized debug logs when investigating a problem:

```yaml
logger:
  default: warning
  logs:
    custom_components.hue_entertainment: debug
    custom_components.hue_entertainment.jointspace: debug
```

For JointSpace failures, check TV credentials, network reachability, API version, TLS
verification, and topology support. For Hue failures, press the physical link button,
verify that an Entertainment Area exists, and retry authorization. Never include TV
credentials, Hue application keys, or client keys in bug reports.

## Port conflicts

### Recommended: run Home Assistant itself on port 80 (HA 2026.8+)

Since Home Assistant 2026.8 the frontend can listen on port 80 itself (Settings → System →
Network → *HTTP server port*). When it does — plain HTTP, no certificate — this integration
detects it and serves the Hue API **through Home Assistant's own web server** instead of
starting a second one, so there is no port conflict at all. Nothing to configure; the
*Hue API server* option (Configure → Automatic / Standalone / Home Assistant) exists only to
override the detection. The DTLS stream still uses UDP port 2100 directly.

One HA endpoint overlaps with the Hue API: `GET /api/config`. Unauthenticated requests
(what a Hue client sends) receive the Hue bridge config; requests carrying a Home Assistant
token reach Home Assistant's own handler as before.

If Home Assistant stays on 8123, or serves HTTPS on 443, the integration runs its own server
on port 80. When something else already owns port 80 on the host, pick one of the fallbacks below.

The TV hardcodes port 80 for HTTP and port 2100 for DTLS. Port 80 cannot be changed — this is a Philips Hue protocol requirement, not a limitation of this integration. If something else (Traefik, Nginx, Pi-hole) already occupies port 80 on your HA host, you have two options.

### Fallback A — Secondary IP address (simplest)

Assign a second IP address to your HA host and tell the integration to use it. The bridge binds exclusively to that IP, leaving port 80 on the primary IP free for your reverse proxy.

**Step 1 — assign a secondary IP.**

The exact method depends on your setup. For a Linux host:

```bash
ip addr add 192.168.1.200/24 dev eth0
```

For a permanent alias, add it to your network config (e.g. `/etc/network/interfaces`, Netplan, or your router's DHCP reservations for a macvlan interface).

Docker / HA OS users can create a [macvlan network](https://docs.docker.com/network/drivers/macvlan/) and attach a container with its own IP on the LAN.

**Step 2 — set the Bind IP in the integration.**

Go to **Configure** on the integration card, enter the secondary IP in **Bind IP address**, and save. HA will reload the integration, and the bridge will advertise and listen only on that IP.

> The TV discovers the bridge via mDNS, which will advertise the bind IP automatically — no manual IP configuration needed on the TV.

### Fallback B — iptables redirect

If a secondary IP isn't possible, redirect traffic at the firewall level. This forwards connections arriving on a specific source (e.g. the TV's IP) from port 80 to a high port where the integration listens.

```bash
# Redirect TCP port 80 → 8080 for traffic from the TV
iptables -t nat -A PREROUTING -s <TV_IP> -p tcp --dport 80 -j REDIRECT --to-port 8080
```

Then change the integration's HTTP port to 8080 via the config entry data. This is more fragile (requires knowing the TV's IP statically) and harder to maintain than Fallback A.

## Network requirements

- The TV must reach the HA host on **port 80 (TCP)** and **port 2100 (UDP)**
- If the TV and HA are on different VLANs, **mDNS relay** must be enabled (e.g. Avahi, Unifi mDNS, or a multicast relay) — without it, the TV cannot discover the bridge
- The TV ignores the port advertised by mDNS and always connects to port 80; only the IP address from mDNS is used

## How the adaptive drain loop works

Zigbee radios handle one command per light at a time (~150–200 ms round-trip). At 25 fps input with 4 lights, naive dispatch creates a backlog that grows without bound.

Instead, each incoming frame writes its colour into a per-light slot (newest wins, older frames discarded). A background loop round-robins through the lights, sending one `light.turn_on` call at a time. The transition duration is set dynamically to match the measured inter-update interval, so lights fade smoothly rather than stepping.

With 4 lights on a typical Zigbee coordinator, expect ~5–6 commands/second (~1.5 updates/light/second) with continuous smooth fading.

## Tested with

- Philips 55OLED806/12 (Ambilight, v1 XY frames at 25 fps)
- Home Assistant OS 2026.8 with the frontend on port 80 (Hue API on HA's server) and on 8123 (standalone)
- SLZB-06Mg24 Zigbee coordinator (TCP, via ZHA)
- Various Zigbee colour bulbs

## Troubleshooting

**TV doesn't find the bridge**
- If the integration card says *Failed to set up* / *Retrying*, port 80 is taken on the HA host —
  see [Port conflicts](#port-conflicts). `curl http://<HA_IP>/api/nouser/config` must return the bridge JSON.
- Verify mDNS works across VLANs if the TV is on a separate network segment
- Check HA logs for mDNS registration errors

**TV says the bulbs are connected but they barely follow, or lag a lot**
- The TV has fallen into "classic" mode (per-light REST commands, ~4 updates/s shared by all bulbs,
  no DTLS stream). This happens when the bridge disappears while the TV is on — e.g. an HA restart.
  Toggling Ambilight+hue does not fix it; **restart the TV** and it will stream again (allow 2–3
  minutes for it to rediscover the bridge).

**Lights don't change colour**
- Confirm the selected lights are ZHA colour-capable entities (`color_mode: xy` or `hs`)
- Check port 2100 (UDP) is reachable from the TV: `nc -u <HA_IP> 2100`
- Enable debug logging:
  ```yaml
  logger:
    logs:
      custom_components.hue_entertainment: debug
  ```

**Colours are behind / out of sync**
- This is normal for Zigbee — the adaptive drain loop minimises lag but Zigbee throughput is the hard limit (~1.5 updates/light/second with 4 lights)
- Reducing the number of lights in the entertainment area increases the per-light update rate

## License

MIT
