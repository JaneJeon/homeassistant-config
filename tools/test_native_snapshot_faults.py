"""Run inside the published VictoriaLogs image; no production data or network."""

import errno
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

os.environ["VICTORIA_ENGINE"] = "victorialogs"
loader = SourceFileLoader("published_native_runtime", "/usr/local/bin/native-backup")
module_spec = spec_from_loader(loader.name, loader)
runtime = module_from_spec(module_spec)
loader.exec_module(runtime)


class SnapshotFaults(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        root = Path(self.scratch.name)
        for key, name in (
            ("DATA", ""), ("LIVE", "live"), ("WORK", "work"), ("BACKUP", "backup"),
            ("INSTALL", "installation.json"), ("READY", "live-ready.json"),
            ("JOURNAL", "operation.json"),
        ):
            setattr(runtime, key, root / name)
        runtime.startup("init-empty")
        self.snapshot = runtime.LIVE / "partitions/20261030/snapshots/fault-fixture"
        for table in ("datadb", "indexdb"):
            path = self.snapshot / table
            path.mkdir(parents=True)
            (path / "parts.json").write_text("[]")
        (self.snapshot / "fixture").write_text("preserved snapshot")

    def tearDown(self):
        self.scratch.cleanup()

    def export(self):
        def api(path, params=None):
            return [str(self.snapshot)] if path.endswith("/create") else None
        with patch.object(runtime, "api", side_effect=api):
            runtime.export()

    def test_enospc_during_copy_preserves_the_committed_export(self):
        self.export()
        previous = (runtime.BACKUP / "manifest.json").read_bytes()
        with patch.object(runtime, "copy_verified", side_effect=OSError(errno.ENOSPC, "injected full disk")):
            with self.assertRaises(OSError) as error:
                self.export()
        self.assertEqual(error.exception.errno, errno.ENOSPC)
        self.assertEqual(previous, (runtime.BACKUP / "manifest.json").read_bytes())
        self.assertFalse(runtime.JOURNAL.exists())
        runtime.validate_backup()

    def test_enospc_manifest_write_cannot_publish_partial_state(self):
        self.export()
        previous = (runtime.BACKUP / "manifest.json").read_bytes()
        write = runtime.durable_json

        def full(path, value):
            if path == runtime.WORK / "export/manifest.json":
                raise OSError(errno.ENOSPC, "injected full disk")
            return write(path, value)

        with patch.object(runtime, "durable_json", side_effect=full):
            with self.assertRaises(OSError):
                self.export()
        self.assertEqual(previous, (runtime.BACKUP / "manifest.json").read_bytes())
        self.assertFalse(runtime.JOURNAL.exists())
        runtime.validate_backup()

    def test_interrupted_hydration_is_uncommitted_and_can_retry(self):
        self.export()
        shutil.rmtree(runtime.LIVE)

        def interrupted(source, destination):
            destination.mkdir()
            (destination / "partial").write_text("incomplete")
            raise InterruptedError("injected interruption")

        with patch.object(runtime, "copy_verified", side_effect=interrupted):
            with self.assertRaises(InterruptedError):
                runtime.startup("restore_only")
        self.assertFalse(runtime.LIVE.exists())
        self.assertTrue(runtime.INSTALL.exists())
        runtime.validate_backup()
        runtime.startup("restore_only")
        self.assertEqual((runtime.LIVE / "partitions/20261030/fixture").read_text(), "preserved snapshot")


if __name__ == "__main__":
    unittest.main(verbosity=2)
