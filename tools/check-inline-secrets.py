#!/usr/bin/env python3
"""Fail if Home Assistant config in this repo holds a secret inline.

gitleaks catches known token formats. This catches the HA-specific case it
cannot: a credential-shaped key (password, token, api_key, Authorization...)
whose value is written inline instead of `!secret some_name`. Real values live
only in secrets.yaml on the Green, which is never committed.

Usage: tools/check-inline-secrets.py            (all tracked files)
       tools/check-inline-secrets.py --staged   (files staged for commit)
"""
import re
import subprocess
import sys

SENSITIVE_KEYS = {
    "password", "passwd", "pass", "passphrase", "pin",
    "token", "access_token", "refresh_token", "api_token", "bearer_token",
    "api_key", "apikey", "app_key", "client_secret", "secret", "secret_key",
    "private_key", "authorization", "x-api-key", "x-auth-token",
}
# Lines like `password: !secret foo`, `token: "{{ states('input_text.x') }}"`,
# or an empty value are fine.
ALLOWED_VALUE = re.compile(r"""^(!secret\s+\S+|["']?\{\{.*\}\}["']?|""|''|~|null)?$""")
KEY_LINE = re.compile(r"""^\s*(?:-\s+)?["']?([A-Za-z_][\w-]*)["']?\s*:\s*(.*?)\s*(?:#.*)?$""")
URL_CREDS = re.compile(r"://[^/\s:@'\"]+:[^/\s@'\"]+@")
SKIP_FILES = {"secrets.fake.yaml"}


def tracked_files(staged: bool) -> list[str]:
    cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"] if staged else ["git", "ls-files"]
    return [f for f in subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.splitlines() if f]


def main() -> int:
    staged = "--staged" in sys.argv
    problems = []
    for path in tracked_files(staged):
        name = path.rsplit("/", 1)[-1]
        if name in ("secrets.yaml", "secrets.yml"):
            problems.append(f"{path}: secrets.yaml must never be committed")
            continue
        if name in SKIP_FILES or not name.endswith((".yaml", ".yml", ".jinja", ".sh")):
            continue
        try:
            if staged:
                text = subprocess.run(["git", "show", f":{path}"], capture_output=True, text=True, check=True).stdout
            else:
                text = open(path, encoding="utf-8").read()
        except (OSError, subprocess.CalledProcessError, UnicodeDecodeError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if URL_CREDS.search(line):
                problems.append(f"{path}:{n}: URL with embedded credentials")
            m = KEY_LINE.match(line)
            if m and m.group(1).lower() in SENSITIVE_KEYS and not ALLOWED_VALUE.match(m.group(2)):
                problems.append(f"{path}:{n}: '{m.group(1)}' must use !secret, not an inline value")
    for p in problems:
        print(p, file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} inline secret(s) found. Put the value in secrets.yaml on the Green and reference it with !secret.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
