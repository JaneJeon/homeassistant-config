"""Core API checks for the isolated native restore proof."""
import importlib.util
import json
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


if __name__ == "__main__":
    unittest.main()
