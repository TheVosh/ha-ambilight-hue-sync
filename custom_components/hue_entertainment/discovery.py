"""mDNS advertisement for the Hue Entertainment Bridge."""

from __future__ import annotations

import logging
import socket

from zeroconf import IPVersion
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

from .const import BRIDGE_MODEL_ID

_LOGGER = logging.getLogger(__name__)


class HueBridgeDiscovery:
    """Advertise the bridge via mDNS (_hue._tcp.local)."""

    def __init__(
        self,
        bridge_id: str,
        host_ip: str,
        port: int,
        async_zeroconf: AsyncZeroconf | None = None,
    ) -> None:
        self._bridge_id = bridge_id
        self._host_ip = host_ip
        self._port = port
        self._external_zeroconf = async_zeroconf
        self._zeroconf: AsyncZeroconf | None = None
        self._service_info: AsyncServiceInfo | None = None

    async def async_start(self) -> None:
        """Register the mDNS service."""
        last6 = self._bridge_id[-6:].upper()
        service_name = f"Philips Hue - {last6}._hue._tcp.local."

        self._service_info = AsyncServiceInfo(
            type_="_hue._tcp.local.",
            name=service_name,
            addresses=[socket.inet_aton(self._host_ip)],
            port=self._port,
            properties={
                "bridgeid": self._bridge_id.upper(),
                "modelid": BRIDGE_MODEL_ID,
            },
            server=f"philips-hue-{last6.lower()}.local.",
        )

        if self._external_zeroconf is not None:
            self._zeroconf = self._external_zeroconf
        else:
            self._zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only)

        await self._zeroconf.async_register_service(self._service_info)
        _LOGGER.info(
            "mDNS: advertising %s at %s:%d",
            service_name,
            self._host_ip,
            self._port,
        )

    async def async_stop(self) -> None:
        """Unregister the mDNS service."""
        if self._zeroconf and self._service_info:
            await self._zeroconf.async_unregister_service(self._service_info)
            # Only close zeroconf if we created it (not HA's shared instance)
            if self._external_zeroconf is None:
                await self._zeroconf.async_close()
            _LOGGER.info("mDNS: service unregistered")
