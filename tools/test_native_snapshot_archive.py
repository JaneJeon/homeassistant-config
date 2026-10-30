"""Recovery-point validation against real SecureTar archives."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile

import pytest
from securetar import SecureTarArchive

spec = importlib.util.spec_from_file_location("snapshot_archive", Path(__file__).parents[1] / "custom_components/native_snapshot_backups/archive.py")
archive = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = archive
spec.loader.exec_module(archive)
SLUGS = ("894a6dc5_victoria-metrics", "894a6dc5_victoria-logs")
IDENT = "c1ba08cc-780e-4f15-a2a1-909684c806af"
GENERATION = "874e335a-ac3f-47f9-982f-a90e4577f103"


def add(tar, name, value, kind=None):
    info = tarfile.TarInfo(name)
    if kind:
        info.type = kind
        info.linkname = "elsewhere"
        tar.addfile(info)
    else:
        info.size = len(value)
        tar.addfile(info, io.BytesIO(value))


def make(version=3, flaw=None):
    output = io.BytesIO()
    with SecureTarArchive(fileobj=output, mode="w", password="test password", create_version=version) as outer:
        for slug, engine, ver in zip(SLUGS, ("victoriametrics", "victorialogs"), ("1.153.0", "1.52.0")):
            if flaw == "missing" and slug == SLUGS[1]:
                continue
            payload_path = "payload/index"
            content = b"snapshot contents"
            payloads = {payload_path: content}
            if engine == "victorialogs":
                payloads = {}
                for db, files in (("indexdb", ("metadata.json", "index.bin", "metaindex.bin", "items.bin", "lens.bin")), ("datadb", ("metadata.json", "index.bin", "metaindex.bin", "columns_header.bin", "timestamps.bin", "message_bloom.bin", "message_values.bin", "column_names.bin", "columns_header_index.bin", "column_idxs.bin"))):
                    base = f"payload/partitions/20261030/{db}"
                    payloads[f"{base}/parts.json"] = b'["ABCDEF0123456789"]'
                    for filename in files:
                        payloads[f"{base}/ABCDEF0123456789/{filename}"] = b'{"FormatVersion":3,"BloomValuesShardsCount":0}' if filename == "metadata.json" else content
            if engine == "victorialogs" and flaw == "missing_parts":
                del payloads["payload/partitions/20261030/datadb/parts.json"]
            if engine == "victorialogs" and flaw == "missing_partfile":
                del payloads["payload/partitions/20261030/datadb/ABCDEF0123456789/index.bin"]
            if engine == "victorialogs" and flaw == "unlisted_part":
                payloads["payload/partitions/20261030/datadb/1111111111111111/index.bin"] = content
            install = dict(schema_version=1, engine=engine, engine_version=ver, installation_id=IDENT)
            manifest = dict(**install, generation=GENERATION, snapshot_started_at="2026-10-30T12:00:01.000000Z", snapshot_completed_at="2026-10-30T12:00:02.000000Z", export_completed_at="2026-10-30T12:00:03.000000Z", partitions=["20261030"] if engine == "victorialogs" else [], files=[dict(path=path, size=len(data), sha256=hashlib.sha256(data).hexdigest()) for path, data in sorted(payloads.items())])
            if flaw == "stale": manifest["snapshot_started_at"] = "2026-10-30T11:59:59.000000Z"
            if flaw == "order": manifest["export_completed_at"] = "2026-10-30T12:00:00.000000Z"
            if flaw == "partition" and engine == "victorialogs": manifest["partitions"] = ["20261029"]
            if flaw == "install_extra": install["unexpected"] = True
            if flaw == "uuid_upper": install["installation_id"] = manifest["installation_id"] = IDENT.upper()
            if flaw == "timestamp_no_fraction": manifest["snapshot_started_at"] = "2026-10-30T12:00:01Z"
            if flaw == "timestamp_offset": manifest["snapshot_started_at"] = "2026-10-30T12:00:01.000000+00:00"
            if flaw == "file_extra": manifest["files"][0]["unexpected"] = True
            if flaw == "file_unsorted" and engine == "victorialogs": manifest["files"].reverse()
            with outer.create_tar(f"{slug}.tar.gz") as inner:
                add(inner, "data/installation.json", json.dumps(install).encode())
                for path, data in payloads.items():
                    add(inner, "data/backup/" + path, b"bad" if flaw == "corrupt" else data)
                if flaw == "symlink": add(inner, "data/backup/payload/link", b"", tarfile.SYMTYPE)
                if flaw == "traversal": add(inner, "data/backup/payload/../outside", b"bad")
                if flaw == "duplicate":
                    first_path = next(iter(payloads))
                    add(inner, "data/backup/" + first_path, payloads[first_path])
                if flaw == "journal": add(inner, "data/operation.json", b"{}")
                # Manifest intentionally follows payload to exercise a streaming reader.
                add(inner, "data/backup/manifest.json", json.dumps(manifest).encode())
        for component in ("homeassistant", "supervisor", "ca5234a0_paperless-ngx"):
            if flaw == "missing_" + component:
                continue
            if flaw == "corrupt_generic" and component == "supervisor":
                add(outer.tar, component + ".tar.gz", b"corrupt encrypted data")
                continue
            with outer.create_tar(component + ".tar.gz") as inner:
                if component == "supervisor":
                    add(inner, "mounts.json", b"{}")
                elif flaw == "empty_homeassistant" and component == "homeassistant":
                    add(inner, "homeassistant.json", b"{}")
                else:
                    add(inner, "data/configuration.yaml" if component == "homeassistant" else "data/db.sqlite3", b"fixture")
        with outer.create_tar("share.tar.gz") as share:
            add(share, "share/paperless/media/a.txt", b"paperless")
        addons = [dict(slug=slug, name=slug, version="1.0.0") for slug in (*SLUGS, "ca5234a0_paperless-ngx")]
        if flaw == "listed_missing_addon":
            addons.append(dict(slug="abcdef01_extra", name="Extra", version="1.0.0"))
        if flaw == "listed_missing_folder":
            folders = ["share", "ssl"]
        else:
            folders = ["share"]
        metadata = dict(slug="example", name="Fixture", type="partial", date="2026-10-30T12:00:00Z", protected=True, compressed=True, homeassistant=dict(version="2026.10.4"), addons=addons, folders=folders)
        if flaw == "missing_supervisor": metadata["date"] = "2026-10-30T12:00:00+00:00"
        add(outer.tar, "backup.json", json.dumps(metadata).encode())
    return output.getvalue()


def validate(data, password="test password"):
    return archive.validate_archive(io.BytesIO(data), password=password, expected_backup_id="example", expected_slugs=SLUGS)


@pytest.mark.parametrize("version", [2, 3])
def test_securetar_versions(version):
    result = validate(make(version))
    assert result.backup_id == "example"
    assert result.paperless_file_count == 1
    expected = hashlib.sha256(b"paperless").hexdigest() + "  /share/paperless/media/a.txt\n"
    assert result.paperless_sha256 == hashlib.sha256(expected.encode()).hexdigest()
    assert result.apps[SLUGS[1]]["partitions"] == ["20261030"]


@pytest.mark.parametrize("flaw", ["missing", "stale", "order", "partition", "corrupt", "symlink", "traversal", "duplicate", "journal", "missing_parts", "missing_partfile", "unlisted_part", "missing_homeassistant", "missing_ca5234a0_paperless-ngx", "listed_missing_addon", "listed_missing_folder", "corrupt_generic", "empty_homeassistant", "install_extra", "uuid_upper", "timestamp_no_fraction", "timestamp_offset", "file_extra", "file_unsorted"])
def test_rejects_bad_snapshot(flaw):
    with pytest.raises(archive.ValidationError):
        validate(make(flaw=flaw))


def test_optional_supervisor_archive():
    result = validate(make(flaw="missing_supervisor"))
    assert result.backup_id == "example"


def test_wrong_key_and_truncated_archive():
    data = make()
    with pytest.raises(archive.ValidationError):
        validate(data, "wrong password")
    with pytest.raises(archive.ValidationError):
        validate(data[:len(data)//2])
    with pytest.raises(archive.ValidationError):
        validate(data[:-10240])
