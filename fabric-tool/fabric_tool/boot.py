"""Boot persistence: the systemd unit that restores the relay table at boot.

``install-boot`` writes the unit ``fabric-table.service``, copies the topology
file and the facts document to ``/etc/fabric-table/``, enables the unit and
installs the table now; ``remove-boot`` disables and removes both. The unit
runs ``apply --boot``, whose restore semantics add every missing object and
remove nothing, so a boot either reinstalls the whole table (``/run`` is
cleared, interfaces and their routes and filters come up empty, markers are
gone) or changes nothing while the table stands.
"""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
from pathlib import Path

from . import commands

UNIT_NAME = "fabric-table.service"
INPUT_DIR = "/etc/fabric-table"
TOPOLOGY_INPUT = f"{INPUT_DIR}/topology"
FACTS_INPUT = f"{INPUT_DIR}/facts.json"
LIB_DIR = "/usr/local/lib/fabric-tool"
BIN_PATH = "/usr/local/bin/fabric-tool"
DEFAULT_TOOL = BIN_PATH


def unit_text(tool: str = DEFAULT_TOOL) -> str:
    """The unit file of ``fabric-table.service``."""
    return "\n".join([
        "[Unit]",
        "Description=Fabric relay table of directly cabled DGX Sparks (fabric-tool)",
        "Documentation=file:///usr/local/lib/fabric-tool/README.md",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=oneshot",
        f"ExecStart={tool} apply --boot --topology {TOPOLOGY_INPUT} --facts {FACTS_INPUT}",
        "RemainAfterExit=yes",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ])


def wrapper_text() -> str:
    """The ``fabric-tool`` entry point the unit runs: the package copied to ``LIB_DIR``."""
    return ("#!/usr/bin/env python3\n"
            "import sys\n"
            f"sys.path.insert(0, {LIB_DIR!r})\n"
            "from fabric_tool.cli import main\n"
            "sys.exit(main())\n")


@dataclasses.dataclass(frozen=True)
class BootPlan:
    """The files ``install-boot`` writes and the commands it runs."""

    topology_path: str
    facts_path: str
    unit: str
    files: tuple[tuple[str, str], ...]     # (destination, content)

    @property
    def commands(self) -> tuple[str, ...]:
        return ("systemctl daemon-reload", f"systemctl enable {UNIT_NAME}")


def plan(topology_path: str, facts_path: str, tool: str = DEFAULT_TOOL) -> BootPlan:
    """The unit and input files of an ``install-boot`` (the caller passes validated inputs)."""
    topology = Path(topology_path).read_text(encoding="utf-8")
    facts = Path(facts_path).read_text(encoding="utf-8")
    return BootPlan(topology_path, facts_path, UNIT_NAME,
                    ((TOPOLOGY_INPUT, topology), (FACTS_INPUT, facts), (f"/etc/systemd/system/{UNIT_NAME}",
                                                                       unit_text(tool))))


def install(spec: BootPlan, *, root: str = "/", execute: bool) -> list[str]:
    """Write the unit and inputs; with ``root`` (tests) under that directory, else enable the unit."""
    written = []
    for destination, content in spec.files:
        path = Path(root) / destination.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(str(path))
    if root != "/":
        return written
    if execute:
        for command in spec.commands:
            subprocess.run(command.split(), check=True)
    return written


def remove(*, root: str = "/", execute: bool) -> list[str]:
    """Remove the unit and the copied inputs; with ``root`` (tests) under that directory."""
    removed = []
    if root == "/" and execute:
        subprocess.run(["systemctl", "disable", UNIT_NAME], check=True)
    for destination in (f"/etc/systemd/system/{UNIT_NAME}", TOPOLOGY_INPUT, FACTS_INPUT):
        path = Path(root) / destination.lstrip("/")
        if path.exists():
            path.unlink()
            removed.append(str(path))
    _rmdir_if_empty(Path(root) / INPUT_DIR.lstrip("/"))
    if root == "/" and execute:
        subprocess.run(["systemctl", "daemon-reload"], check=True)
    return removed


def _rmdir_if_empty(directory: Path) -> None:
    try:
        directory.rmdir()
    except OSError:
        pass


def copy_inputs(topology_path: str, facts_path: str, *, root: str = "/") -> list[str]:
    """Copy validated inputs to ``/etc/fabric-table/`` (used by ``install-boot`` before the unit runs)."""
    written = []
    for source, destination in ((topology_path, TOPOLOGY_INPUT), (facts_path, FACTS_INPUT)):
        target = Path(root) / destination.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        written.append(str(target))
    return written


def marker_of(record: dict | None) -> str | None:
    """The marker path a boot record names, for status output."""
    return str(record.get("marker")) if record and record.get("marker") else commands.DEFAULT_MARKER
