"""Serve the Hue REST API from Home Assistant's own HTTP server.

Hue clients hardcode port 80.  When Home Assistant itself listens on :80
(the "HTTP server port" setting, HA >= 2026.8) a second server cannot bind it,
so the Hue routes are registered as views on ``hass.http`` instead.

Views can be registered but never removed, so they are registered once per
HA instance and forward to whichever :class:`HueAPIServer` is currently
attached (the config flow's temporary pairing server, then the entry's
server).  With nothing attached they answer 503.
"""

from __future__ import annotations

import logging
from ipaddress import ip_address
from typing import TYPE_CHECKING, Any

from aiohttp import web
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.http import KEY_AUTHENTICATED, HomeAssistantView

from .const import DOMAIN, HTTP_MODE_HOMEASSISTANT, HTTP_MODE_STANDALONE
from .hue_api import HueAPIServer

if TYPE_CHECKING:
    from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)

DATA_HTTP_HOST = f"{DOMAIN}_http_host"

# HA's `api` component owns GET /api/config (authenticated).  Hue clients call
# it unauthenticated to validate the bridge, so that one route gets a shim
# instead of a view: see HueHttpHost._install_api_config_shim.
_HA_OWNED = {("GET", "/api/config")}


def is_lan_request(request: web.Request) -> bool:
    """True when the client address is not globally routable (LAN, loopback, link-local, CGNAT).

    HA's forwarded middleware has already replaced ``request.remote`` with the
    real client when a trusted reverse proxy is in front, so a request arriving
    from the internet via such a proxy is correctly seen as non-LAN.
    """
    remote = request.remote
    if not remote:
        return False
    try:
        addr = ip_address(remote)
    except ValueError:
        return False
    return not addr.is_global


@callback
def resolve_use_ha_http(hass: HomeAssistant, mode: str) -> bool:
    """Decide whether the Hue API should live on hass.http for this mode."""
    if mode == HTTP_MODE_HOMEASSISTANT:
        return True
    if mode == HTTP_MODE_STANDALONE:
        return False
    # auto: only when HA already occupies :80 in plain HTTP (what the TV expects)
    return hass.http.server_port == 80 and hass.http.ssl_certificate is None


@callback
def async_get_http_host(hass: HomeAssistant) -> HueHttpHost:
    """Return the per-instance host, registering the views on first use."""
    host: HueHttpHost | None = hass.data.get(DATA_HTTP_HOST)
    if host is None:
        host = HueHttpHost(hass)
        hass.data[DATA_HTTP_HOST] = host
    return host


class HueHttpHost:
    """Routes registered once on hass.http, forwarding to the attached server."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._target: HueAPIServer | None = None
        self._registered = False

    @property
    def target(self) -> HueAPIServer | None:
        return self._target

    @callback
    def attach(self, server: HueAPIServer) -> None:
        """Make ``server`` the recipient of Hue requests."""
        if not self._registered:
            self._register_views()
            self._registered = True
        self._target = server

    @callback
    def detach(self, server: HueAPIServer) -> None:
        """Stop forwarding to ``server`` (no-op if another server took over)."""
        if self._target is server:
            self._target = None

    def _register_views(self) -> None:
        for method, path, attr in HueAPIServer.ROUTES:
            if (method, path) in _HA_OWNED:
                continue
            self._hass.http.register_view(_HueView(self, method, path, attr))
        if not self._install_api_config_shim():
            self._hass.http.register_view(_HueView(self, "GET", "/api/config", "_handle_config"))
        _LOGGER.debug("Hue API views registered on Home Assistant's HTTP server")

    def _install_api_config_shim(self) -> bool:
        """Wrap HA's GET /api/config so unauthenticated calls get the Hue config.

        Authenticated requests (HA frontend, API clients) still reach HA's own
        handler.  Returns False when the route isn't there (api not loaded), in
        which case a normal view is registered instead.
        """
        router = self._hass.http.app.router
        for resource in router.resources():
            if resource.canonical != "/api/config":
                continue
            for route in resource:
                if route.method != "GET":
                    continue
                original: Callable[[web.Request], Any] = route.handler

                async def shim(request: web.Request) -> web.StreamResponse:
                    target = self._target
                    if (
                        target is None
                        or request.get(KEY_AUTHENTICATED, False)
                        or not is_lan_request(request)
                    ):
                        return await original(request)
                    return await target.handle("_handle_config", request)

                route._handler = shim  # noqa: SLF001 — aiohttp has no public setter
                _LOGGER.debug("Shimmed HA's GET /api/config for unauthenticated Hue clients")
                return True
        return False

    async def dispatch(self, attr: str, request: web.Request) -> web.StreamResponse:
        if not is_lan_request(request):
            # Hue clients live on the LAN.  Through an internet-facing reverse
            # proxy these unauthenticated routes would otherwise leak the
            # bridge/network details to anyone.
            raise web.HTTPNotFound
        target = self._target
        if target is None:
            return web.json_response(
                [
                    {
                        "error": {
                            "type": 901,
                            "address": request.path,
                            "description": "bridge not running",
                        }
                    }
                ],
                status=503,
            )
        return await target.handle(attr, request)


class _HueView(HomeAssistantView):
    """One Hue route on hass.http; unauthenticated (Hue has its own username scheme)."""

    requires_auth = False
    cors_allowed = False

    def __init__(self, host: HueHttpHost, method: str, path: str, attr: str) -> None:
        self.url = path
        self.name = f"api:{DOMAIN}:{method.lower()}:{path}"
        self._host = host
        self._attr = attr

        async def handler(request: web.Request, **_kwargs: Any) -> web.StreamResponse:
            return await host.dispatch(attr, request)

        setattr(self, method.lower(), handler)
