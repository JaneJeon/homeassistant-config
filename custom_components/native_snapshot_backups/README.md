# Native snapshot backup validation

This integration checks the actual encrypted B2 artifact before a complete-home backup advances the backup-health timestamp. It complements the snapshot-aware VictoriaMetrics and VictoriaLogs apps in `homeassistant-apps`.

## What is checked

The validator selects the existing configured Backblaze B2 agent and downloads the requested backup ID from that agent. It streams decryption using HA's existing backup key. It requires the HA configuration, Paperless app data, Share/Paperless media and both native snapshot exports. Every app and folder listed in the archive metadata must have a readable, authenticated component.

The native exports must match their installation identities, pinned engine versions and file inventories. Live/work directories and pending promotion operations are forbidden in the artifact. The snapshot request must start at or after the outer archive's creation time. An app that was stopped cannot silently contribute an old export as a new snapshot.

The completed event and HA's built-in last-successful timestamp can advance despite an omitted app or folder. They are not the content-validation result. A deliberate partial backup does not replace a complete-home recovery point.

## States and actions

- `sensor.native_snapshot_backup_status` reports validation state and the latest checked artifact or retrieval error.
- `sensor.native_snapshot_backup_last_validated` is the creation time of the newest valid artifact, not the time a check ran. Rechecking older data cannot renew the 36-hour freshness clock.
- `sensor.native_snapshot_backup_last_restore_test` records a separate full restore test and its evidence digest. Archive validation does not establish that restoration was demonstrated.
- `native_snapshot_backups.validate` accepts `backup_id` for an explicit recheck. It requires an administrator when called by a user.
- `native_snapshot_backups.record_restore_test` accepts a validated `backup_id` and the SHA256 of an independently checked restore evidence report. Call it only after completing the actual isolated restore.

New complete-home backups are checked after completion and retried after startup. Transport failures retry; invalid archive contents remain failures until explicitly rechecked or superseded by a newer valid backup. Retrieval unavailability has its own notification. Content and upload warnings must remain open until a newer complete archive passes.

## Restore acceptance

Use the shipped `tools/native_restore_lab.py` and `tools/verify_native_restore.py` commands to repeat the recovery drill. The lab uses ARM64 HA OS with QEMU/HVF. Preload required public container images before locking the lab. The restore lock permanently forbids Internet preload mode for that lab disk.

Only after the lab is locked and running with restricted networking may a production backup be introduced. Restore the exact B2 artifact through HA/Supervisor, check native app startup and historical query results, and compare restored Paperless media with the archive manifest. Production-LAN access and Internet access must fail in the isolated guest.

Keep the evidence report private: it records the backup ID, artifact digest, software versions, query comparisons and file-manifest comparisons. Repeat the full drill after changes to snapshot formats or restore/bootstrap code.

Create a local environment for the operator commands:

```sh
python3 -m venv /tmp/native-backup-tools
/tmp/native-backup-tools/bin/pip install -r tools/requirements-native-backup.txt
/tmp/native-backup-tools/bin/python tools/verify_native_restore.py --help
```

`download` requires the HA URL, the exact B2 agent and backup IDs, and a private token file. `validate` and `restore` read the encryption key from a private file. The tools refuse credentials in non-private files. Use `capture` before cutover to record fixed historical queries. The `restore` command requires the locked isolated lab, its SSH key, the encrypted archive, those expectations and a new private report path. Supply `--ha-token-file` to verify the preserved HA account and the actual restored Core API; a running container or landing page alone cannot pass.
