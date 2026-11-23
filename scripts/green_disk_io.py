#!/usr/bin/env python3
"""Sample host I/O pressure and the disk backing Home Assistant's config."""

import json
import os
from pathlib import Path
import tempfile
import time


STATE = Path('/dev/shm/green_disk_io.json')


def pressure():
    values = {'some_pct': None, 'full_pct': None}
    try:
        for line in Path('/proc/pressure/io').read_text().splitlines():
            parts = line.split()
            if parts and parts[0] in ('some', 'full'):
                fields = dict(item.split('=', 1) for item in parts[1:] if '=' in item)
                try:
                    values[parts[0] + '_pct'] = float(fields['avg300'])
                except (ValueError, KeyError):
                    pass
    except OSError:
        pass
    return values


def disk_path():
    dev = os.stat('/config').st_dev
    path = Path('/sys/dev/block') / f'{os.major(dev)}:{os.minor(dev)}'
    path = path.resolve(strict=True)
    if (path / 'partition').exists():
        path = path.parent
    return path


def rates(previous, current):
    result = dict.fromkeys(('read_mib_s', 'write_mib_s', 'read_ms', 'write_ms'))
    try:
        elapsed = current['monotonic'] - previous['monotonic']
        if previous['identity'] != current['identity'] or not 0 < elapsed <= 120:
            return result
        delta = [a - b for a, b in zip(current['counters'], previous['counters'], strict=True)]
        if len(delta) != 6 or any(x < 0 for x in delta):
            return result
        reads, read_sectors, read_time, writes, write_sectors, write_time = delta
        result['read_mib_s'] = read_sectors * 512 / 1048576 / elapsed
        result['write_mib_s'] = write_sectors * 512 / 1048576 / elapsed
        result['read_ms'] = read_time / reads if reads else None
        result['write_ms'] = write_time / writes if writes else None
    except (KeyError, TypeError, ValueError):
        pass
    return result


def save_state(state):
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=STATE.parent, prefix='.green_disk_io.', delete=False) as output:
            name = output.name
            json.dump(state, output)
        os.replace(name, STATE)
    except OSError:
        if name:
            try:
                os.unlink(name)
            except OSError:
                pass


def main():
    result = {'sampled_at': time.time(), **pressure(), 'device': None,
              'read_mib_s': None, 'write_mib_s': None, 'read_ms': None, 'write_ms': None}
    try:
        path = disk_path()
        result['device'] = path.name
        # Linux block stat sectors are always 512 bytes.
        fields = [int(x) for x in (path / 'stat').read_text().split()]
        counters = [fields[i] for i in (0, 2, 3, 4, 6, 7)]
        try:
            diskseq = (path / 'diskseq').read_text().strip()
        except OSError:
            diskseq = None
        current = {'identity': [str(path), diskseq], 'counters': counters, 'monotonic': time.monotonic()}
        try:
            previous = json.loads(STATE.read_text())
            result.update(rates(previous, current))
        except (OSError, ValueError):
            pass
        save_state(current)
    except (OSError, ValueError, IndexError):
        pass
    print(json.dumps(result, allow_nan=False))


if __name__ == '__main__':
    main()
