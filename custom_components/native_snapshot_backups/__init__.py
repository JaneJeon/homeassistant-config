"""Check actual B2 archives before treating a home backup as complete."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from functools import partial
import logging
import re

from homeassistant.components.backup import async_get_manager
from homeassistant.components.backup.manager import BackupManagerState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import config_validation as cv, discovery
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .archive import ValidationError, validate_archive
from .stream import AsyncDownloadReader

DOMAIN = "native_snapshot_backups"
SIGNAL = f"{DOMAIN}_updated"
EXPECTED_SLUGS = ("894a6dc5_victoria-metrics", "894a6dc5_victoria-logs")
CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)
_LOGGER = logging.getLogger(__name__)
_MIN_TIME = datetime.min.replace(tzinfo=UTC)


def _time(value: str | None) -> datetime | None:
    return dt_util.parse_datetime(value) if value else None


def _json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(v) for v in value]
    return value


class NativeBackupChecks:
    """Serialize streaming checks; keep creation time separate from check time."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.manager = async_get_manager(hass)
        self.store = Store(hass, 1, DOMAIN, private=True)
        self.data: dict = {}
        self.lock = asyncio.Lock()
        self.scan_lock = asyncio.Lock()
        self.reader: AsyncDownloadReader | None = None
        self.stopping = False
        self.tasks: set[asyncio.Task] = set()
        self.validation_tasks: set[asyncio.Task] = set()
        self.checking_id: str | None = None

    async def load(self) -> None:
        self.data = await self.store.async_load() or {
            "armed_at": dt_util.utcnow().isoformat(),
            "status": "unverified",
            "attempts": {},
        }
        await self.store.async_save(self.data)

    @callback
    def changed(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL)

    @callback
    def queue_scan(self, _=None) -> None:
        if self.stopping or self.scan_lock.locked():
            return
        task = self.hass.async_create_background_task(self.scan(), "Validate native B2 backups")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def agent(self):
        selected = self.manager.config.data.create_backup.agent_ids
        ids = [agent_id for agent_id in selected if agent_id.startswith("backblaze_b2.")]
        if len(ids) != 1 or ids[0] not in self.manager.backup_agents:
            raise ValidationError("Exactly one configured Backblaze B2 agent must be available")
        return ids[0], self.manager.backup_agents[ids[0]]

    async def scan(self) -> None:
        async with self.scan_lock:
            if self.stopping or self.manager.state != BackupManagerState.IDLE:
                return
            try:
                agent_id, _ = self.agent()
                backups, errors = await self.manager.async_get_backups()
                if agent_id in errors:
                    raise ValidationError("Cannot list backups from Backblaze B2")
                recovered_listing = self.data.pop("scan_error", None) is not None
                if recovered_listing and self.hass.services.has_service("persistent_notification", "dismiss"):
                    try:
                        await self.hass.services.async_call("persistent_notification", "dismiss", {
                            "notification_id": "native_backup_validation_unavailable",
                        }, blocking=True)
                    except Exception:
                        _LOGGER.warning("Could not dismiss backup listing notification")
                armed_at = _time(self.data["armed_at"])
                candidates = []
                for backup in backups.values():
                    present = {addon.slug for addon in backup.addons}
                    failed = {getattr(addon, "slug", addon) for addon in backup.failed_addons}
                    complete_home = backup.with_automatic_settings or set(EXPECTED_SLUGS) <= present | failed
                    if (backup.homeassistant_included and complete_home and agent_id in backup.agents
                            and dt_util.parse_datetime(backup.date) >= armed_at):
                        candidates.append(backup)
                for backup in sorted(candidates, key=lambda item: item.date):
                    prior = self.data["attempts"].get(backup.backup_id)
                    if prior and prior["status"] == "valid":
                        continue
                    # Invalid artifacts need explicit recheck; transient retrieval failures retry.
                    if prior and not prior.get("retryable"):
                        continue
                    await self.validate(backup.backup_id)
                await self.save()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = self.reason(exc)
                changed = self.data.get("scan_error") != reason
                self.data["scan_error"] = reason
                await self.save()
                if changed:
                    await self.alert("B2 backup validation unavailable", reason, "native_backup_validation_unavailable")

    @staticmethod
    def reason(exc: Exception) -> str:
        # Network exception text can include request details. Never persist it.
        return str(exc) if isinstance(exc, ValidationError) else f"B2 archive could not be checked ({type(exc).__name__})"

    async def save(self) -> None:
        await self.store.async_save(self.data)
        self.changed()

    async def alert(self, title: str, reason: str, notification_id="automatic_backup_problem") -> None:
        for domain, service, data in (
            ("persistent_notification", "create", {
                "notification_id": notification_id, "title": title,
                "message": f"{reason}. Keep the Paperless pending-shred originals until a later complete backup passes validation.",
            }),
            ("notify", "send_message", {
                "entity_id": "notify.telegram_bot_7842566738_7054392448",
                "title": title, "message": reason,
            }),
        ):
            if self.hass.services.has_service(domain, service):
                try:
                    await self.hass.services.async_call(domain, service, data, blocking=True)
                except Exception:
                    _LOGGER.warning("Could not deliver native backup alert through %s", domain)

    async def validate(self, backup_id: str) -> dict:
        task = asyncio.current_task()
        self.validation_tasks.add(task)
        try:
            return await self._validate(backup_id)
        finally:
            self.validation_tasks.discard(task)

    async def _validate(self, backup_id: str) -> dict:
        async with self.lock:
            if self.stopping:
                raise HomeAssistantError("Home Assistant is stopping")
            agent_id, agent = self.agent()
            backup = await agent.async_get_backup(backup_id)
            if not backup:
                raise ValidationError("Backup is absent from Backblaze B2")
            created = dt_util.parse_datetime(backup.date)
            if created is None:
                raise ValidationError("Backup has no valid creation date")
            self.checking_id = backup_id
            self.data["pending"] = {"backup_id": backup_id, "created_at": created.isoformat()}
            await self.save()
            self.changed()
            report = {"backup_id": backup_id, "created_at": created.isoformat(), "agent_id": agent_id}
            try:
                known = self.manager.known_backups.get(backup_id)
                if known and (known.failed_addons or known.failed_folders):
                    raise ValidationError("Home Assistant omitted an app or folder from this backup")
                password = self.manager.config.data.create_backup.password if backup.protected else None
                if backup.protected and not password:
                    raise ValidationError("The configured backup encryption key is unavailable")
                async with asyncio.timeout(3600):
                    stream = await agent.async_download_backup(backup_id)
                    reader = self.reader = AsyncDownloadReader(self.hass.loop, stream)
                    worker = self.hass.async_add_executor_job(partial(
                        validate_archive, reader, password=password,
                        expected_backup_id=backup_id, expected_slugs=EXPECTED_SLUGS,
                    ))
                    try:
                        result = await asyncio.shield(worker)
                    finally:
                        await reader.aclose()
                        await asyncio.gather(worker, return_exceptions=True)
                        self.reader = None
                created = result.created_at
                report.update(_json_value(asdict(result)), status="valid", retryable=False)
                if created > (_time(self.data.get("last_validated")) or _MIN_TIME):
                    self.data["last_validated"] = created.isoformat()
                    self.data["last_validated_id"] = backup_id
                self.data.pop("scan_error", None)
            except asyncio.CancelledError:
                await self.save()
                raise
            except Exception as exc:
                transport_failed = "reader" in locals() and reader.transport_failed
                report.update(status="failed", error=("B2 download interrupted" if transport_failed else self.reason(exc)),
                              retryable=transport_failed or not isinstance(exc, ValidationError))
            finally:
                self.checking_id = None
                self.changed()
            self.data["attempts"][backup_id] = report
            self.data.pop("pending", None)
            latest = _time(self.data.get("latest_created_at")) or _MIN_TIME
            if created >= latest:
                self.data.update(latest_created_at=created.isoformat(), latest_backup_id=backup_id, status=report["status"])
                if report["status"] == "failed":
                    self.data["last_error"] = report["error"]
                    await self.alert("Backup content validation failed", f"Backup {backup_id}: {report['error']}")
                else:
                    self.data.pop("last_error", None)
                    if self.hass.services.has_service("persistent_notification", "dismiss"):
                        try:
                            await self.hass.services.async_call("persistent_notification", "dismiss", {
                                "notification_id": "automatic_backup_problem",
                            }, blocking=True)
                        except Exception:
                            _LOGGER.warning("Could not dismiss native backup notification")
            await self.save()
            return report

    async def stop(self, _=None) -> None:
        self.stopping = True
        if self.reader:
            self.reader.abort()
        tasks = self.tasks | self.validation_tasks
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*list(tasks), return_exceptions=True)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    checks = NativeBackupChecks(hass)
    await checks.load()
    hass.data[DOMAIN] = checks

    async def require_admin(call: ServiceCall) -> None:
        if call.context.user_id is not None:
            user = await hass.auth.async_get_user(call.context.user_id)
            if user is None or not user.is_admin:
                raise Unauthorized

    async def validate_service(call: ServiceCall) -> dict:
        await require_admin(call)
        backup_id = call.data.get("backup_id")
        if not isinstance(backup_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", backup_id):
            raise HomeAssistantError("Provide a backup_id from Backblaze B2")
        return await checks.validate(backup_id)

    async def record_restore(call: ServiceCall) -> None:
        await require_admin(call)
        backup_id = call.data.get("backup_id")
        proof = call.data.get("proof_sha256")
        report = checks.data["attempts"].get(backup_id, {})
        if report.get("status") != "valid" or not isinstance(proof, str) or not re.fullmatch(r"[0-9a-f]{64}", proof):
            raise HomeAssistantError("A validated backup ID and restore-proof SHA256 are required")
        checks.data["restore_test"] = {"backup_id": backup_id, "proof_sha256": proof, "tested_at": dt_util.utcnow().isoformat()}
        await checks.save()

    hass.services.async_register(DOMAIN, "validate", validate_service, supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "record_restore_test", record_restore)

    @callback
    def on_backup_event(event) -> None:
        if event.manager_state == BackupManagerState.IDLE:
            checks.queue_scan()

    remove_backup = checks.manager.async_subscribe_events(on_backup_event)
    remove_timer = async_track_time_interval(hass, checks.queue_scan, timedelta(minutes=5))

    async def stop(event) -> None:
        remove_backup()
        remove_timer()
        await checks.stop()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)
    if hass.is_running:
        checks.queue_scan()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, checks.queue_scan)
    hass.async_create_task(discovery.async_load_platform(hass, Platform.SENSOR, DOMAIN, {}, config))
    return True
