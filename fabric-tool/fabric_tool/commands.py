"""Shell commands the tool sends to a host.

Derived from the SparkRing relay installer; see NOTICE.
Every remote action is one ``bash -s`` script over SSH in batch mode (or the
same script run locally through ``bash -s``), one command per line, every
interpolated value shell-quoted. The read script (:func:`read_script`) uses
read-only commands and needs no root: it is the READ-ONLY part of every dry
run. Apply scripts change host state through ``sudo -n`` (MUTATES HOST), stop
at the first failing command (``set -euo pipefail``) and first check the
host's name (:func:`guard`), so a wrong SSH target changes nothing.

Ownership marks, by which ``remove`` deletes exactly the tool's objects:

- routes and permanent neighbours carry routing protocol number 82
  (:data:`ROUTE_PROTOCOL`, ``proto 82``);
- relay filters occupy the reserved preferences 11 to 17 of chain 0 on the
  ingress of the four fabric network devices (preference ``10 + k``, handle
  ``k``, protocol ``TAG(k)``);
- marker processes run the marker executable, whose first 15 characters are
  the process name ``pgrep -x`` matches;
- the record ``/run/fabric-table/relay-state.json`` names the host's group,
  layout, relay egress, plan digest and the ingress qdiscs the tool added.
  ``/run`` is cleared at boot, like every object of the plan.

Nothing else is changed: not the connections, addresses, links, other
routes, other neighbours, other filters, other qdiscs or other processes. A
device name outside the fabric's four is refused (:func:`_netdev`), and the
record of SparkRing's installer (``/run/sparkring-fabric/relay-state.json``)
is read so the tool can refuse a host whose table SparkRing owns.

The marker executable runs as root. Its path must not lie under a directory
every user can write (``/tmp``, ``/var/tmp``, ``/dev/shm``); the build script
installs it owned by root and records its SHA-256 beside it (``<path>.sha256``);
and every start first checks that the executable, the record and every
directory above them are owned by root and not writable by group or others,
and that the executable still has the recorded digest (:func:`marker_checks`).
Root also opens the marker logs, in a root-owned log directory, without
following a symbolic link (:func:`log_dir_setup`, :data:`MARKER_LAUNCH`).
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import posixpath
import re
import shlex
from collections.abc import Iterable, Mapping

from . import model
from .model import FabricError
from .plan import RelayFilter

ROUTE_PROTOCOL = 82
STATE_DIR = "/run/fabric-table"
RECORD_PATH = f"{STATE_DIR}/relay-state.json"
RECORD_SCHEMA = "fabric-table-record/v1"
SPARKRING_RECORD_PATH = "/run/sparkring-fabric/relay-state.json"
# The marker runs as root, so it lives in a directory only root can write. Its
# file name keeps the process name ``fabric-tool-mar`` that identifies the
# tool's marker processes.
DEFAULT_MARKER = "/usr/local/libexec/fabric-tool/fabric-tool-marker"
# Directories every local user can write to; a marker path under them is refused.
SHARED_WRITABLE_DIRS = ("/tmp", "/var/tmp", "/dev/shm")
# Marker logs are opened by root, so they live in a root-owned directory too.
DEFAULT_LOG_DIR = "/var/lib/fabric-tool/logs"
REFUSE_EXIT = 4
# The root shell that starts a marker: it removes any file at the log path, creates the log
# with noclobber (O_EXCL), so a symbolic link at the log path is never followed, then
# execs the marker ($0 is the log, "$@" the marker command line).
MARKER_LAUNCH = 'rm -f -- "$0" && set -C && exec "$@" < /dev/null > "$0" 2>&1'
SECTION = "@@"
# Ends every section of a read script with a newline, so the next ``@@`` header starts its own
# line even after output without one (``head -c 64`` of the digest record prints none).
SECTION_END = "echo"
HEADER = "set -euo pipefail"
SETTLE = "sleep 1.5"
_PATH = re.compile(r"^/[A-Za-z0-9_./+-]+$")
_MAC = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
_HOSTNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


def q(value: object) -> str:
    return shlex.quote(str(value))


@dataclasses.dataclass(frozen=True)
class MarkerConfig:
    """Where the marker executable lives on every host and where each marker logs."""

    path: str = DEFAULT_MARKER
    log_dir: str = DEFAULT_LOG_DIR

    def __post_init__(self) -> None:
        for label, value in (("marker path", self.path), ("marker log directory", self.log_dir)):
            if not _PATH.match(value) or "/../" in f"{value}/":
                raise FabricError(f"{label} {value!r} must be an absolute path of letters, digits and _./+-")
        # Root runs the executable and opens the logs, so neither may live where every user can write.
        for label, value, default in (("marker path", self.path, DEFAULT_MARKER),
                                      ("marker log directory", self.log_dir, DEFAULT_LOG_DIR)):
            if value != posixpath.normpath(value) or value.startswith("//"):
                raise FabricError(f"{label} {value!r} must be a normalized absolute path")
            for directory in SHARED_WRITABLE_DIRS:
                if value == directory or value.startswith(directory + "/"):
                    raise FabricError(f"{label} {value!r} is under {directory}, which every local user can "
                                      "write; the marker runs as root, so it must be a root-owned location such "
                                      f"as the default {default}")

    @property
    def name(self) -> str:
        return posixpath.basename(self.path)

    @property
    def comm(self) -> str:
        """The process name the kernel keeps for the executable (its first 15 characters)."""
        return self.name[:15]

    @property
    def directory(self) -> str:
        return posixpath.dirname(self.path)

    @property
    def record(self) -> str:
        """The digest record: the ``sha256sum`` line of the executable, written when it is installed."""
        return f"{self.path}.sha256"

    @property
    def checked_paths(self) -> tuple[str, ...]:
        """The executable, its digest record and every directory above them up to ``/``.

        All of them must be owned by root and not writable by group or
        others before the marker starts (:func:`marker_checks`).
        """
        paths = [self.path, self.record]
        directory = self.directory
        while directory != "/":
            paths.append(directory)
            directory = posixpath.dirname(directory)
        return (*paths, "/")

    @property
    def log_dirs(self) -> tuple[str, ...]:
        """The log directory and every directory above it up to ``/``.

        Each that exists must be owned by root and not writable by group or
        others before a marker starts (:func:`log_dir_setup`).
        """
        paths = []
        directory = self.log_dir
        while directory != "/":
            paths.append(directory)
            directory = posixpath.dirname(directory)
        return (*paths, "/")

    def log(self, device: str) -> str:
        return f"{self.log_dir}/{self.name}-{device}.log"


def _ipv4(value: str) -> str:
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError:
        raise FabricError(f"{value!r} is not an IPv4 address; apply needs the hosts' facts, not placeholders") from None


def _mac(value: str) -> str:
    if not _MAC.match(value.lower()):
        raise FabricError(f"{value!r} is not a MAC address; apply needs the hosts' facts, not placeholders")
    return value.lower()


def _netdev(value: str, *, bindings: model.FabricBindings = model.DEFAULT_BINDINGS) -> str:
    if value not in bindings.netdevs:
        raise FabricError(f"{value!r} is not a fabric network device")
    return q(model.device_name(value))


def _device(value: str, *, bindings: model.FabricBindings = model.DEFAULT_BINDINGS) -> str:
    if value not in bindings.devices:
        raise FabricError(f"{value!r} is not a fabric RDMA device")
    return q(model.device_name(value))


# -- read script ---------------------------------------------------------------------------


def read_script(marker: MarkerConfig, *, bindings: model.FabricBindings = model.DEFAULT_BINDINGS) -> str:
    """Read-only facts and relay objects of one host, as ``@@<section>`` blocks."""
    lines = ["set -u"]

    def section(name: str, *commands: str) -> None:
        lines.append(f"echo {q(SECTION + name)}")
        lines.extend(commands)
        lines.append(SECTION_END)

    section("hostname", "hostname")
    section("versions", "ip -V 2>&1 || true", "tc -V 2>&1 || true")
    section("links", "ip -j addr show")
    for device in bindings.devices:
        section(f"rdma:{device}", f"ls /sys/class/infiniband/{q(device)}/device/net 2>/dev/null || true")
    section("routes", "ip -j -d route show table main")
    section("neighbours", "ip -j neigh show nud permanent")
    for netdev in bindings.netdevs:
        section(f"qdisc:{netdev}", f"tc -j qdisc show dev {q(netdev)} 2>/dev/null || true")
        section(f"filters:{netdev}", f"tc -j filter show dev {netdev} ingress 2>/dev/null || true")
    section("markers", f"pgrep -a -x {q(marker.comm)} || true")
    section("marker-binary", f"sha256sum {q(marker.path)} 2>/dev/null || true")
    section("marker-record", f"head -c 64 {q(marker.record)} 2>/dev/null || true")
    section("marker-owners", f"stat -c '%U %a %n' -- {' '.join(q(p) for p in marker.checked_paths)} "
                             f"{' '.join(q(p) for p in marker.log_dirs if p not in marker.checked_paths)} "
                             "2>/dev/null || true")
    for device in bindings.devices:
        section(f"marker-log:{device}", f"head -c 4096 {q(marker.log(device))} 2>/dev/null || true")
    section("record", f"cat {RECORD_PATH} 2>/dev/null || true")
    section("sparkring-record", f"cat {SPARKRING_RECORD_PATH} 2>/dev/null || true")
    lines.append(f"echo {SECTION}end")
    return "\n".join(lines) + "\n"


def route_get_script(destinations: Iterable[str]) -> str:
    """``ip -o route get`` of every destination (read-only), as ``@@route:<address>`` blocks.

    Verify resolves the IP route for every lane destination.
    """
    lines = ["set -u"]
    for destination in destinations:
        address = _ipv4(destination)
        lines += [f"echo {SECTION}route:{address}", f"ip -o route get {address} 2>&1 || true", SECTION_END]
    lines.append(f"echo {SECTION}end")
    return "\n".join(lines) + "\n"


# -- apply commands --------------------------------------------------------------------------


def guard(hostname: str) -> str:
    """Stop the script unless it runs on ``hostname``."""
    if not _HOSTNAME.match(hostname):
        raise FabricError(f"host name {hostname!r} cannot be checked safely")
    return (f'test "$(hostname)" = {q(hostname)} || '
            f'{{ echo {q("refusing: this host is not " + hostname)} >&2; exit 3; }}')


def qdisc_add(netdev: str, *, bindings: model.FabricBindings = model.DEFAULT_BINDINGS) -> str:
    return f"sudo -n tc qdisc add dev {_netdev(netdev, bindings=bindings)} ingress"


def qdisc_del(netdev: str, *, bindings: model.FabricBindings = model.DEFAULT_BINDINGS) -> str:
    return f"sudo -n tc qdisc del dev {_netdev(netdev, bindings=bindings)} ingress"


def filter_add(relay_filter: RelayFilter, *, bindings: model.FabricBindings = model.DEFAULT_BINDINGS) -> str:
    return (f"sudo -n tc filter add dev {_netdev(relay_filter.in_netdev, bindings=bindings)} ingress protocol "
            f"0x{relay_filter.protocol:04x} pref {relay_filter.pref} handle {relay_filter.handle} flower skip_sw "
            f"action pedit ex munge eth dst set {_mac(relay_filter.next_mac)} pipe "
            f"action pedit ex munge eth type set 0x{relay_filter.new_type:04x} pipe "
            f"action mirred egress redirect dev {_netdev(relay_filter.out_netdev, bindings=bindings)}")


def filter_del(netdev: str, pref: int, *, protocol: int | None = None, handle: int | None = None, bindings: model.FabricBindings = model.DEFAULT_BINDINGS) -> str:
    """Delete one filter (protocol and handle known) or every filter of a reserved preference."""
    if protocol is None or handle is None:
        return f"sudo -n tc filter del dev {_netdev(netdev, bindings=bindings)} ingress pref {int(pref)}"
    return (f"sudo -n tc filter del dev {_netdev(netdev, bindings=bindings)} ingress protocol 0x{int(protocol):04x} "
            f"pref {int(pref)} handle {int(handle)} flower")


def neigh_replace(destination: str, mac: str, netdev: str, *, bindings: model.FabricBindings = model.DEFAULT_BINDINGS) -> str:
    return (f"sudo -n ip neigh replace {_ipv4(destination)} lladdr {_mac(mac)} dev {_netdev(netdev, bindings=bindings)} "
            f"nud permanent proto {ROUTE_PROTOCOL}")


def neigh_del(destination: str, netdev: str, *, bindings: model.FabricBindings = model.DEFAULT_BINDINGS) -> str:
    return f"sudo -n ip neigh del {_ipv4(destination)} dev {_netdev(netdev, bindings=bindings)}"


def route_replace(destination: str, netdev: str, source: str, *, bindings: model.FabricBindings = model.DEFAULT_BINDINGS) -> str:
    return (f"sudo -n ip route replace {_ipv4(destination)}/32 dev {_netdev(netdev, bindings=bindings)} src {_ipv4(source)} "
            f"scope link proto {ROUTE_PROTOCOL}")


def route_del(destination: str, netdev: str, *, protocol: int | None, metric: int = 0, bindings: model.FabricBindings = model.DEFAULT_BINDINGS) -> str:
    """Delete one /32 route; naming its protocol makes the kernel refuse any other route."""
    command = f"sudo -n ip route del {_ipv4(destination)}/32 dev {_netdev(netdev, bindings=bindings)}"
    if protocol is not None:
        command += f" proto {int(protocol)}"
    if metric:
        command += f" metric {int(metric)}"
    return command


def _owner_check(paths: Iterable[str], action: str, *, existing_only: bool = False) -> str:
    """Stop the script (exit 4) unless every path is owned by root and not writable by group or others.

    ``stat -c '%U %a'`` prints the owner and the octal mode, whose last two
    digits (group, others) must lack the write bit: 0, 1, 4 or 5. A missing
    path prints nothing and a symbolic link reports mode 777, so both are
    refused. ``existing_only`` skips paths that do not exist yet, the
    directories an install creates.
    """
    skip = '[ -e "$f" ] || continue; ' if existing_only else ""
    requirement = "be owned by root" if existing_only else "exist, be owned by root"
    return (f"for f in {' '.join(q(path) for path in paths)}; do {skip}"
            "case \"$(stat -c '%U %a' -- \"$f\" 2>/dev/null)\" in 'root '*[0145][0145]) ;; "
            f'*) echo "refusing to {action}: $f must {requirement} and not be writable by group '
            f'or others" >&2; exit {REFUSE_EXIT};; esac; done')


def log_dir_setup(marker: MarkerConfig) -> tuple[str, str]:
    """Refuse a log directory, or any directory above it, that exists but is not root's alone; then
    create the missing ones owned by root.

    Root opens each log in that directory without following a symbolic link
    (:data:`MARKER_LAUNCH`), and no other user can replace the directory or
    any directory above it.
    """
    return (_owner_check(marker.log_dirs, "start the marker", existing_only=True),
            f"sudo -n install -d -o root -g root -m 0755 {q(marker.log_dir)}")


def marker_checks(marker: MarkerConfig, action: str = "start the marker") -> tuple[str, str]:
    """The two checks that precede every start of the marker as root.

    The executable, its digest record and every directory above them must
    be root's alone, so no other user can replace them between these checks
    and the start. The executable's SHA-256 must then equal the digest that
    :func:`marker_build_script` recorded when it installed the executable.
    Either failure ends the script with exit 4 before anything runs.
    """
    refusal = (f"refusing to {action}: {marker.path} differs from the SHA-256 recorded in "
               f"{marker.record} when it was installed; reinstall the marker")
    return (_owner_check(marker.checked_paths, action),
            f"printf '%s  %s\\n' \"$(head -c 64 {q(marker.record)})\" {q(marker.path)} | sha256sum -c --status || "
            f"{{ echo {q(refusal)} >&2; exit {REFUSE_EXIT}; }}")


def marker_start(marker: MarkerConfig, device: str,
                 rules: Iterable[tuple[str, int]], *, bindings: model.FabricBindings = model.DEFAULT_BINDINGS) -> tuple[str, str, str, str, str]:
    """The log directory (:func:`log_dir_setup`), the checks of :func:`marker_checks`, then one managed
    marker started as root in its own session.

    ``setsid -f`` forks the session and returns at once, so ``sudo`` exits and
    the start command ends; root's shell opens the log (:data:`MARKER_LAUNCH`)
    and execs the marker with stdin from ``/dev/null`` and stdout and stderr on
    the log. No process keeps the descriptors of the script's caller (an SSH
    session waits until every holder of its stdout and stderr exits). The
    marker keeps its process name, its command line and its rules until
    SIGTERM.
    """
    rule_words = " ".join(f"--rule {_ipv4(destination)}=0x{int(value):04x}" for destination, value in rules)
    if not rule_words:
        raise FabricError(f"a marker for {device} needs at least one rule")
    return (*log_dir_setup(marker), *marker_checks(marker),
            f"sudo -n setsid -f bash -c {q(MARKER_LAUNCH)} {q(marker.log(device))} {q(marker.path)} "
            f"--device {_device(device, bindings=bindings)} {rule_words} --managed < /dev/null")


def marker_stop(marker: MarkerConfig, pid: int) -> tuple[str, str]:
    """Signal one marker process (only while it still is one) and wait up to 5 s for it to exit.

    The marker removes its RDMA-TX rules when it exits.
    """
    pid = int(pid)
    return (f'[ "$(cat /proc/{pid}/comm 2>/dev/null)" != {q(marker.comm)} ] || sudo -n kill {pid}',
            f"for i in $(seq 1 50); do [ -e /proc/{pid} ] || break; sleep 0.1; done; [ ! -e /proc/{pid} ]")


def record_text(record: Mapping[str, object]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


def record_write(record: Mapping[str, object]) -> tuple[str, str]:
    return (f"sudo -n mkdir -p {STATE_DIR}",
            f"printf '%s\\n' {q(record_text(record))} | sudo -n tee {RECORD_PATH} >/dev/null")


def record_remove() -> str:
    return f"sudo -n rm -f {RECORD_PATH}"


# -- marker executable -----------------------------------------------------------------------

MARKER_SOURCE_END = "FABRIC_TOOL_MARKER_SOURCE_END"


def marker_build_script(hostname: str, marker: MarkerConfig, source: str) -> str:
    """Compile the marker source on a host (rdma-core headers needed) and install it at the marker path.

    The directories that already exist above the marker must be root's alone;
    the install creates the missing ones and the executable owned by root
    (mode 0755), writes the digest record and ends with the checks every
    start runs (:func:`marker_checks`).
    """
    if any(line.strip() == MARKER_SOURCE_END for line in source.splitlines()):
        raise FabricError("the marker source contains the here-document delimiter")
    return "\n".join([
        HEADER,
        guard(hostname),
        "work=$(mktemp -d)",
        "trap 'rm -rf \"$work\"' EXIT",
        f"cat > \"$work/mesh_marker.c\" <<'{MARKER_SOURCE_END}'",
        source.rstrip("\n"),
        MARKER_SOURCE_END,
        "cc -O2 -Wall -Wextra \"$work/mesh_marker.c\" -o \"$work/marker\" -libverbs -lmlx5",
        _owner_check(marker.checked_paths[2:], "install the marker", existing_only=True),
        f"sudo -n install -d -o root -g root -m 0755 {q(marker.directory)}",
        f"sudo -n install -o root -g root -m 0755 \"$work/marker\" {q(marker.path)}",
        f"sha256sum {q(marker.path)} | sudo -n tee {q(marker.record)} >/dev/null",
        *marker_checks(marker, "install the marker"),
        f"sha256sum {q(marker.path)}",
    ]) + "\n"
