"""Core API checks for the isolated native restore proof."""
import importlib.util
import io
import tarfile
import json
import tempfile
from types import SimpleNamespace
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("native_restore_proof", Path(__file__).with_name("verify_native_restore.py"))
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


class CoreConfigTests(unittest.TestCase):
    def test_running_backed_up_core_with_integration_is_filtered(self):
        raw = json.dumps({"version": "2026.10.4", "state": "RUNNING", "components": ["native_snapshot_backups"], "latitude": 42, "location_name": "private"})
        self.assertEqual(proof.core_config_summary(raw, "2026.10.4"), {"version": "2026.10.4", "state": "RUNNING", "native_integration_loaded": True})

    def test_landing_failed_and_missing_integration_are_rejected(self):
        examples = [
            {"version": "landingpage", "state": "RUNNING", "components": ["native_snapshot_backups"]},
            {"version": "2026.10.4", "state": "STARTING", "components": ["native_snapshot_backups"]},
            {"version": "2026.10.4", "state": "RUNNING", "components": []},
        ]
        for config in examples:
            with self.subTest(config=config), self.assertRaises(proof.ProofError):
                proof.core_config_summary(json.dumps(config), "2026.10.4")

    def test_token_is_passed_via_stdin_not_command(self):
        response = json.dumps({"version": "2026.10.4", "state": "RUNNING", "components": ["native_snapshot_backups"]})
        with patch.object(proof, "ssh", return_value=response) as ssh:
            proof.guest_core_config(["ssh"], "example-private-token", "2026.10.4")
        self.assertNotIn("example-private-token", ssh.call_args.args[1])
        self.assertEqual(ssh.call_args.kwargs["input_bytes"], b"example-private-token\n")


class RestoreSelectionTests(unittest.TestCase):
    def make_archive(self, path, metadata):
        raw = json.dumps(metadata).encode()
        with tarfile.open(path, "w") as tar:
            info = tarfile.TarInfo("./backup.json")
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))

    def test_partial_selects_every_app_and_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.tar"
            apps = [f"test_app_{i}" for i in range(23)]
            self.make_archive(path, {"slug": "fa106105", "type": "partial", "homeassistant": {"version": "2026.10.4"}, "addons": [{"slug": app} for app in apps], "folders": ["share", "ssl"]})
            metadata = proof.backup_metadata_from_archive(path)
            self.assertEqual(metadata["apps"], apps)
            self.assertEqual(metadata["folders"], ["share", "ssl"])
            args = proof.restore_selection_arguments(metadata)
            self.assertEqual(args[0], "--homeassistant")
            self.assertEqual([args[i + 1] for i, item in enumerate(args) if item == "--app"], apps)
            self.assertEqual([args[i + 1] for i, item in enumerate(args) if item == "--folders"], ["share", "ssl"])

    def test_full_uses_full_restore_and_injection_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.tar"
            metadata = {"slug": "fa106105", "type": "full", "homeassistant": {"version": "2026.10.4"}, "addons": [{"slug": "a"}], "folders": ["share"]}
            self.make_archive(path, metadata)
            self.assertEqual(proof.restore_selection_arguments(proof.backup_metadata_from_archive(path)), [])
            metadata["addons"] = [{"slug": "a;bad"}]
            self.make_archive(path, metadata)
            with self.assertRaises(proof.ProofError):
                proof.backup_metadata_from_archive(path)


class RestoreHarnessTests(unittest.TestCase):
    def test_cli_error_is_rejected_even_with_successful_process_exit(self):
        with self.assertRaises(proof.ProofError):
            proof.check_restore_cli_result('{"result":"error","message":"blocked"}')
        proof.check_restore_cli_result('{"result":"ok","data":null}')

    def test_offline_overrides_only_expected_conditions(self):
        calls = [{"ignore_conditions": []}, None, {"ignore_conditions": ["internet_host", "internet_system"]}]
        report = {}
        with patch.object(proof, "guest_json", side_effect=calls) as guest:
            proof.configure_offline_conditions(["ssh"], report)
        self.assertEqual(report["supervisor_job_conditions"]["verified_ignored"], ["internet_host", "internet_system"])
        self.assertIn("--ignore-conditions internet_host --ignore-conditions internet_system", guest.call_args_list[1].args[1])
        with patch.object(proof, "guest_json", return_value={"ignore_conditions": ["healthy"]}) as guest:
            with self.assertRaises(proof.ProofError):
                proof.configure_offline_conditions(["ssh"], {})
            self.assertEqual(guest.call_count, 1)

    def test_interrupt_writes_failure_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            with patch.object(proof, "guard_lab", side_effect=KeyboardInterrupt):
                with self.assertRaises(SystemExit) as error:
                    proof.restore(SimpleNamespace(report=path, lab_dir=directory))
            self.assertEqual(error.exception.code, 130)
            self.assertEqual(json.loads(path.read_text())["status"], "failed")
            self.assertIn("Interrupted", json.loads(path.read_text())["failure"])


if __name__ == "__main__":
    unittest.main()
