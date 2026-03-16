"""Hue v1 REST API server for entertainment mode pairing and streaming."""

from __future__ import annotations

import datetime
import logging
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable

from aiohttp import web

from .const import (
    BRIDGE_API_VERSION,
    BRIDGE_MODEL_ID,
    BRIDGE_SW_VERSION,
)
from .user_store import UserStore

_LOGGER = logging.getLogger(__name__)


@web.middleware
async def _request_logger(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Log every request at DEBUG — shows exactly which endpoints the TV hits."""
    resp = await handler(request)
    _LOGGER.debug("%s %s -> %d", request.method, request.path, resp.status)
    return resp


class HueAPIServer:
    """Serve the Hue v1 API endpoints needed for TV entertainment mode."""

    LINK_BUTTON_TIMEOUT = 30.0  # seconds, same as real Hue bridge

    def __init__(
        self,
        bridge_id: str,
        mac: str,
        host_ip: str,
        http_port: int,
        channel_count: int,
        light_entities: list[str],
        user_store: UserStore | None = None,
    ) -> None:
        self._bridge_id = bridge_id
        self._mac = mac
        self._host_ip = host_ip
        self._http_port = http_port
        self._channel_count = channel_count
        self._light_entities = light_entities

        self._user_store = user_store if user_store is not None else UserStore()

        # Link button state — timed window, just like the real bridge
        self._link_button_expires = 0.0

        # Entertainment streaming state
        self._entertainment_active = False
        self._entertainment_owner: str | None = None

        # Callbacks
        self._on_entertainment_start: Callable[[str], Awaitable[None]] | None = None
        self._on_entertainment_stop: Callable[[], Awaitable[None]] | None = None

        self._http_runner: web.AppRunner | None = None

    def set_entertainment_callbacks(
        self,
        on_start: Callable[[str], Awaitable[None]],
        on_stop: Callable[[], Awaitable[None]],
    ) -> None:
        """Set callbacks for entertainment start/stop."""
        self._on_entertainment_start = on_start
        self._on_entertainment_stop = on_stop

    @property
    def entertainment_active(self) -> bool:
        """Whether entertainment streaming is currently active."""
        return self._entertainment_active

    @property
    def entertainment_owner(self) -> str | None:
        """Username of the client that activated entertainment, or None."""
        return self._entertainment_owner

    def clear_entertainment(self) -> None:
        """Reset entertainment state (e.g. from watchdog timeout)."""
        self._entertainment_active = False
        self._entertainment_owner = None

    @property
    def _link_button_active(self) -> bool:
        return time.monotonic() < self._link_button_expires

    def set_link_button(self, active: bool) -> None:
        """Enable the link button for 30 seconds, or disable it immediately."""
        if active:
            self._link_button_expires = time.monotonic() + self.LINK_BUTTON_TIMEOUT
        else:
            self._link_button_expires = 0.0

    def get_user_psk(self, username: str) -> str | None:
        """Get the PSK (clientkey) for a username."""
        return self._user_store.get_psk(username)

    async def async_start(self) -> None:
        """Start the HTTP server."""
        app = web.Application(middlewares=[_request_logger])
        self._register_routes(app)

        self._http_runner = web.AppRunner(app)
        await self._http_runner.setup()
        http_site = web.TCPSite(self._http_runner, "0.0.0.0", self._http_port)
        await http_site.start()
        _LOGGER.info("Hue API HTTP server on port %d", self._http_port)

    async def async_stop(self) -> None:
        """Stop the HTTP server."""
        if self._http_runner:
            await self._http_runner.cleanup()
        _LOGGER.info("Hue API server stopped")

    def _register_routes(self, app: web.Application) -> None:
        """Register all API routes."""
        # UPnP description
        app.router.add_get("/description.xml", self._handle_description_xml)

        # Unauthenticated config (two paths the TV may try)
        app.router.add_get("/api/nouser/config", self._handle_config)
        app.router.add_get("/api/config", self._handle_config)
        app.router.add_post("/api", self._handle_create_user)
        app.router.add_get("/api/{username}", self._handle_full_datastore)
        app.router.add_get("/api/{username}/config", self._handle_config_auth)
        app.router.add_get("/api/{username}/capabilities", self._handle_capabilities)
        # V1 lights and groups (TV reads these to find colour bulbs + entertainment areas)
        app.router.add_get("/api/{username}/lights", self._handle_v1_lights)
        app.router.add_get("/api/{username}/lights/{light_id}", self._handle_v1_light_by_id)
        app.router.add_put("/api/{username}/lights/{light_id}/state", self._handle_v1_light_state)
        app.router.add_get("/api/{username}/groups", self._handle_v1_groups)
        app.router.add_get("/api/{username}/groups/{group_id}", self._handle_v1_group_by_id)
        app.router.add_put("/api/{username}/groups/{group_id}", self._handle_v1_group_put)
        app.router.add_put("/api/{username}/groups/{group_id}/stream", self._handle_v1_stream)
        # Catch-alls for unimplemented v1 resources (avoids 404s / 405s)
        app.router.add_get("/api/{username}/{resource}", self._handle_v1_catchall)
        app.router.add_get("/api/{username}/{resource}/{id}", self._handle_v1_catchall)
        app.router.add_put("/api/{username}/{resource}/{id}", self._handle_v1_put_catchall)
        app.router.add_put("/api/{username}/{resource}/{id}/{param}", self._handle_v1_put_catchall)
        app.router.add_post("/api/{username}/{resource}", self._handle_v1_post_catchall)
        app.router.add_delete("/api/{username}/{resource}/{id}", self._handle_v1_delete_catchall)

    # ---------------------------------------------------------------------------
    # Data builders
    # ---------------------------------------------------------------------------

    def _build_v1_config(self) -> dict:
        return {
            "name": "Hue Entertainment Bridge",
            "datastoreversion": "163",
            "swversion": BRIDGE_SW_VERSION,
            "apiversion": BRIDGE_API_VERSION,
            "mac": self._mac,
            "bridgeid": self._bridge_id.upper(),
            "factorynew": False,
            "replacesbridgeid": None,
            "modelid": BRIDGE_MODEL_ID,
            "starterkitid": "",
            "ipaddress": self._host_ip,
            "dhcp": True,
            "netmask": "255.255.255.0",
            "gateway": self._host_ip.rsplit(".", 1)[0] + ".1",
            "UTC": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S"),
            "localtime": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "timezone": "Europe/London",
            "zigbeechannel": 25,
            "linkbutton": self._link_button_active,
            "portalservices": False,
            "portalstate": {
                "signedon": False,
                "incoming": False,
                "outgoing": False,
                "communication": "disconnected",
            },
            "internetservices": {
                "internet": "disconnected",
                "remoteaccess": "disconnected",
                "swupdate": "disconnected",
                "time": "disconnected",
            },
            "swupdate2": {
                "checkforupdate": False,
                "state": "noupdates",
                "autoinstall": {"on": False, "updatetime": "T14:00:00"},
            },
            "backup": {"errorcode": 0, "status": "idle"},
            "whitelist": {
                u: {
                    "create date": "2026-01-01T00:00:00",
                    "last use date": "2026-01-01T00:00:00",
                    "name": info.get("devicetype", "unknown"),
                }
                for u, info in self._user_store.users.items()
            },
        }

    def _build_v1_lights(self) -> dict:
        lights = {}
        for i, entity_id in enumerate(self._light_entities, 1):
            h = uuid.uuid5(uuid.NAMESPACE_DNS, f"uniqueid-{entity_id}").hex
            uniqueid = ":".join(h[j : j + 2] for j in range(0, 16, 2)) + "-0b"
            lights[str(i)] = {
                "state": {
                    "on": True,
                    "bri": 254,
                    "hue": 0,
                    "sat": 0,
                    "xy": [0.0, 0.0],
                    "ct": 500,
                    "alert": "select",
                    "effect": "none",
                    "colormode": "xy",
                    "mode": "homeautomation",
                    "reachable": True,
                },
                "type": "Extended color light",
                "name": entity_id,
                "modelid": "LCT015",
                "manufacturername": "Signify Netherlands B.V.",
                "productname": "Hue color lamp",
                "capabilities": {
                    "certified": True,
                    "control": {
                        "mindimlevel": 200,
                        "maxlumen": 800,
                        "colorgamuttype": "C",
                        "colorgamut": [
                            [0.6915, 0.3083],
                            [0.17, 0.7],
                            [0.1532, 0.0475],
                        ],
                        "ct": {"min": 153, "max": 500},
                    },
                    "streaming": {"renderer": True, "proxy": False},
                },
                "uniqueid": uniqueid,
                "swversion": "1.104.2",
            }
        return lights

    def _build_v1_groups(self) -> dict:
        if not self._light_entities:
            return {}
        light_ids = [str(i) for i in range(1, len(self._light_entities) + 1)]
        locations = {}
        for i in range(1, len(self._light_entities) + 1):
            x = -1.0 + (2.0 * (i - 1) / max(len(self._light_entities) - 1, 1))
            locations[str(i)] = [round(x, 4), 1.0, 0.0]
        return {
            "1": {
                "name": "TV Entertainment",
                "lights": light_ids,
                "sensors": [],
                "type": "Entertainment",
                "class": "TV",
                "stream": {
                    "proxymode": "auto",
                    "proxynode": f"/lights/{light_ids[0]}",
                    "active": self.entertainment_active,
                    "owner": self.entertainment_owner,
                },
                "locations": locations,
                "state": {"any_on": True, "all_on": True},
                "action": {
                    "on": True,
                    "bri": 254,
                    "hue": 0,
                    "sat": 0,
                    "effect": "none",
                    "xy": [0.0, 0.0],
                    "ct": 500,
                    "alert": "none",
                    "colormode": "xy",
                },
            }
        }

    # ---------------------------------------------------------------------------
    # Request handlers
    # ---------------------------------------------------------------------------

    async def _handle_description_xml(self, request: web.Request) -> web.Response:
        """GET /description.xml — UPnP device description."""
        serial = self._mac.replace(":", "").lower()
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<root xmlns="urn:schemas-upnp-org:device-1-0">\n'
            "<specVersion><major>1</major><minor>0</minor></specVersion>\n"
            f"<URLBase>http://{self._host_ip}:{self._http_port}/</URLBase>\n"
            "<device>\n"
            "<deviceType>urn:schemas-upnp-org:device:Basic:1</deviceType>\n"
            f"<friendlyName>Philips hue ({self._host_ip})</friendlyName>\n"
            "<manufacturer>Signify</manufacturer>\n"
            "<manufacturerURL>http://www.meethue.com</manufacturerURL>\n"
            "<modelDescription>Philips hue Personal Wireless Lighting</modelDescription>\n"
            "<modelName>Philips hue bridge 2015</modelName>\n"
            "<modelNumber>BSB002</modelNumber>\n"
            "<modelURL>http://www.meethue.com</modelURL>\n"
            f"<serialNumber>{serial}</serialNumber>\n"
            f"<UDN>uuid:2f402f80-da50-11e1-9b23-{serial}</UDN>\n"
            "<presentationURL>index.html</presentationURL>\n"
            "</device>\n"
            "</root>"
        )
        return web.Response(text=xml, content_type="text/xml")

    def _check_username(self, request: web.Request) -> str | None:
        """Return username if valid, or None if unknown."""
        username = request.match_info.get("username", "")
        if self._user_store.get_psk(username) is not None:
            return username
        return None

    @staticmethod
    def _unauthorized_response() -> web.Response:
        """Return a Hue-style 'unauthorized user' error response."""
        return web.json_response(
            [{"error": {"type": 1, "address": "/", "description": "unauthorized user"}}]
        )

    @staticmethod
    async def _safe_json(request: web.Request) -> dict | None:
        """Parse JSON body, returning None on malformed input."""
        try:
            return await request.json()
        except (ValueError, UnicodeDecodeError):
            return None

    async def _set_entertainment_active(self, active: bool, username: str = "") -> None:
        """Toggle entertainment streaming state and fire callbacks."""
        if active:
            self._entertainment_active = True
            self._entertainment_owner = username
            _LOGGER.info("Entertainment streaming started by %s", username)
            if self._on_entertainment_start:
                await self._on_entertainment_start(username)
        else:
            self._entertainment_active = False
            self._entertainment_owner = None
            _LOGGER.info("Entertainment streaming stopped")
            if self._on_entertainment_stop:
                await self._on_entertainment_stop()

    async def _handle_full_datastore(self, request: web.Request) -> web.Response:
        """GET /api/{username} — v1 full datastore."""
        username = self._check_username(request)
        if username is None:
            return self._unauthorized_response()
        return web.json_response(
            {
                "lights": self._build_v1_lights(),
                "groups": self._build_v1_groups(),
                "config": self._build_v1_config(),
                "schedules": {},
                "scenes": {},
                "rules": {},
                "sensors": {},
                "resourcelinks": {},
            }
        )

    async def _handle_config(self, request: web.Request) -> web.Response:
        """GET /api/nouser/config and /api/config — basic bridge info for discovery."""
        return web.json_response(self._build_v1_config())

    async def _handle_config_auth(self, request: web.Request) -> web.Response:
        """GET /api/{username}/config — authenticated config."""
        if self._check_username(request) is None:
            return self._unauthorized_response()
        return web.json_response(self._build_v1_config())

    async def _handle_capabilities(self, request: web.Request) -> web.Response:
        """GET /api/{username}/capabilities."""
        if self._check_username(request) is None:
            return self._unauthorized_response()
        return web.json_response(
            {
                "lights": {"available": 50, "total": 50},
                "sensors": {"available": 250, "total": 250},
                "groups": {"available": 64, "total": 64},
                "scenes": {"available": 200, "total": 200},
                "schedules": {"available": 100, "total": 100},
                "rules": {"available": 250, "total": 250},
                "resourcelinks": {"available": 64, "total": 64},
                "streaming": {
                    "available": 1,
                    "total": 1,
                    "channels": self._channel_count,
                },
                "timezones": {"values": []},
            }
        )

    async def _handle_create_user(self, request: web.Request) -> web.Response:
        """POST /api — pair a new client (link button press).

        Idempotent: if a user with the same devicetype already exists, return
        its credentials instead of creating a duplicate.  This handles TVs that
        fire a burst of POST /api requests — every response carries the same
        username/clientkey so the TV gets consistent credentials regardless of
        which response it uses.
        """
        body = await self._safe_json(request)
        if body is None:
            return web.json_response(
                [
                    {
                        "error": {
                            "type": 2,
                            "address": "/",
                            "description": "body contains invalid json",
                        }
                    }
                ]
            )
        device_type = body.get("devicetype", "unknown")
        generate_clientkey = body.get("generateclientkey", False)

        if not self._link_button_active:
            return web.json_response(
                [{"error": {"type": 101, "address": "", "description": "link button not pressed"}}]
            )

        # Return existing credentials if this devicetype already paired
        existing = self._user_store.get_by_devicetype(device_type)
        if existing is not None:
            username, clientkey = existing
        else:
            username = uuid.uuid4().hex[:32]
            clientkey = secrets.token_hex(16)
            self._user_store.add(username, clientkey, device_type)
            await self._user_store.async_save()
            _LOGGER.info("Paired new client: %s (%s)", device_type, username)

        result = {"success": {"username": username}}
        if generate_clientkey:
            result["success"]["clientkey"] = clientkey

        return web.json_response([result])

    async def _handle_v1_lights(self, request: web.Request) -> web.Response:
        """GET /api/{username}/lights."""
        if self._check_username(request) is None:
            return self._unauthorized_response()
        return web.json_response(self._build_v1_lights())

    async def _handle_v1_groups(self, request: web.Request) -> web.Response:
        """GET /api/{username}/groups."""
        if self._check_username(request) is None:
            return self._unauthorized_response()
        return web.json_response(self._build_v1_groups())

    async def _handle_v1_light_state(self, request: web.Request) -> web.Response:
        """PUT /api/{username}/lights/{light_id}/state — accept light state changes."""
        if self._check_username(request) is None:
            return self._unauthorized_response()
        light_id = request.match_info["light_id"]
        body = await self._safe_json(request)
        if body is None:
            return web.json_response(
                [
                    {
                        "error": {
                            "type": 2,
                            "address": "/",
                            "description": "body contains invalid json",
                        }
                    }
                ]
            )
        _LOGGER.debug("Light %s state update: %s", light_id, body)
        result = [{"success": {f"/lights/{light_id}/state/{k}": v}} for k, v in body.items()]
        return web.json_response(result)

    async def _handle_v1_light_by_id(self, request: web.Request) -> web.Response:
        """GET /api/{username}/lights/{light_id} — single light object."""
        if self._check_username(request) is None:
            return self._unauthorized_response()
        light_id = request.match_info["light_id"]
        lights = self._build_v1_lights()
        if light_id not in lights:
            return web.json_response(
                [
                    {
                        "error": {
                            "type": 3,
                            "address": f"/lights/{light_id}",
                            "description": f"resource, /lights/{light_id}, not available",
                        }
                    }
                ],
                status=404,
            )
        return web.json_response(lights[light_id])

    async def _handle_v1_group_by_id(self, request: web.Request) -> web.Response:
        """GET /api/{username}/groups/{group_id} — single group object."""
        if self._check_username(request) is None:
            return self._unauthorized_response()
        group_id = request.match_info["group_id"]
        groups = self._build_v1_groups()
        if group_id not in groups:
            return web.json_response(
                [
                    {
                        "error": {
                            "type": 3,
                            "address": f"/groups/{group_id}",
                            "description": f"resource, /groups/{group_id}, not available",
                        }
                    }
                ],
                status=404,
            )
        return web.json_response(groups[group_id])

    async def _handle_v1_group_put(self, request: web.Request) -> web.Response:
        """PUT /api/{username}/groups/{group_id} — update group (incl. stream start/stop)."""
        if self._check_username(request) is None:
            return self._unauthorized_response()
        group_id = request.match_info["group_id"]
        username = request.match_info["username"]
        body = await self._safe_json(request)
        if body is None:
            return web.json_response(
                [
                    {
                        "error": {
                            "type": 2,
                            "address": "/",
                            "description": "body contains invalid json",
                        }
                    }
                ]
            )

        # Handle stream activation (TV sends {"stream": {"active": true}} here)
        stream = body.get("stream")
        if stream is not None:
            await self._set_entertainment_active(stream.get("active", False), username)

        # Echo back each field as success
        result = []
        for k, v in body.items():
            if isinstance(v, dict):
                for sk, sv in v.items():
                    result.append({"success": {f"/groups/{group_id}/{k}/{sk}": sv}})
            else:
                result.append({"success": {f"/groups/{group_id}/{k}": v}})
        return web.json_response(result)

    async def _handle_v1_stream(self, request: web.Request) -> web.Response:
        """PUT /api/{username}/groups/{group_id}/stream — v1 entertainment start/stop."""
        if self._check_username(request) is None:
            return self._unauthorized_response()
        body = await self._safe_json(request)
        if body is None:
            return web.json_response(
                [
                    {
                        "error": {
                            "type": 2,
                            "address": "/",
                            "description": "body contains invalid json",
                        }
                    }
                ]
            )
        # TV sends {"stream": {"active": true}}, but accept flat {"active": true} too
        stream = body.get("stream", body)
        active = stream.get("active", False)
        username = request.match_info["username"]

        await self._set_entertainment_active(active, username)

        return web.json_response([{"success": {"/groups/1/stream/active": active}}])

    async def _handle_v1_catchall(self, request: web.Request) -> web.Response:
        """Catch-all GET for unimplemented v1 resources — avoids 404s."""
        return web.json_response({})

    async def _handle_v1_put_catchall(self, request: web.Request) -> web.Response:
        """Catch-all PUT for unimplemented v1 resources — echo success."""
        resource = request.match_info.get("resource", "")
        rid = request.match_info.get("id", "")
        param = request.match_info.get("param", "")
        body = await self._safe_json(request)
        if body is None:
            return web.json_response(
                [
                    {
                        "error": {
                            "type": 2,
                            "address": "/",
                            "description": "body contains invalid json",
                        }
                    }
                ]
            )
        prefix = f"/{resource}/{rid}"
        if param:
            prefix += f"/{param}"
        result = [{"success": {f"{prefix}/{k}": v}} for k, v in body.items()]
        return web.json_response(result)

    async def _handle_v1_post_catchall(self, request: web.Request) -> web.Response:
        """Catch-all POST for unimplemented v1 resources."""
        resource = request.match_info.get("resource", "")
        if resource == "lights":
            return web.json_response([{"success": {"/lights": "Searching for new devices"}}])
        return web.json_response([{"success": {"id": "0"}}])

    async def _handle_v1_delete_catchall(self, request: web.Request) -> web.Response:
        """Catch-all DELETE for unimplemented v1 resources."""
        resource = request.match_info.get("resource", "")
        rid = request.match_info.get("id", "")
        return web.json_response([{"success": f"/{resource}/{rid} deleted"}])
