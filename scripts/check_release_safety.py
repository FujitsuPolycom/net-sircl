"""Scan tracked text for host names, private IPv4 addresses, home paths and credentials without printing matches."""
import argparse
import ipaddress
import json
from pathlib import Path
import re
import subprocess

RULES = {
    "site-host": r"\bspark-[a-z0-9]{4}\b",
    "local-user-path": r"[A-Za-z]:(?:\\|/)+Users(?:\\|/)|/mnt/[a-z]/Users/|/home/[^/\s]+/|AppData(?:\\|/)",
    "private-key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "credential": r"(?:password|secret|api[_-]?key|access[_-]?token)[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9/+_.-]{12,}",
    "provider-token": r"xox[baprs]-|gh[pousr]_[A-Za-z0-9]{20,}",
}
IPV4 = re.compile(r"(?<![\w.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![\w.])")
DOCUMENTATION_NETS = tuple(ipaddress.ip_network(n) for n in
                           ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "127.0.0.0/8"))


def findings(text, site=()):
    for number, line in enumerate(text.splitlines(), 1):
        for name, pattern in RULES.items():
            if re.search(pattern, line, re.I):
                yield number, name
        for value in IPV4.findall(line):
            try:
                addr = ipaddress.ip_address(value)
            except ValueError:
                continue
            if not addr.is_unspecified and not any(addr in net for net in DOCUMENTATION_NETS):
                yield number, "non-documentation-ipv4"
        if any(re.search(r"(?<![\w.-])" + re.escape(value) + r"(?![\w.-])", line, re.I) for value in site):
            yield number, "site-value"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--site-values", type=Path, help="add one literal host name or address per line from FILE")
    args = parser.parse_args()
    site = []
    if args.site_values:
        site = [line.split("#", 1)[0].strip() for line in args.site_values.read_text().splitlines()]
        site = [value for value in site if value]
    try:
        paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=args.root).decode().split("\0")
        count = 0
        for name in filter(None, paths):
            if name == "scripts/check_release_safety.py":
                continue
            data = (args.root / name).read_bytes()
            if b"\0" in data:
                continue
            for line, rule in findings(data.decode("utf-8"), site):
                print(json.dumps({"path": name, "line": line, "rule": rule}))
                count += 1
        print(f"Release-safety findings: {count}; matched content withheld; site values: {len(site)}")
        return int(count > 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Release-safety scan could not inspect all tracked files")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
