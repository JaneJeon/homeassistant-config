"""Streaming validation of a Home Assistant backup and native snapshot exports."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import tarfile
from uuid import UUID


class ValidationError(ValueError):
    """The archive cannot be used as a verified recovery point."""


@dataclass(frozen=True)
class ValidationResult:
    backup_id: str
    created_at: datetime
    apps: dict
    paperless_file_count: int
    paperless_sha256: str
    components: tuple[str, ...]


_LIMIT = 8 * 1024 * 1024
_CHUNK = 1024 * 1024
_ENGINES = {
    "894a6dc5_victoria-metrics": ("victoriametrics", "1.153.0"),
    "894a6dc5_victoria-logs": ("victorialogs", "1.52.0"),
}


def _name(name):
    while name.startswith("./"):
        name = name[2:]
    name = name.rstrip("/")
    if not name or name.startswith("/") or any(x in ("", ".", "..") for x in name.split("/")):
        raise ValidationError("Invalid archive path")
    return name


def _json(fileobj):
    data = fileobj.read(_LIMIT + 1)
    if len(data) > _LIMIT:
        raise ValidationError("Archive metadata too large")
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise ValidationError("Invalid archive metadata") from exc
    if not isinstance(value, dict):
        raise ValidationError("Invalid archive metadata")
    return value


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|\+00:00)", value):
        raise ValidationError("Invalid UTC timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        raise ValidationError("Invalid UTC timestamp") from exc


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise ValidationError("Invalid snapshot identifier") from exc


def _hash(fileobj):
    h = hashlib.sha256()
    size = 0
    while chunk := fileobj.read(_CHUNK):
        size += len(chunk)
        h.update(chunk)
    return size, h.hexdigest()


def _app(tar, slug):
    engine, version = _ENGINES[slug]
    found = {}
    inventory = {}
    seen = set()
    partition_dirs = set()
    snapshot_dirs = set()
    part_lists = {}
    part_metadata = {}
    for member in tar:
        if member.isdir() and member.name.rstrip("/") in ("", "."):
            continue
        name = _name(member.name)
        if name in seen:
            raise ValidationError("Duplicate app archive member")
        seen.add(name)
        if name == "data/operation.json" or name in ("data/live", "data/work") or name.startswith(("data/live/", "data/work/")):
            raise ValidationError("App contains live data or a pending operation")
        if name.startswith("data/backup/payload/partitions/") and member.isdir():
            snapshot_dirs.add(name.removeprefix("data/backup/"))
            parts = name.split("/")
            if len(parts) == 5:
                partition_dirs.add(parts[4])
        if name in ("data/installation.json", "data/backup/manifest.json"):
            if not member.isfile():
                raise ValidationError("Invalid app metadata member")
            found[name] = _json(tar.extractfile(member))
        elif name == "data/backup/payload" or name.startswith("data/backup/payload/"):
            if member.isdir():
                continue
            if not member.isfile():
                raise ValidationError("Non-regular snapshot payload member")
            path = name.removeprefix("data/backup/")
            if engine == "victorialogs" and (re.fullmatch(r"payload/partitions/\d{8}/(?:indexdb|datadb)/parts\.json", path) or re.fullmatch(r"payload/partitions/\d{8}/(?:indexdb|datadb)/[0-9A-F]{16}/metadata\.json", path)):
                raw = tar.extractfile(member).read(_LIMIT + 1)
                if len(raw) > _LIMIT:
                    raise ValidationError("Partition metadata too large")
                inventory[path] = (len(raw), hashlib.sha256(raw).hexdigest())
                try:
                    parsed = json.loads(raw)
                    if path.endswith("/parts.json"):
                        part_lists[path] = parsed
                    else:
                        part_metadata[path] = parsed
                except (ValueError, UnicodeError) as exc:
                    raise ValidationError("Invalid partition part list") from exc
            else:
                inventory[path] = _hash(tar.extractfile(member))
        elif name.startswith("data/backup/") and not member.isdir():
            raise ValidationError("Unexpected snapshot member")
    try:
        install = found["data/installation.json"]
        manifest = found["data/backup/manifest.json"]
    except KeyError as exc:
        raise ValidationError("Missing app snapshot metadata") from exc
    if set(install) != {"schema_version", "engine", "engine_version", "installation_id"}:
        raise ValidationError("Invalid installation metadata fields")
    for obj in (install, manifest):
        if obj.get("schema_version") != 1 or obj.get("engine") != engine or obj.get("engine_version") != version:
            raise ValidationError("Incompatible app snapshot metadata")
        _uuid(obj.get("installation_id"))
    if install["installation_id"] != manifest["installation_id"]:
        raise ValidationError("App installation identifier mismatch")
    _uuid(manifest.get("generation"))
    timestamps = [manifest.get(k) for k in ("snapshot_started_at", "snapshot_completed_at", "export_completed_at")]
    if any(not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", value) for value in timestamps):
        raise ValidationError("Invalid canonical snapshot timestamp")
    started, completed, exported = map(_date, timestamps)
    if not started <= completed <= exported:
        raise ValidationError("Snapshot timestamps out of order")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValidationError("Empty snapshot inventory")
    expected = {}
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"} or not isinstance(entry.get("path"), str):
            raise ValidationError("Invalid snapshot inventory")
        path = _name(entry["path"])
        if path != entry["path"] or not path.startswith("payload/") or path in expected or not isinstance(entry.get("size"), int) or isinstance(entry["size"], bool) or entry["size"] < 0 or not isinstance(entry.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
            raise ValidationError("Invalid snapshot inventory")
        expected[path] = (entry["size"], entry["sha256"])
    if [entry["path"] for entry in files] != sorted(expected):
        raise ValidationError("Snapshot inventory is not sorted")
    if inventory != expected:
        raise ValidationError("Snapshot inventory mismatch")
    partitions = manifest.get("partitions")
    if not isinstance(partitions, list) or any(not isinstance(p, str) or not re.fullmatch(r"\d{8}", p) for p in partitions) or len(set(partitions)) != len(partitions):
        raise ValidationError("Invalid partition inventory")
    if engine == "victoriametrics" and partitions:
        raise ValidationError("Unexpected VictoriaMetrics partitions")
    if engine == "victorialogs":
        actual = {p.split("/")[2] for p in inventory if p.startswith("payload/partitions/") and len(p.split("/")) >= 4}
        if any(not p.startswith("payload/partitions/") or len(p.split("/")) < 4 for p in inventory) or set(partitions) != actual or not partition_dirs.issubset(set(partitions)):
            raise ValidationError("VictoriaLogs partition inventory mismatch")
        for partition in partitions:
            for db in ("indexdb", "datadb"):
                base = f"payload/partitions/{partition}/{db}"
                listed = part_lists.get(f"{base}/parts.json")
                if not isinstance(listed, list) or any(not isinstance(part, str) or not re.fullmatch(r"[0-9A-F]{16}", part) for part in listed) or len(set(listed)) != len(listed):
                    raise ValidationError("Invalid or missing VictoriaLogs part list")
                prefix = base + "/"
                actual_parts = {p[len(prefix):].split("/")[0] for p in inventory if p.startswith(prefix) and p != prefix + "parts.json"}
                actual_parts |= {p[len(prefix):].split("/")[0] for p in snapshot_dirs if p.startswith(prefix)}
                if set(listed) != actual_parts:
                    raise ValidationError("VictoriaLogs part inventory mismatch")
                required = {"metadata.json", "index.bin", "metaindex.bin"}
                if db == "indexdb":
                    required |= {"items.bin", "lens.bin"}
                else:
                    required |= {"columns_header.bin", "timestamps.bin", "message_bloom.bin", "message_values.bin"}
                for part in listed:
                    metadata = part_metadata.get(f"{prefix}{part}/metadata.json")
                    if not isinstance(metadata, dict):
                        raise ValidationError("Invalid VictoriaLogs part metadata")
                    part_required = set(required)
                    if db == "datadb":
                        part_format_version = metadata.get("FormatVersion", 0)
                        shards = metadata.get("BloomValuesShardsCount", 0)
                        if type(part_format_version) is not int or part_format_version not in range(4) or type(shards) is not int or not 0 <= shards <= 10000:
                            raise ValidationError("Invalid VictoriaLogs part format")
                        if part_format_version >= 1:
                            part_required |= {"column_names.bin", "columns_header_index.bin"}
                            for shard in range(shards):
                                part_required |= {f"bloom.bin{shard}", f"values.bin{shard}"}
                        else:
                            part_required |= {"bloom.bin", "values.bin"}
                        if part_format_version >= 3:
                            part_required.add("column_idxs.bin")
                    if not all(f"{prefix}{part}/{filename}" in inventory for filename in part_required):
                        raise ValidationError("Incomplete VictoriaLogs part")
    return {"engine": engine, "engine_version": version, "installation_id": install["installation_id"], "generation": manifest["generation"], "snapshot_started_at": started, "snapshot_completed_at": completed, "export_completed_at": exported, "partitions": partitions, "file_count": len(inventory)}


def _share(tar):
    entries = {}
    seen = set()
    for member in tar:
        if member.isdir() and member.name.rstrip("/") in ("", "."):
            continue
        name = _name(member.name)
        if name in seen:
            raise ValidationError("Duplicate Share archive member")
        seen.add(name)
        relative = name.removeprefix("share/")
        if relative in ("paperless", "paperless/media") and not member.isdir():
            raise ValidationError("Invalid Paperless media directory")
        if relative.startswith("paperless/media/"):
            if member.isdir():
                continue
            if not member.isfile():
                raise ValidationError("Non-regular Paperless media member")
            _, digest = _hash(tar.extractfile(member))
            entries[f"/share/{relative}"] = digest
    if not entries:
        raise ValidationError("Paperless media is missing or empty")
    lines = "".join(f"{digest}  {name}\n" for name, digest in sorted(entries.items()))
    return len(entries), hashlib.sha256(lines.encode()).hexdigest()


def _generic(tar):
    """Consume every regular member, recording whether component data exists."""
    seen = set()
    has_data = False
    for member in tar:
        if member.isdir() and member.name.rstrip("/") in ("", "."):
            continue
        name = _name(member.name)
        if name in seen:
            raise ValidationError("Duplicate component archive member")
        seen.add(name)
        if member.isfile():
            _hash(tar.extractfile(member))
            if name.startswith("data/"):
                has_data = True
    return has_data


def _declared_components(metadata):
    addons, folders = metadata.get("addons"), metadata.get("folders")
    if not isinstance(addons, list) or not isinstance(folders, list):
        raise ValidationError("Invalid backup component listing")
    app_slugs = []
    for addon in addons:
        if not isinstance(addon, dict) or not isinstance(addon.get("slug"), str):
            raise ValidationError("Invalid backup app listing")
        slug = addon["slug"]
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", slug):
            raise ValidationError("Invalid backup app slug")
        app_slugs.append(slug)
    if len(set(app_slugs)) != len(app_slugs):
        raise ValidationError("Duplicate backup app listing")
    folder_slugs = []
    for folder in folders:
        if not isinstance(folder, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", folder):
            raise ValidationError("Invalid backup folder listing")
        folder_slugs.append(folder.replace("/", "_"))
    if len(set(folder_slugs)) != len(folder_slugs):
        raise ValidationError("Duplicate backup folder listing")
    required_apps = set(_ENGINES) | {"ca5234a0_paperless-ngx"}
    if not required_apps.issubset(app_slugs) or "share" not in folder_slugs or not isinstance(metadata.get("homeassistant"), dict):
        raise ValidationError("Required components absent from backup metadata")
    if metadata.get("type") not in ("full", "partial"):
        raise ValidationError("Invalid backup type")
    return set(app_slugs) | set(folder_slugs) | {"homeassistant"}


class _TailReader:
    def __init__(self, source):
        self.source = source
        self.tail = b""

    def read(self, size=-1):
        chunk = self.source.read(size)
        self.tail = (self.tail + chunk)[-1024:]
        return chunk


def validate_archive(stream, *, password, expected_backup_id, expected_slugs):
    """Validate a sequential outer tar; raise ValidationError on any failed check."""
    # Supervisor stores a plain outer tar and individually encrypted inner tars.
    from contextlib import nullcontext
    from securetar import SecureTarDecryptStream, SecureTarRootKeyContext

    try:
        expected_slugs = tuple(expected_slugs)
        if len(expected_slugs) != len(_ENGINES) or set(expected_slugs) != set(_ENGINES):
            raise ValidationError("Both native snapshot apps are required")
        metadata = None
        apps = {}
        paperless = None
        seen = set()
        components = {}
        tracked = _TailReader(stream)
        with tarfile.open(fileobj=tracked, mode="r|") as outer:
            for member in outer:
                name = _name(member.name)
                if name in seen:
                    raise ValidationError("Duplicate outer archive member")
                seen.add(name)
                if name == "backup.json":
                    if not member.isfile():
                        raise ValidationError("Invalid backup metadata member")
                    metadata = _json(outer.extractfile(member))
                    continue
                if name.endswith(".tar.gz"):
                    component = name[:-7]
                elif name.endswith(".tar"):
                    component = name[:-4]
                else:
                    continue
                if "/" in component or not component:
                    raise ValidationError("Invalid backup component name")
                target = component if component in expected_slugs else None
                share = component == "share"
                if not member.isfile():
                    raise ValidationError("Invalid backup component")
                if component in components:
                    raise ValidationError("Duplicate backup component")
                source = outer.extractfile(member)
                decrypt = (SecureTarDecryptStream(source, ciphertext_size=member.size, root_key_context=SecureTarRootKeyContext(password))
                           if password else nullcontext(source))
                with decrypt as plaintext:
                    with tarfile.open(fileobj=plaintext, mode="r|gz" if name.endswith(".gz") else "r|") as inner:
                        if share:
                            paperless = _share(inner)
                        elif target:
                            apps[target] = _app(inner, target)
                        else:
                            components[component] = _generic(inner)
                    if share or target:
                        components[component] = True
                    # Tar readers can stop before the cipher's authenticated final frame.
                    while plaintext.read(_CHUNK):
                        pass
        while tracked.read(_CHUNK):
            pass
        if len(tracked.tail) != 1024 or tracked.tail != bytes(1024):
            raise ValidationError("Incomplete outer archive")
        if metadata is None or metadata.get("slug") != expected_backup_id:
            raise ValidationError("Backup identifier mismatch or metadata missing")
        declared = _declared_components(metadata)
        if not declared.issubset(components):
            raise ValidationError("Declared or required backup component missing")
        if not components.get("homeassistant") or not components.get("ca5234a0_paperless-ngx"):
            raise ValidationError("Home Assistant or Paperless application data missing")
        created = _date(metadata.get("date"))
        if set(apps) != set(expected_slugs) or paperless is None:
            raise ValidationError("Required backup component missing")
        if any(app["snapshot_started_at"] < created for app in apps.values()):
            raise ValidationError("Snapshot predates backup")
        return ValidationResult(expected_backup_id, created, apps, paperless[0], paperless[1], tuple(sorted(seen)))
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError("Cannot read or decrypt backup archive") from exc
