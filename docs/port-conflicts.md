# Port conflicts

The TV hardcodes port 80 for HTTP and port 2100 for DTLS. Port 80 cannot be
changed — this is a Philips Hue protocol requirement, not a limitation of this
integration.

## Recommended: run Home Assistant itself on port 80 (HA 2026.8+)

Since Home Assistant 2026.8 the frontend can listen on port 80 itself
(Settings → System → Network → *HTTP server port*). When it does — plain
HTTP, no certificate — this integration detects it and serves the Hue API
**through Home Assistant's own web server** instead of starting a second one,
so there is no port conflict at all. Nothing to configure; the *Hue API
server* option (Configure → Automatic / Standalone / Home Assistant) exists
only to override the detection. The DTLS stream still uses UDP port 2100
directly.

One HA endpoint overlaps with the Hue API: `GET /api/config`. Unauthenticated
requests (what a Hue client sends) receive the Hue bridge config; requests
carrying a Home Assistant token reach Home Assistant's own handler as before.

If Home Assistant stays on 8123, or serves HTTPS on 443, the integration runs
its own server on port 80. When something else (Traefik, Nginx, Pi-hole)
already owns port 80 on the host, pick one of the fallbacks below.

## Fallback A — Secondary IP address (simplest)

Assign a second IP address to your HA host and tell the integration to use
it. The bridge binds exclusively to that IP, leaving port 80 on the primary
IP free for your reverse proxy.

**Step 1 — assign a secondary IP.**

The exact method depends on your setup. For a Linux host:

```bash
ip addr add 192.168.1.200/24 dev eth0
```

For a permanent alias, add it to your network config (e.g.
`/etc/network/interfaces`, Netplan, or your router's DHCP reservations for a
macvlan interface).

Docker / HA OS users can create a
[macvlan network](https://docs.docker.com/network/drivers/macvlan/) and
attach a container with its own IP on the LAN.

**Step 2 — set the Bind IP in the integration.**

Go to **Configure** on the integration card, enter the secondary IP in
**Bind IP address**, and save. HA reloads the integration, and the bridge
advertises and listens only on that IP.

> The TV discovers the bridge via mDNS, which advertises the bind IP
> automatically — no manual IP configuration needed on the TV.

## Fallback B — iptables redirect

If a secondary IP isn't possible, redirect traffic at the firewall level.
This forwards connections arriving from a specific source (the TV's IP) on
port 80 to a high port where the integration listens.

```bash
# Redirect TCP port 80 → 8080 for traffic from the TV
iptables -t nat -A PREROUTING -s <TV_IP> -p tcp --dport 80 -j REDIRECT --to-port 8080
```

Then change the integration's HTTP port to 8080 via the config entry data.
This is more fragile (requires knowing the TV's IP statically) and harder to
maintain than Fallback A.
