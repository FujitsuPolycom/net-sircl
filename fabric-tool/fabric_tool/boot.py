"""Local relay restoration at boot and after link or marker loss.

``install-boot`` writes the unit ``fabric-table.service``, copies the topology
file and the facts document to ``/etc/fabric-table/``, enables the unit and
installs the table now; ``remove-boot`` disables and removes both. The unit
runs ``apply --boot --execute`` at startup and reload. A timer requests reload
every 15 seconds after the preceding check finishes. Restore semantics add
missing objects and remove nothing. Reload retains the service's marker
processes; restarting the service on each check would kill healthy markers.
"""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
from pathlib import Path

from . import commands, model

UNIT_NAME = "fabric-table.service"
RECOVERY_SERVICE = "fabric-table-reconcile.service"
RECOVERY_TIMER = "fabric-table-reconcile.timer"
INPUT_DIR = "/etc/fabric-table"
TOPOLOGY_INPUT = f"{INPUT_DIR}/topology"
FACTS_INPUT = f"{INPUT_DIR}/facts.json"
LIB_DIR = "/usr/local/lib/fabric-tool"
BIN_PATH = "/usr/local/bin/fabric-tool"
DEFAULT_TOOL = BIN_PATH


@dataclasses.dataclass(frozen=True)
class RestoreInvocation:
    """Behavior persisted by the boot unit and used by immediate restoration."""

    tool: str = DEFAULT_TOOL
    marker: str = commands.DEFAULT_MARKER
    max_relays: int = model.DEFAULT_MAX_RELAYS
    host: str | None = None
    skip_hostname_check: bool = False

    def argv(self) -> tuple[str, ...]:
        args = (self.tool, "apply", "--boot", "--execute", "--topology", TOPOLOGY_INPUT,
                "--facts", FACTS_INPUT, "--marker", self.marker, "--max-relays", str(self.max_relays))
        if self.host:
            args += ("--host", self.host)
        if self.skip_hostname_check:
            args += ("--skip-hostname-check",)
        return args


def systemd_arg(value: str) -> str:
    """Quote one ExecStart argument, including systemd specifier and variable expansion."""
    if any(ord(c) < 32 for c in value):
        raise ValueError("service arguments must not contain control characters")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def unit_text(tool: str = DEFAULT_TOOL, *, invocation: RestoreInvocation | None = None) -> str:
    """The unit file of ``fabric-table.service``."""
    invocation = invocation or RestoreInvocation(tool=tool)
    return "\n".join([
        "[Unit]",
        "Description=Fabric relay table of directly cabled DGX Sparks (fabric-tool)",
        "Documentation=file:///usr/local/lib/fabric-tool/README.md",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=oneshot",
        "ExecStart=" + " ".join(systemd_arg(arg) for arg in invocation.argv()),
        "ExecReload=" + " ".join(systemd_arg(arg) for arg in invocation.argv()),
        "RemainAfterExit=yes",
        "TimeoutStartSec=300",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ])


def recovery_service_text() -> str:
    """Run checks in the retained service's cgroup so markers survive each check."""
    return ("[Unit]\nDescription=Reconcile missing local fabric relay state\n\n"
            "[Service]\nType=oneshot\n"
            f"ExecStart=/usr/bin/systemctl reload-or-restart {UNIT_NAME}\n"
            "TimeoutStartSec=330\n")


def recovery_timer_text() -> str:
    """Retry incomplete restoration without overlapping local reconciliation jobs."""
    return ("[Unit]\nDescription=Check local fabric relay state periodically\n\n"
            "[Timer]\nOnActiveSec=15s\nOnUnitInactiveSec=15s\nAccuracySec=1s\n"
            f"Unit={RECOVERY_SERVICE}\n\n[Install]\nWantedBy=timers.target\n")


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
        return ("systemctl daemon-reload", f"systemctl enable {UNIT_NAME} {RECOVERY_TIMER}")


def plan(topology_path: str, facts_path: str, tool: str = DEFAULT_TOOL, *,
         invocation: RestoreInvocation | None = None, topology_text: str | None = None,
         facts_text: str | None = None) -> BootPlan:
    """The unit and input files of an ``install-boot`` (the caller passes validated inputs)."""
    topology = topology_text if topology_text is not None else Path(topology_path).read_text(encoding="utf-8")
    facts = facts_text if facts_text is not None else Path(facts_path).read_text(encoding="utf-8")
    return BootPlan(topology_path, facts_path, UNIT_NAME,
                    ((TOPOLOGY_INPUT, topology), (FACTS_INPUT, facts), (f"/etc/systemd/system/{UNIT_NAME}",
                                                                       unit_text(tool, invocation=invocation)),
                     (f"/etc/systemd/system/{RECOVERY_SERVICE}", recovery_service_text()),
                     (f"/etc/systemd/system/{RECOVERY_TIMER}", recovery_timer_text())))


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
        if Path(f"/etc/systemd/system/{RECOVERY_TIMER}").exists():
            subprocess.run(["systemctl", "disable", "--now", RECOVERY_TIMER], check=True)
        if Path(f"/etc/systemd/system/{RECOVERY_SERVICE}").exists():
            subprocess.run(["systemctl", "stop", RECOVERY_SERVICE], check=True)
        subprocess.run(["systemctl", "disable", UNIT_NAME], check=True)
    for destination in (f"/etc/systemd/system/{UNIT_NAME}", f"/etc/systemd/system/{RECOVERY_SERVICE}",
                        f"/etc/systemd/system/{RECOVERY_TIMER}", TOPOLOGY_INPUT, FACTS_INPUT):
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
