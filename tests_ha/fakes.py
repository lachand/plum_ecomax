"""A PlumDevice that never touches the network.

It subclasses the real driver (so the bundled parameter map, slugs and ids are
real) and replaces only the I/O entry points. `link_up = False` simulates an
unreachable boiler; `values` overrides what individual slugs read back as.
"""

from __future__ import annotations

from typing import Any

from custom_components.plum_ecomax.const import DOMAIN
from custom_components.plum_ecomax.plum_device import PlumDevice

SERIAL = "TESTSERIAL123"
IP = "192.0.2.10"
ENTRY_DATA = {
    "ip_address": IP,
    "port": 8899,
    "username": "admin",
    "password": "0000",
    "active_circuits": ["2"],
    "update_interval": 30,
}

__all__ = ["DOMAIN", "ENTRY_DATA", "IP", "SERIAL", "FakePlumDevice"]


class FakePlumDevice(PlumDevice):
    link_up = True
    values: dict[str, Any] = {}  # per-slug overrides
    instances: list[FakePlumDevice] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.writes: list[tuple[str, Any]] = []
        self.closed = False
        FakePlumDevice.instances.append(self)

    def _read(self, slug: str) -> Any:
        if slug in self.values:
            return self.values[slug]
        if slug == "uid":
            return SERIAL
        return 1

    async def get_values(self, slugs: list, retries: int = 2, batch_size: int = 16) -> dict:
        if not FakePlumDevice.link_up:
            self.consecutive_failures += 1
            return {}
        self.consecutive_failures = 0
        return {s: self._read(s) for s in slugs if s in self.params_map}

    async def get_value(self, slug: str, retries: int = 3) -> Any:
        if not FakePlumDevice.link_up or slug not in self.params_map:
            return None
        return self._read(slug)

    async def set_value(
        self, slug: str, value: Any, password: Any = None, user: Any = None
    ) -> bool:
        if not FakePlumDevice.link_up:
            return False
        self.writes.append((slug, value))
        return True

    async def async_close(self) -> None:
        self.closed = True

    @classmethod
    def reset(cls) -> None:
        cls.link_up = True
        cls.values = {}
        cls.instances = []
