"""Archive validation and actual restoration are separate observations."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util

from . import DOMAIN, SIGNAL


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    checks = hass.data[DOMAIN]
    async_add_entities([NativeBackupSensor(checks, key, name) for key, name in (
        ("status", "Native snapshot backup status"),
        ("last_validated", "Native snapshot backup last validated"),
        ("restore_test", "Native snapshot backup last restore test"),
    )])


class NativeBackupSensor(SensorEntity):
    _attr_should_poll = False

    def __init__(self, checks, key, name):
        self.checks = checks
        self.key = key
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{key}"
        if key != "status":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP

    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL, self.async_write_ha_state))

    @property
    def native_value(self):
        if self.key == "status":
            if self.checks.data.get("scan_error"):
                return "unavailable"
            pending = self.checks.data.get("pending")
            if pending and not self.checks.checking_id:
                latest = self.checks.data.get("latest_created_at")
                if not latest or dt_util.parse_datetime(pending["created_at"]) >= dt_util.parse_datetime(latest):
                    return "unverified"
            return "checking" if self.checks.checking_id else self.checks.data.get("status", "unverified")
        value = self.checks.data.get(self.key)
        if self.key == "restore_test":
            value = (value or {}).get("tested_at")
        return dt_util.parse_datetime(value) if value else None

    @property
    def extra_state_attributes(self):
        data = self.checks.data
        if self.key == "restore_test":
            return data.get("restore_test", {})
        if self.key == "last_validated":
            return {"backup_id": data.get("last_validated_id")}
        return {k: data.get(k) for k in ("latest_backup_id", "latest_created_at", "last_error", "scan_error")} | {"checking_backup_id": self.checks.checking_id}
