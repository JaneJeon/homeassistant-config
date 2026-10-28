"""Temporary diagnostics for the unexplained October 2026 Core stalls."""

from __future__ import annotations

import asyncio
from pathlib import Path

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Event, HassJob, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .watchdog import run_watchdog

DOMAIN = "core_stall_watchdog"
CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    stop = asyncio.Event()
    task: asyncio.Task[None] | None = None

    @callback
    def start(event: Event | None = None) -> None:
        nonlocal task
        if not stop.is_set() and task is None:
            task = hass.async_create_background_task(
                run_watchdog(stop, Path(hass.config.path("core-stall-watchdog.log"))),
                "Core stall stack watchdog",
            )

    async def shutdown() -> None:
        stop.set()
        if task is not None:
            await task

    # Shutdown jobs run before HA cancels background tasks.
    hass.async_add_shutdown_job(HassJob(shutdown, "Stop core stall watchdog"))
    if hass.is_running:
        start()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, start)
    return True
