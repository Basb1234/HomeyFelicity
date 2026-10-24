"""Felicity Home Battery - app entry point.

All the work happens in ``drivers/battery``; the app object exists to own the
lifecycle and to leave a line in the log when things start and stop.
"""
from __future__ import annotations

from homey.app import App


class FelicityApp(App):
    """Monitors Felicity TREX inverters over Modbus TCP."""

    async def on_init(self) -> None:
        await super().on_init()
        self.log("Felicity Home Battery initialised")

    async def on_uninit(self) -> None:
        self.log("Felicity Home Battery stopped")
        await super().on_uninit()


homey_export = FelicityApp
