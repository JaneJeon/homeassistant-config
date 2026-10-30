"""Run against the official HA image (no network or production data required).

docker run --rm --network none -v "$PWD:/repo:ro" --entrypoint python3 \
  ghcr.io/home-assistant/home-assistant:2026.10.4 /repo/tools/test_native_backup_integration.py
"""
import asyncio
from datetime import timedelta
import importlib.util
import json
import logging
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

# The existing archive fixture uses pytest decorators, but the HA runtime image
# does not ship pytest. Load its real archive builder with inert decorators.
pytest = ModuleType('pytest')
pytest.mark = SimpleNamespace(parametrize=lambda *args, **kwargs: lambda fn: fn)
sys.modules['pytest'] = pytest
sys.path.insert(0, '/repo')
spec = importlib.util.spec_from_file_location('archive_fixture', '/repo/tools/test_native_snapshot_archive.py')
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)

from homeassistant.core import Context, HomeAssistant
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.components.backup.manager import BackupManagerState
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers import entity_registry, device_registry
from homeassistant.exceptions import Unauthorized
import custom_components.native_snapshot_backups as integration
from custom_components.native_snapshot_backups import sensor

AGENT = 'backblaze_b2.selected'

def archive_for(backup_id, date, flaw=None):
    old_add = fixture.add
    def add(tar, name, value, kind=None):
        if name == 'backup.json':
            data = json.loads(value); data['slug'] = backup_id; data['date'] = date
            value = json.dumps(data).encode()
        return old_add(tar, name, value, kind)
    fixture.add = add
    try: return fixture.make(flaw=flaw)
    finally: fixture.add = old_add


def backup(backup_id, date='2026-10-30T12:00:00Z', slugs=None, automatic=True, included=True, agents=None):
    return SimpleNamespace(backup_id=backup_id, date=date, addons=[SimpleNamespace(slug=x) for x in (slugs if slugs is not None else integration.EXPECTED_SLUGS)], failed_addons=[], with_automatic_settings=automatic, homeassistant_included=included, agents=agents or [AGENT], protected=True)

class FakeAgent:
    def __init__(self): self.backups = {}; self.archives = {}; self.downloaded = []; self.block = None
    async def async_get_backup(self, key): return self.backups.get(key)
    async def async_download_backup(self, key):
        self.downloaded.append(key)
        if self.block:
            event = self.block
            class BlockedStream:
                def __aiter__(self): return self
                async def __anext__(self):
                    await event.wait()
                    raise StopAsyncIteration
                async def aclose(self): pass
            return BlockedStream()
        async def chunks():
            data = self.archives[key]
            for i in range(0, len(data), 4096): yield data[i:i+4096]
        return chunks()

