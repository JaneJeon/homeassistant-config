#!/usr/bin/env python3
"""Prove a stopped native app's old export cannot validate as a fresh backup.

Requires aiohttp and securetar; only connects to the locked, isolated local lab.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tarfile
import shlex
import stat
import subprocess
import time
import threading
from uuid import uuid4

import verify_native_restore as proof


class SupervisorWS:
    def __init__(self, session, token):
        self.session = session
        self.token = token
        self.counter = 0
        self.ws = None

    async def __aenter__(self):
        self.ws = await self.session.ws_connect("http://127.0.0.1:18123/api/websocket", timeout=15)
        greeting = await self.ws.receive_json(timeout=15)
        if greeting.get("type") != "auth_required":
            raise proof.ProofError("Unexpected local Core WebSocket greeting")
        await self.ws.send_json({"type": "auth", "access_token": self.token})
        response = await self.ws.receive_json(timeout=15)
        if response.get("type") != "auth_ok":
            raise proof.ProofError("Local Core WebSocket authentication failed")
        self.token = None
        return self

    async def __aexit__(self, *_):
        if self.ws:
            await self.ws.close()

    async def request(self, endpoint, method="get", data=None, timeout=30):
        self.counter += 1
        message = {"id": self.counter, "type": "supervisor/api", "endpoint": endpoint, "method": method, "timeout": timeout}
        if data is not None:
            message["data"] = data
        await self.ws.send_json(message)
        while True:
            response = await self.ws.receive_json(timeout=timeout + 10)
            if response.get("id") != self.counter:
                continue
            if response.get("type") != "result" or response.get("success") is not True:
                raise proof.ProofError("Local Supervisor API call failed")
            return response.get("result")


def assert_positive_report(path, archive):
    info = Path(path).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise proof.ProofError("Positive proof report must be a private regular file")
    positive = json.loads(Path(path).read_text())
    metadata = proof.backup_metadata_from_archive(archive)
    actual_hash = proof.digest(archive)
    if positive.get("status") != "passed" or positive.get("backup_id") != metadata["backup_id"] or positive.get("source_archive_sha256") != actual_hash:
        raise proof.ProofError("A passing restore proof for this exact archive is required")
    apps = positive.get("archive_validation", {}).get("apps", {})
    generations = {slug: apps.get(slug, {}).get("generation") for slug in proof.SLUGS}
    if any(not isinstance(value, str) for value in generations.values()):
        raise proof.ProofError("Positive report lacks native snapshot generations")
    return metadata, actual_hash, generations


def archived_snapshots(path, password):
    from securetar import SecureTarDecryptStream, SecureTarRootKeyContext

    snapshots = {}
    with tarfile.open(path, mode="r|") as outer:
        for member in outer:
            name = member.name.removeprefix("./")
            slug = next((item for item in proof.SLUGS if name in (item + ".tar.gz", item + ".tar")), None)
            if slug is None:
                continue
            if slug in snapshots or not member.isfile():
                raise proof.ProofError("Invalid native app archive")
            with SecureTarDecryptStream(outer.extractfile(member), ciphertext_size=member.size, root_key_context=SecureTarRootKeyContext(password)) as decrypted:
                with tarfile.open(fileobj=decrypted, mode="r|gz" if name.endswith(".gz") else "r|") as inner:
                    lines = []
                    raw_manifest = None
                    for item in inner:
                        item_name = item.name.removeprefix("./")
                        if item_name == "data/backup/manifest.json" and item.isfile():
                            raw_manifest = inner.extractfile(item).read(8 * 1024 * 1024 + 1)
                            if len(raw_manifest) > 8 * 1024 * 1024:
                                raise proof.ProofError("Native manifest too large")
                        elif item_name.startswith("data/backup/payload/") and item.isfile():
                            relative = item_name.removeprefix("data/backup/")
                            if "\n" in relative or "\\" in relative:
                                raise proof.ProofError("Unsupported native payload path")
                            hasher = hashlib.sha256()
                            stream = inner.extractfile(item)
                            for chunk in iter(lambda: stream.read(proof.CHUNK), b""):
                                hasher.update(chunk)
                            lines.append((relative, hasher.hexdigest()))
                while decrypted.read(proof.CHUNK):
                    pass
            if raw_manifest is None or not lines:
                raise proof.ProofError("Native snapshot metadata or payload missing")
            generation = json.loads(raw_manifest).get("generation")
            payload_manifest = "".join(f"{digest}  {name}\n" for name, digest in sorted(lines))
            snapshots[slug] = {"generation": generation, "manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(), "payload_sha256": hashlib.sha256(payload_manifest.encode()).hexdigest()}
    if set(snapshots) != set(proof.SLUGS):
        raise proof.ProofError("Native app snapshot missing from new archive")
    return snapshots


def guest_snapshot(base, slug, allow_stopped=False):
    names = " ".join(shlex.quote(prefix + slug) for prefix in ("app_", "addon_"))
    command = "for n in " + names + "; do docker inspect --format '{{range .Mounts}}{{if eq .Destination \"/data\"}}{{println .Source}}{{end}}{{end}}' \"$n\" 2>/dev/null || :; done"
    sources = [line.strip() for line in proof.ssh(base, command).splitlines() if line.strip()]
    valid = {f"/mnt/data/supervisor/{kind}/data/{slug}" for kind in ("apps", "addons")}
    if not sources and allow_stopped:
        candidates = " ".join(shlex.quote(path) for path in sorted(valid))
        directory_command = "for d in " + candidates + "; do if [ -d \"$d\" ]; then printf '%s\\n' \"$d\"; fi; done"
        sources = [line.strip() for line in proof.ssh(base, directory_command).splitlines() if line.strip()]
    if len(sources) != 1 or sources[0] not in valid:
        raise proof.ProofError("Native app data mount is invalid or ambiguous")
    app_dir = sources[0]
    root = app_dir + "/backup"
    path_parts = ["/mnt", "/mnt/data", "/mnt/data/supervisor", str(Path(app_dir).parent.parent), str(Path(app_dir).parent), app_dir, root, root + "/manifest.json", root + "/payload"]
    no_links = " && ".join("[ ! -L " + shlex.quote(part) + " ]" for part in path_parts)
    manifest = json.loads(proof.ssh(base, no_links + " && cat " + shlex.quote(root + "/manifest.json"), timeout=30))
    generation = manifest.get("generation")
    if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f-]{36}", generation):
        raise proof.ProofError("Invalid live snapshot generation")
    # Sorted content hashes include all payload file names; verify no symlinks.
    command = f'''set -e; set -o pipefail; {no_links}; cd {shlex.quote(root)}; test -d payload; test -z "$(find payload ! -type d ! -type f -print -quit)"; manifest=$(sha256sum manifest.json | cut -d' ' -f1); payload=$(find payload -type f | LC_ALL=C sort | while IFS= read -r f; do sha256sum "$f"; done | sha256sum | cut -d' ' -f1); printf '%s %s\\n' "$manifest" "$payload"'''
    parts = proof.ssh(base, command, timeout=1800).strip().split()
    if len(parts) != 2 or any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in parts):
        raise proof.ProofError("Invalid snapshot hash result")
    return {"generation": generation, "manifest_sha256": parts[0], "payload_sha256": parts[1], "data_path": app_dir}


def snapshot_identity(snapshot):
    return {key: snapshot.get(key) for key in ("generation", "manifest_sha256", "payload_sha256")}


def container_state(base, slug):
    names = " ".join(shlex.quote(prefix + slug) for prefix in ("app_", "addon_"))
    command = "for n in " + names + "; do if docker inspect --format '{{.Id}} {{.State.StartedAt}} {{.State.Status}}' \"$n\" >/dev/null 2>&1; then printf '%s ' \"$n\"; docker inspect --format '{{.Id}} {{.State.StartedAt}} {{.State.Status}}' \"$n\"; fi; done"
    result = proof.ssh(base, command).strip().split()
    if not result:
        return {"status": "absent"}
    if len(result) != 4 or result[3] not in ("exited", "created"):
        raise proof.ProofError("Native app container is not stopped or its identity is ambiguous")
    return {"container_name": result[0], "container_id": result[1], "started_at": result[2], "status": result[3]}


class StartMonitor:
    """Keep a live Docker event subscription throughout the backup and audit."""

    def __init__(self, base):
        self.base = base
        self.ready = threading.Event()
        self.starts = []

    def __enter__(self):
        command = "printf 'READY\\n'; exec docker events --filter type=container --filter event=start --format '{{.Actor.Attributes.name}}'"
        self.process = subprocess.Popen(self.base + [command], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        names = {prefix + slug for slug in proof.SLUGS for prefix in ("app_", "addon_")}
        def read():
            for raw in self.process.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if line == "READY":
                    self.ready.set()
                elif line in names:
                    self.starts.append(line)
        self.thread = threading.Thread(target=read, daemon=True)
        self.thread.start()
        if not self.ready.wait(15) or self.process.poll() is not None:
            self.process.terminate()
            raise proof.ProofError("Could not start Docker event monitoring")
        return self

    def verify(self):
        if self.process.poll() is not None:
            raise proof.ProofError("Docker event monitoring stopped unexpectedly")
        if self.starts:
            raise proof.ProofError("A native app container started during backup")

    def __exit__(self, exc_type, *_):
        try:
            if exc_type is None:
                self.verify()
        finally:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
            self.thread.join(timeout=5)


def no_job_errors(job):
    if not isinstance(job, dict) or job.get("errors"):
        return False
    children = job.get("child_jobs", [])
    return isinstance(children, list) and all(no_job_errors(child) for child in children)


async def run(args, report):
    import aiohttp

    root = Path(args.lab_dir).resolve()
    print("Checking isolated lab and passing restore evidence", flush=True)
    proof.guard_lab(root)
    metadata, source_hash, source_generations = assert_positive_report(args.positive_report, args.source_archive)
    report.update(source_backup_id=metadata["backup_id"], source_archive_sha256=source_hash)
    key = proof.private_file(args.key_file)
    token = proof.private_file(args.ha_token_file)
    # The passing report is bound to the full SHA-256 of this exact archive.
    report["source_positive_proof_verified"] = True
    base = proof.ssh_base(root, args.ssh_key)
    report["isolation"] = proof.probes(base)
    timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_read=180)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
        async with SupervisorWS(session, token) as ws:
            del token
            print("Recording native app states and snapshot hashes", flush=True)
            before = {}
            report["apps"] = before
            prior = None
            if args.prestop_report:
                prior_path = Path(args.prestop_report)
                prior_info = prior_path.lstat()
                if not stat.S_ISREG(prior_info.st_mode) or prior_info.st_mode & 0o077:
                    raise proof.ProofError("Pre-stop report must be a private regular file")
                prior = json.loads(prior_path.read_text())
                if prior.get("phase") != "stop_native_apps" or prior.get("source_archive_sha256") != source_hash or prior.get("source_backup_id") != metadata["backup_id"]:
                    raise proof.ProofError("Pre-stop report does not match the proven source")
                report["prestop_report_sha256"] = proof.digest(prior_path)
            for slug in proof.SLUGS:
                info = await ws.request(f"/addons/{slug}/info")
                if not isinstance(info, dict):
                    raise proof.ProofError("Invalid native app state")
                if prior is None:
                    if info.get("state") not in ("started", "running"):
                        raise proof.ProofError("Both native apps must initially be running")
                    before[slug] = {"initial_state": info["state"], "initial_boot": info.get("boot"), "initial_watchdog": info.get("watchdog"), "initial_snapshot": guest_snapshot(base, slug), "source_generation": source_generations[slug]}
                else:
                    previous = prior.get("apps", {}).get(slug, {})
                    initial = previous.get("initial_snapshot")
                    if info.get("state") != "stopped" or info.get("boot") != "manual" or info.get("watchdog") is not False or previous.get("initial_state") not in ("started", "running") or not isinstance(initial, dict):
                        raise proof.ProofError("Pre-stop evidence or current app state is invalid")
                    before[slug] = {"initial_state": previous["initial_state"], "initial_boot": previous.get("initial_boot"), "initial_watchdog": previous.get("initial_watchdog"), "initial_snapshot": initial, "source_generation": source_generations[slug]}
                if before[slug]["initial_snapshot"].get("generation") != source_generations[slug]:
                    raise proof.ProofError("Current native generation differs from the proven source archive")
            report["phase"] = "stop_native_apps"
            if prior is None:
                print("Disabling native app watchdogs and stopping apps", flush=True)
                for slug in proof.SLUGS:
                    await ws.request(f"/addons/{slug}/options", "post", {"boot": "manual", "watchdog": False})
                    info = await ws.request(f"/addons/{slug}/info")
                    if info.get("boot") != "manual" or info.get("watchdog") is not False:
                        raise proof.ProofError("Could not disable native app boot and watchdog")
                    await ws.request(f"/addons/{slug}/stop", "post", timeout=120)
            for slug in proof.SLUGS:
                info = await ws.request(f"/addons/{slug}/info")
                if info.get("state") != "stopped" or info.get("boot") != "manual" or info.get("watchdog") is not False:
                    raise proof.ProofError("Native app is not stopped with boot and watchdog disabled")
                before[slug]["stopped_snapshot"] = guest_snapshot(base, slug, allow_stopped=True)
                before[slug]["stopped_container"] = container_state(base, slug)
                if snapshot_identity(before[slug]["initial_snapshot"]) != snapshot_identity(before[slug]["stopped_snapshot"]):
                    raise proof.ProofError("Snapshot changed while native apps were being stopped")
            report["phase"] = "create_local_backup"
            print("Creating a local backup with both native apps stopped", flush=True)
            with StartMonitor(base) as monitor:
                name = "stopped-native-proof-" + uuid4().hex
                request = {"name": name, "homeassistant": True, "addons": metadata["apps"], "folders": metadata["folders"], "password": key, "background": True, "location": ".local"}
                created = await ws.request("/backups/new/partial", "post", request, timeout=120)
                del request
                if not isinstance(created, dict):
                    raise proof.ProofError("Invalid backup creation response")
                job_id = created.get("job_id")
                if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
                    raise proof.ProofError("Backup job identifier missing")
                report["backup_job_id"] = job_id
                deadline = time.monotonic() + args.timeout
                backup_id = created.get("slug")
                while time.monotonic() < deadline:
                    response = await ws.request(f"/jobs/{job_id}")
                    if not isinstance(response, dict):
                        raise proof.ProofError("Invalid backup job response")
                    job = response
                    monitor.verify()
                    if job.get("reference"):
                        backup_id = job["reference"]
                    for slug in proof.SLUGS:
                        info = await ws.request(f"/addons/{slug}/info")
                        if info.get("state") != "stopped" or container_state(base, slug) != before[slug]["stopped_container"]:
                            raise proof.ProofError("Native app restarted during backup")
                    if job.get("done") is True:
                        if not no_job_errors(job):
                            raise proof.ProofError("Local backup job reported errors")
                        break
                    await asyncio.sleep(5)
                else:
                    raise proof.ProofError("Local backup job timed out")
                if not isinstance(backup_id, str) or not re.fullmatch(r"[0-9a-f]{8}", backup_id):
                    raise proof.ProofError("Completed backup identifier missing")
                report["new_backup_id"] = backup_id
                backup_info = proof.guest_json(base, f"ha backups info {backup_id}")
                if not isinstance(backup_info, dict) or backup_info.get("slug") != backup_id or backup_info.get("homeassistant") != metadata["core_version"]:
                    raise proof.ProofError("Completed backup CLI result is missing Core or has the wrong identifier")
                report["backup_cli_confirmed"] = True
                report["phase"] = "retrieve_and_validate"
                print("Retrieving local archive and checking stale snapshot rejection", flush=True)
                fd = os.open(args.archive_output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "wb") as output:
                    result = subprocess.run(base + [f"cat /mnt/data/supervisor/backup/{backup_id}.tar"], stdout=output, stderr=subprocess.PIPE, timeout=3600)
                if result.returncode:
                    raise proof.ProofError("Could not retrieve the local encrypted archive")
                report["new_archive_sha256"] = proof.digest(args.archive_output)
                new_metadata = proof.backup_metadata_from_archive(args.archive_output)
                if new_metadata["backup_id"] != backup_id or set(new_metadata["apps"]) != set(metadata["apps"]) or set(new_metadata["folders"]) != set(metadata["folders"]):
                    raise proof.ProofError("New backup omitted or added configured components")
                validator = proof.load_validator()
                try:
                    with open(args.archive_output, "rb") as archive:
                        validator.validate_archive(archive, password=key, expected_backup_id=backup_id, expected_slugs=proof.SLUGS)
                except validator.ValidationError as exc:
                    if str(exc) != "Snapshot predates backup":
                        raise proof.ProofError("New backup failed validation for an unexpected reason") from exc
                    report["validation_rejection"] = "Snapshot predates backup"
                else:
                    raise proof.ProofError("Stale native exports unexpectedly passed validation")
                print("Comparing archived native snapshots with the stopped app data", flush=True)
                new_snapshots = archived_snapshots(args.archive_output, key)
                for slug in proof.SLUGS:
                    before[slug]["archived_snapshot"] = new_snapshots[slug]
                    if new_snapshots[slug] != snapshot_identity(before[slug]["stopped_snapshot"]):
                        raise proof.ProofError("Archived native snapshot differs from stopped app data")
                    info = await ws.request(f"/addons/{slug}/info")
                    before[slug]["final_state"] = info.get("state")
                    before[slug]["final_snapshot"] = guest_snapshot(base, slug, allow_stopped=True)
                    before[slug]["final_container"] = container_state(base, slug)
                    if before[slug]["final_state"] != "stopped" or snapshot_identity(before[slug]["final_snapshot"]) != snapshot_identity(before[slug]["stopped_snapshot"]) or before[slug]["final_container"] != before[slug]["stopped_container"]:
                        raise proof.ProofError("Native app or its snapshot changed during the stopped-app proof")
    report["docker_start_monitor"] = "No native container start events observed during backup and verification"
    report["phase"] = "complete"
    report["status"] = "passed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("lab-dir", "positive-report", "source-archive", "key-file", "ha-token-file", "ssh-key", "archive-output", "report"):
        parser.add_argument("--" + option, required=True)
    parser.add_argument("--prestop-report")
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()
    with proof.create_private(args.report) as out:
        report = {"schema_version": 1, "status": "failed", "phase": "guard", "tested_at": datetime.now(timezone.utc)}
        interrupted = False
        try:
            asyncio.run(run(args, report))
        except KeyboardInterrupt:
            interrupted = True
            report["failure"] = "Interrupted; the local backup or stopped apps may need inspection"
        except Exception as exc:
            report["failure"] = str(exc) if isinstance(exc, proof.ProofError) else f"{type(exc).__name__} during stopped-app proof"
        json.dump(report, out, indent=2, sort_keys=True, default=proof.json_default)
        out.write("\n")
    if interrupted:
        raise SystemExit(130)
    if report["status"] != "passed":
        print(report["failure"])
        raise SystemExit(1)
    print("Stopped-app proof passed")


if __name__ == "__main__":
    main()
