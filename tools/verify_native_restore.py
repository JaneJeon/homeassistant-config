#!/usr/bin/env python3
"""Capture baseline evidence and prove a native backup in an isolated HAOS lab."""
from __future__ import annotations

import argparse
import dataclasses
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess
import sys
import tarfile
import time
from urllib.parse import urlencode, quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

SLUGS = ("894a6dc5_victoria-metrics", "894a6dc5_victoria-logs")
CHUNK = 1024 * 1024


class ProofError(Exception):
    pass


def private_file(path):
    info = Path(path).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ProofError("Secret file must be a private regular file (0600)")
    content = Path(path).read_text().removesuffix("\n").removesuffix("\r")
    if not content:
        raise ProofError("Secret file is empty")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        parsed = content
    if not isinstance(parsed, str):
        parsed = content
    if not parsed or "\n" in parsed or "\r" in parsed:
        raise ProofError("Secret must be a single-line string")
    return parsed


def create_private(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    return os.fdopen(fd, "w", encoding="utf-8")


def json_write(path, value):
    with create_private(path) as f:
        json.dump(value, f, indent=2, sort_keys=True, default=json_default)
        f.write("\n")


def json_default(value):
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    raise TypeError("Unsupported JSON value")


def load_validator():
    path = Path(__file__).resolve().parents[1] / "custom_components/native_snapshot_backups/archive.py"
    spec = importlib.util.spec_from_file_location("native_snapshot_archive_proof", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def validate(path, key, backup_id):
    validator = load_validator()
    with open(path, "rb") as stream:
        return validator.validate_archive(stream, password=key, expected_backup_id=backup_id, expected_slugs=SLUGS)


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def backup_id_from_archive(path):
    with tarfile.open(path, "r|") as outer:
        for member in outer:
            if member.name.removeprefix("./") == "backup.json" and member.isfile():
                raw = outer.extractfile(member).read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    break
                result = json.loads(raw).get("slug")
                if isinstance(result, str) and re.fullmatch(r"[a-zA-Z0-9_-]+", result):
                    return result
                break
    raise ProofError("Archive backup identifier is missing or invalid")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download(args):
    if not re.fullmatch(r"backblaze_b2\.[a-zA-Z0-9_-]+", args.agent_id):
        raise ProofError("Backup agent must be a Backblaze B2 agent")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", args.backup_id):
        raise ProofError("Invalid backup identifier")
    parsed = urlsplit(args.ha_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProofError("Invalid Home Assistant URL")
    token = private_file(args.token_file)
    url = args.ha_url.rstrip("/") + "/api/backup/download/" + quote(args.backup_id, safe="") + "?" + urlencode({"agent_id": args.agent_id})
    request = Request(url, headers={"Authorization": "Bearer " + token})
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as out, build_opener(_NoRedirect).open(request, timeout=60) as response:
            if response.status != 200:
                raise ProofError("Backup download failed")
            h = hashlib.sha256()
            total = 0
            while chunk := response.read(CHUNK):
                out.write(chunk)
                h.update(chunk)
                total += len(chunk)
            length = response.headers.get("Content-Length")
            if length is not None and total != int(length):
                raise ProofError("Backup download was incomplete")
    except BaseException:
        Path(args.output).unlink(missing_ok=True)
        raise
    print(json.dumps({"backup_id": args.backup_id, "bytes": total, "sha256": h.hexdigest()}))


def request_json(base, path, params):
    url = base.rstrip("/") + path + "?" + urlencode(params)
    with urlopen(Request(url), timeout=30) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ProofError("Query result exceeds size limit")
    return raw.decode("utf-8")


def normalized(kind, raw):
    if kind == "metrics":
        data = json.loads(raw)
        if data.get("status") != "success" or data.get("data", {}).get("resultType") != "vector":
            raise ProofError("Invalid VictoriaMetrics query response")
        result = data["data"].get("result")
        if not isinstance(result, list) or len(result) > 1:
            raise ProofError("Unexpected VictoriaMetrics result")
        if not result:
            return None
        value = result[0]["value"][1]
    else:
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        if len(rows) > 1:
            raise ProofError("Unexpected VictoriaLogs result")
        if not rows:
            return None
        value = rows[0].get("rows")
    try:
        from decimal import Decimal, InvalidOperation
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise ValueError
        return format(number.normalize(), "f")
    except (ValueError, InvalidOperation) as exc:
        raise ProofError("Invalid query count") from exc


def capture(args):
    hour = int(time.time() // 3600 * 3600)
    queries = []
    for days in (1, 7, 30):
        params = {"query": "sum(count_over_time(up[1h]))", "time": str(hour - days * 86400)}
        raw = request_json(args.metrics_url, "/api/v1/query", params)
        value = normalized("metrics", raw)
        if value in (None, "0") and days != 30:
            raise ProofError(f"No VictoriaMetrics baseline data {days} days ago")
        queries.append({"kind": "metrics", "days_ago": days, "path": "/api/v1/query", "params": params, "expected": value, "skipped": value in (None, "0")})
    for days in (1, 7):
        end = hour - days * 86400
        params = {"query": "* | stats count() as rows", "start": datetime.fromtimestamp(end - 3600, timezone.utc).isoformat().replace("+00:00", "Z"), "end": datetime.fromtimestamp(end, timezone.utc).isoformat().replace("+00:00", "Z")}
        raw = request_json(args.logs_url, "/select/logsql/query", params)
        value = normalized("logs", raw)
        if value in (None, "0"):
            raise ProofError(f"No VictoriaLogs baseline data {days} days ago")
        queries.append({"kind": "logs", "days_ago": days, "path": "/select/logsql/query", "params": params, "expected": value, "skipped": False})
    json_write(args.output, {"schema_version": 1, "captured_at": datetime.now(timezone.utc), "metrics_url": args.metrics_url, "logs_url": args.logs_url, "queries": queries})
    print("Baseline expectations captured")


def qmp_status(root):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(str(root / "qmp.sock"))
        f = s.makefile("rwb", buffering=0)
        json.loads(f.readline())
        f.write(b'{"execute":"qmp_capabilities"}\n')
        json.loads(f.readline())
        f.write(b'{"execute":"query-status"}\n')
        while line := f.readline():
            value = json.loads(line)
            if "return" in value:
                return value["return"].get("status")
            if "error" in value:
                break
    raise ProofError("Cannot verify lab QMP state")


def guard_lab(root):
    state = json.loads((root / "state.json").read_text())
    if state.get("restore_locked") is not True or state.get("last_network") != "isolated" or qmp_status(root) != "running":
        raise ProofError("Lab must be running, isolated, and permanently restore-locked")
    pid = int((root / "qemu.pid").read_text().strip())
    result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=10, check=True)
    command = result.stdout
    if "qemu-system-aarch64" not in command or "restrict=on" not in command or "hostfwd=tcp:127.0.0.1:22222-:22222" not in command or str(root / "disk.qcow2") not in command:
        raise ProofError("Running VM does not have the required isolated network and lab disk")


def ssh_base(root, key):
    info = Path(key).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ProofError("SSH key must be a private regular file")
    known = root / "restore-known-hosts"
    fd = os.open(known, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(fd)
    if known.stat().st_mode & 0o077:
        raise ProofError("Lab known-hosts file is not private")
    return ["ssh", "-F", "/dev/null", "-p", "22222", "-i", str(key), "-o", "ProxyCommand=none", "-o", "ClearAllForwardings=yes", "-o", "ControlMaster=no", "-o", "IdentityAgent=none", "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={known}", "-o", "GlobalKnownHostsFile=/dev/null", "root@127.0.0.1"]


def ssh(base, command, input_bytes=None, timeout=30):
    result = subprocess.run(base + [command], input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        raise ProofError("Lab SSH command failed")
    return result.stdout.decode("utf-8", errors="strict")


def probes(base):
    if ssh(base, "command -v curl >/dev/null && echo READY").strip() != "READY":
        raise ProofError("Guest curl is unavailable for isolation probes")
    results = {}
    for name, url in (("lan_router", "http://10.0.0.1/"), ("public_dns", "http://example.com/"), ("public_ip", "http://1.1.1.1/")):
        command = "ip=$(curl --noproxy '*' --connect-timeout 4 --max-time 6 -sS -o /dev/null -w '%{remote_ip}' " + shlex.quote(url) + " 2>/dev/null || :); if [ -n \"$ip\" ]; then echo REACHABLE; else echo BLOCKED; fi"
        value = ssh(base, command, timeout=15).strip()
        if value not in ("BLOCKED", "REACHABLE"):
            raise ProofError("Isolation probe did not produce a valid result")
        results[name] = value == "BLOCKED"
    if not all(results.values()):
        raise ProofError("Lab isolation probe failed")
    return results


def query_guest(base, query):
    port = 8428 if query["kind"] == "metrics" else 9428
    url = f"http://127.0.0.1:{port}" + query["path"] + "?" + urlencode(query["params"])
    result = ssh(base, "curl --noproxy '*' -fsS --connect-timeout 5 --max-time 30 " + shlex.quote(url), timeout=40)
    return normalized(query["kind"], result)


def guest_json(base, command):
    raw = ssh(base, command + " --raw-json", timeout=30)
    data = json.loads(raw)
    if data.get("result") == "error":
        raise ProofError("Home Assistant CLI reported an error")
    return data.get("data", data)


def restore(args):
    # Reserve the evidence file before touching the guest, including on failures.
    with create_private(args.report) as report_file:
        report = {"schema_version": 1, "status": "failed", "tested_at": datetime.now(timezone.utc)}
        try:
            root = Path(args.lab_dir).resolve()
            guard_lab(root)
            key = private_file(args.key_file)
            backup_id = backup_id_from_archive(args.archive)
            report["backup_id"] = backup_id
            report["source_archive_sha256"] = digest(args.archive)
            result = validate(args.archive, key, backup_id)
            report["archive_validation"] = dataclasses.asdict(result)
            expectations = json.loads(Path(args.expectations).read_text())
            if expectations.get("schema_version") != 1 or not isinstance(expectations.get("queries"), list):
                raise ProofError("Invalid historical expectations")
            queries = expectations["queries"]
            expected_keys = {(kind, days) for kind, days in (("metrics", 1), ("metrics", 7), ("metrics", 30), ("logs", 1), ("logs", 7))}
            if len(queries) != len(expected_keys) or {(q.get("kind"), q.get("days_ago")) for q in queries} != expected_keys:
                raise ProofError("Historical expectations are incomplete")
            for query in queries:
                if query.get("path") != ("/api/v1/query" if query["kind"] == "metrics" else "/select/logsql/query") or not isinstance(query.get("params"), dict):
                    raise ProofError("Invalid historical query")
                if query.get("skipped") and (query["kind"], query["days_ago"]) != ("metrics", 30):
                    raise ProofError("A required historical query cannot be skipped")
                if not query.get("skipped") and query.get("expected") in (None, "0"):
                    raise ProofError("Historical expectation is empty")
            base = ssh_base(root, args.ssh_key)
            report["phase"] = "isolation_before_upload"
            report["isolation_before_upload"] = probes(base)
            print("Lab isolation verified; uploading encrypted backup", flush=True)
            # Backup ID is restricted to a filename-safe alphabet above.
            with open(args.archive, "rb") as artifact:
                completed = subprocess.run(base + [f"umask 077; set -C; cat > /mnt/data/supervisor/backup/{backup_id}.tar"], stdin=artifact, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=3600)
            if completed.returncode:
                raise ProofError("Encrypted archive upload failed")
            uploaded = ssh(base, f"sha256sum /mnt/data/supervisor/backup/{backup_id}.tar | cut -d' ' -f1", timeout=3600).strip()
            if uploaded != report["source_archive_sha256"]:
                raise ProofError("Uploaded encrypted archive hash differs from source")
            report["uploaded_sha256"] = uploaded
            ssh(base, "ha backups reload >/dev/null", timeout=120)
            print("Starting full restore", flush=True)
            report["phase"] = "restore"
            marker = f"/mnt/data/native-restore-{backup_id}.exit"
            log = f"/mnt/data/native-restore-{backup_id}.log"
            command = (f'test ! -e {marker} && test ! -e {log} || exit 1; IFS= read -r key; '
                       f'( ha backups restore {backup_id} --password "$key" --raw-json --no-progress >{log} 2>&1; '
                       f'printf "%s\\n" "$?" >{marker}.tmp; mv {marker}.tmp {marker} ) </dev/null >/dev/null 2>&1 &')
            ssh(base, command, input_bytes=(key + "\n").encode(), timeout=120)
            del key
            deadline = time.monotonic() + args.timeout
            ready = False
            finished = False
            while time.monotonic() < deadline:
                try:
                    if not finished:
                        status = ssh(base, f"if [ -f {marker} ]; then cat {marker}; else echo PENDING; fi").strip()
                        if status != "PENDING":
                            if status != "0":
                                raise ProofError("Home Assistant restore command failed")
                            finished = True
                        else:
                            time.sleep(10)
                            continue
                    core = guest_json(base, "ha core info")
                    apps = {slug: guest_json(base, "ha apps info " + slug) for slug in (*SLUGS, "ca5234a0_paperless-ngx")}
                    core_state = ssh(base, "docker inspect --format '{{.State.Status}}' homeassistant").strip()
                    if core_state == "running" and all(app.get("state") in ("started", "running") for app in apps.values()):
                        ready = True
                        break
                except ProofError as exc:
                    if str(exc) == "Home Assistant restore command failed":
                        raise
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
                time.sleep(10)
            if not ready:
                raise ProofError("Restore did not reach running Core and app states before timeout")
            report["core"] = {key: core.get(key) for key in ("version", "image")}
            report["core"]["state"] = core_state
            report["apps"] = {slug: {key: app.get(key) for key in ("state", "version", "image")} for slug, app in apps.items()}
            report["isolation_after_restore"] = probes(base)
            report["phase"] = "historical_queries"
            comparisons = []
            report["historical_queries"] = comparisons
            for query in queries:
                if query["skipped"]:
                    comparisons.append({**query, "actual": None, "matches": None})
                    continue
                actual = query_guest(base, query)
                comparisons.append({**query, "actual": actual, "matches": actual == query["expected"]})
            if any(item["matches"] is False for item in comparisons):
                raise ProofError("Restored historical data did not match the baseline")
            # The manifest format is sha256 + two spaces + /share/path + newline,
            # sorted by path, matching the archive validator's Paperless summary.
            report["phase"] = "paperless_inventory"
            media = "/mnt/data/supervisor/share/paperless/media"
            script = f'''set -e; export LC_ALL=C; cd {media}; count=$(find . -type f | wc -l | tr -d ' '); digest=$(find . -type f | sort | while IFS= read -r f; do sha256sum "$f" | sed 's|  \\./|  /share/paperless/media/|'; done | sha256sum | cut -d' ' -f1); printf '%s %s\\n' "$count" "$digest"'''
            output = ssh(base, script, timeout=180).strip().split()
            if len(output) != 2 or not output[0].isdigit() or not re.fullmatch(r"[0-9a-f]{64}", output[1]):
                raise ProofError("Invalid restored Paperless inventory")
            report["paperless"] = {"file_count": int(output[0]), "sha256": output[1], "matches": int(output[0]) == result.paperless_file_count and output[1] == result.paperless_sha256}
            if not report["paperless"]["matches"]:
                raise ProofError("Restored Paperless media differs from archive")
            report["phase"] = "complete"
            report["status"] = "passed"
        except Exception as exc:
            report["failure"] = str(exc) if isinstance(exc, ProofError) else f"{type(exc).__name__} during restore proof"
        json.dump(report, report_file, indent=2, sort_keys=True, default=json_default)
        report_file.write("\n")
    if report["status"] != "passed":
        raise ProofError(report["failure"])
    print("Restore proof passed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    dl = sub.add_parser("download")
    for option in ("ha-url", "token-file", "agent-id", "backup-id", "output"):
        dl.add_argument("--" + option, required=True)
    check = sub.add_parser("validate")
    for option in ("archive", "key-file", "backup-id"):
        check.add_argument("--" + option, required=True)
    cap = sub.add_parser("capture")
    for option in ("metrics-url", "logs-url", "output"):
        cap.add_argument("--" + option, required=True)
    run = sub.add_parser("restore")
    for option in ("lab-dir", "archive", "key-file", "ssh-key", "expectations", "report"):
        run.add_argument("--" + option, required=True)
    run.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()
    try:
        if args.action == "download":
            download(args)
        elif args.action == "validate":
            result = validate(args.archive, private_file(args.key_file), args.backup_id)
            print(json.dumps(dataclasses.asdict(result), default=json_default, sort_keys=True))
        elif args.action == "capture":
            capture(args)
        else:
            restore(args)
    except Exception as exc:
        # Avoid printing HTTP/SSH exception text, which can contain sensitive URLs.
        print(str(exc) if isinstance(exc, ProofError) else f"{type(exc).__name__}: operation failed", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