class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.tmp.name)
        self.agent = FakeAgent()
        self.manager = SimpleNamespace(config=SimpleNamespace(data=SimpleNamespace(create_backup=SimpleNamespace(agent_ids=[AGENT], password='test password'))), backup_agents={AGENT:self.agent}, known_backups={}, state=BackupManagerState.IDLE, async_get_backups=AsyncMock(return_value=({},{})), async_subscribe_events=lambda cb: lambda: None)
        self.notices = []; self.notification_fails = False
        async def notice(call):
            self.notices.append((call.service, dict(call.data)))
            if self.notification_fails: raise RuntimeError('notification unavailable')
        self.hass.services.async_register('persistent_notification','create', notice)
        self.hass.services.async_register('persistent_notification','dismiss', notice)
        self.hass.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=False)))
        async def skip_discovery(*args): pass
        self.patches = [patch.object(integration, 'async_get_manager', return_value=self.manager), patch.object(integration.discovery, 'async_load_platform', side_effect=skip_discovery)]
        for p in self.patches: p.start()
        await integration.async_setup(self.hass,{})
        self.checks = self.hass.data[integration.DOMAIN]
        self.checks.data['armed_at']='2026-10-30T00:00:00+00:00'
        await asyncio.sleep(0)

    async def asyncTearDown(self):
        await self.checks.stop()
        for p in self.patches: p.stop()
        await self.hass.async_stop(force=True)
        self.tmp.cleanup()

    def add(self, name, date='2026-10-30T12:00:00Z', flaw=None):
        self.agent.backups[name] = backup(name,date)
        self.agent.archives[name] = archive_for(name,date,flaw)

    async def call(self, service, data, **kw):
        return await self.hass.services.async_call(integration.DOMAIN, service, data, blocking=True, **kw)

    async def test_setup_schema_and_invalid_service_data(self):
        self.assertIn(integration.DOMAIN, integration.CONFIG_SCHEMA({integration.DOMAIN: {}}))
        with self.assertLogs(level='WARNING') as log:
            integration.CONFIG_SCHEMA({integration.DOMAIN: {'unexpected': True}})
        self.assertTrue(any('does not support' in message for message in log.output))
        with self.assertRaises(Exception):
            await self.call('validate', {'backup_id':'../escape'}, return_response=True)
        self.assertEqual(self.agent.downloaded, [])
        with self.assertRaises(Exception):
            await self.call('record_restore_test', {'backup_id':'none', 'proof_sha256':'bad'})
        self.assertNotIn('restore_test', self.checks.data)

    async def test_actual_encrypted_archive_and_agent(self):
        self.add('example')
        other = FakeAgent(); self.manager.backup_agents['backblaze_b2.other'] = other
        result = await self.call('validate', {'backup_id':'example'}, return_response=True)
        self.assertEqual(result['status'], 'valid', repr(result))
        self.assertEqual(self.agent.downloaded, ['example']); self.assertEqual(other.downloaded, [])
        self.assertEqual(self.checks.data['last_validated_id'], 'example')
        self.assertEqual(self.checks.data['attempts']['example']['paperless_file_count'],1)
        self.assertEqual(self.notices[-1][0], 'dismiss')

    async def test_known_partial_metadata_blocks_download(self):
        self.add('omitted')
        self.manager.known_backups['omitted'] = SimpleNamespace(failed_addons=[], failed_folders=['share'])
        report = await self.checks.validate('omitted')
        self.assertEqual(report['status'], 'failed', repr(report))
        self.assertFalse(report['retryable'])
        self.assertEqual(self.agent.downloaded, [])

    async def test_archive_date_controls_freshness_not_b2_metadata(self):
        self.add('example', date='2026-10-30T12:00:00Z')
        self.agent.backups['example'].date='2026-10-30T12:15:00Z'
        report=await self.checks.validate('example')
        self.assertEqual(report['status'], 'valid', repr(report))
        self.assertEqual(self.checks.data['last_validated'], '2026-10-30T12:00:00+00:00')
        self.assertEqual(self.checks.data['latest_created_at'], '2026-10-30T12:00:00+00:00')

    async def test_old_recheck_does_not_clear_new_failure_or_advance_clock(self):
        self.add('new', flaw='corrupt'); self.add('old', date='2026-10-30T11:59:00Z')
        await self.checks.validate('new')
        self.assertEqual(self.checks.data['status'], 'failed')
        old_report = await self.checks.validate('old')
        self.assertEqual(old_report['status'], 'valid', repr(old_report))
        self.assertEqual(self.checks.data['status'], 'failed')
        self.assertEqual(self.checks.data['latest_backup_id'], 'new')
        self.assertEqual(self.checks.data['last_validated'], '2026-10-30T11:59:00+00:00')
        self.assertEqual(self.notices[-1][0], 'create')

    async def test_older_success_cannot_extend_freshness(self):
        self.add('new'); self.add('old', date='2026-10-30T11:59:00Z')
        await self.checks.validate('new'); await self.checks.validate('old')
        self.assertEqual(self.checks.data['last_validated'], '2026-10-30T12:00:00+00:00')
        self.assertEqual(self.checks.data['last_validated_id'], 'new')

    async def test_stopped_native_app_is_failed_backup(self):
        self.add('stopped', flaw='missing')
        entry = self.agent.backups['stopped']
        entry.addons = [SimpleNamespace(slug=integration.EXPECTED_SLUGS[0])]
        entry.failed_addons = [integration.EXPECTED_SLUGS[1]]
        entry.with_automatic_settings = False
        self.manager.async_get_backups.return_value=({'stopped':entry},{})
        await self.checks.scan()
        self.assertEqual(self.agent.downloaded, ['stopped'])
        self.assertEqual(self.checks.data['status'], 'failed')
        self.assertEqual(self.notices[-1][0], 'create')

    async def test_listing_failure_and_recovery_preserves_content_warning(self):
        self.add('bad', flaw='corrupt')
        await self.checks.validate('bad')
        self.manager.async_get_backups.return_value=({}, {AGENT: RuntimeError('offline')})
        await self.checks.scan()
        status_sensor = sensor.NativeBackupSensor(self.checks, 'status', 'Status')
        self.assertEqual(status_sensor.native_value, 'unavailable')
        self.assertEqual(self.notices[-1][1]['notification_id'], 'native_backup_validation_unavailable')
        self.manager.async_get_backups.return_value=({}, {})
        await self.checks.scan()
        self.assertNotIn('scan_error', self.checks.data)
        self.assertEqual(status_sensor.native_value, 'failed')
        self.assertEqual(self.notices[-1], ('dismiss', {'notification_id': 'native_backup_validation_unavailable'}))
        self.assertEqual(self.checks.data['latest_backup_id'], 'bad')
        self.assertTrue(self.checks.data['last_error'])

    async def test_cancel_old_manual_does_not_hide_recent_success(self):
        self.add('new'); self.add('old',date='2026-10-30T11:59:00Z')
        await self.checks.validate('new')
        self.agent.block = asyncio.Event()
        task = asyncio.create_task(self.checks.validate('old'))
        for _ in range(100):
            if self.checks.reader: break
            await asyncio.sleep(.01)
        self.assertIsNotNone(self.checks.reader)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(sensor.NativeBackupSensor(self.checks, 'status', 'Status').native_value, 'valid')
        self.assertEqual(self.checks.data['last_validated_id'], 'new')

    async def test_scan_skips_legacy_and_intentional_partial(self):
        self.add('good')
        values = {'good':self.agent.backups['good'], 'legacy':backup('legacy',date='2026-09-30T12:00:00Z'), 'partial':backup('partial', slugs=[integration.EXPECTED_SLUGS[0]], automatic=False)}
        self.manager.async_get_backups.return_value=(values,{})
        self.hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        for _ in range(200):
            if self.checks.data['attempts'].get('good'): break
            await asyncio.sleep(.01)
        self.assertEqual(self.agent.downloaded,['good'])
        self.assertEqual(self.checks.data['attempts']['good']['status'], 'valid')

    async def test_cancelled_retrieval_does_not_mark_valid(self):
        self.add('example'); self.agent.block=asyncio.Event()
        task=asyncio.create_task(self.checks.validate('example'))
        for _ in range(100):
            if self.checks.reader: break
            await asyncio.sleep(.01)
        self.assertFalse(task.done(), f'validation finished before cancellation: {task.result() if task.done() else None}')
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertNotEqual(self.checks.data['attempts'].get('example', {}).get('status'), 'failed')
        self.assertEqual(self.checks.data['status'], 'unverified')
        self.assertIsNone(self.checks.data.get('last_validated'))
        self.assertFalse(self.notices)
        self.agent.block = None
        self.manager.async_get_backups.return_value=({'example':self.agent.backups['example']},{})
        await self.checks.scan()
        self.assertEqual(self.checks.data['status'], 'valid')

    async def test_stop_during_scan_does_not_leave_stale_success(self):
        self.add('example'); self.agent.block=asyncio.Event()
        self.manager.async_get_backups.return_value=({'example': self.agent.backups['example']},{})
        self.checks.data.update(status='valid', latest_backup_id='older', latest_created_at='2026-10-29T12:00:00+00:00')
        self.checks.queue_scan()
        for _ in range(100):
            if self.checks.reader: break
            await asyncio.sleep(.01)
        self.assertIsNotNone(self.checks.reader)
        await self.checks.stop()
        status_sensor = sensor.NativeBackupSensor(self.checks, 'status', 'Native snapshot backup status')
        self.assertEqual(status_sensor.native_value, 'unverified', repr(self.checks.data))
        self.assertEqual(self.checks.data['pending']['backup_id'], 'example')
        self.assertNotEqual(self.checks.data['attempts'].get('example', {}).get('status'), 'failed')
        self.assertFalse(self.notices)
        self.agent.block = None
        restarted = integration.NativeBackupChecks(self.hass)
        await restarted.load()
        self.assertEqual(sensor.NativeBackupSensor(restarted, 'status', 'Status').native_value, 'unverified')
        await restarted.scan()
        self.assertEqual(restarted.data['status'], 'valid')
        await restarted.stop()

    async def test_failed_notification_persists_report(self):
        self.add('bad', flaw='corrupt'); self.notification_fails=True
        try: await self.checks.validate('bad')
        except Exception: pass
        self.assertEqual(self.checks.data['attempts']['bad']['status'],'failed')
        stored = json.loads((Path(self.tmp.name)/'.storage'/integration.DOMAIN).read_text())['data']
        self.assertEqual(stored['attempts']['bad']['status'],'failed')

    async def test_transport_failure_is_retryable(self):
        self.add('network')
        async def broken_download(key):
            self.agent.downloaded.append(key)
            async def chunks():
                yield b'partial archive'
                raise OSError('connection reset')
            return chunks()
        with patch.object(self.agent, 'async_download_backup', side_effect=broken_download):
            report = await self.checks.validate('network')
        self.assertEqual(report['status'], 'failed')
        self.assertTrue(report['retryable'], repr(report))
        self.assertNotIn('connection reset', report['error'])
        self.manager.async_get_backups.return_value=({'network': self.agent.backups['network']},{})
        await self.checks.scan()
        self.assertEqual(self.checks.data['status'], 'valid')
        self.assertEqual(self.agent.downloaded, ['network', 'network'])

    async def test_failed_dismiss_persists_success(self):
        self.add('good'); self.notification_fails=True
        result = await self.checks.validate('good')
        self.assertEqual(result['status'], 'valid')
        stored = json.loads((Path(self.tmp.name)/'.storage'/integration.DOMAIN).read_text())['data']
        self.assertEqual(stored['attempts']['good']['status'],'valid')

    async def test_services_require_admin(self):
        self.add('example')
        with self.assertRaises(Unauthorized):
            await self.call('validate',{'backup_id':'example'},context=Context(user_id='nonadmin'),return_response=True)
        self.assertEqual(self.agent.downloaded,[])
        self.checks.data['attempts']['example']={'status':'valid'}
        with self.assertRaises(Unauthorized):
            await self.call('record_restore_test',{'backup_id':'example','proof_sha256':'a'*64},context=Context(user_id='nonadmin'))
        self.assertNotIn('restore_test',self.checks.data)
        self.hass.auth.async_get_user.return_value = SimpleNamespace(is_admin=True)
        await self.call('record_restore_test', {'backup_id':'example','proof_sha256':'a'*64}, context=Context(user_id='admin'))
        self.assertEqual(self.checks.data['restore_test']['backup_id'],'example')

    async def test_registered_sensors_reflect_dispatcher(self):
        device_registry.async_setup(self.hass)
        await device_registry.async_load(self.hass, load_empty=True)
        await entity_registry.async_load(self.hass, load_empty=True)
        platform=EntityPlatform(hass=self.hass,logger=logging.getLogger(__name__),domain='sensor',platform_name=integration.DOMAIN,platform=sensor,scan_interval=timedelta(seconds=15),entity_namespace=None)
        entities=[]
        await sensor.async_setup_platform(self.hass,{},entities.extend)
        await platform.async_add_entities(entities)
        self.assertEqual(len(entities),3)
        self.checks.data['status']='failed'; self.checks.data['last_error']='test failure'; self.checks.changed()
        await asyncio.sleep(0)
        self.assertEqual(self.hass.states.get('sensor.native_snapshot_backup_status').state,'failed')
        self.assertEqual(self.hass.states.get('sensor.native_snapshot_backup_status').attributes['last_error'],'test failure')

if __name__ == '__main__': unittest.main(verbosity=2)
