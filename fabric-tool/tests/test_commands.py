"""The shell commands: forms, the guard, refusals of placeholders and of non-fabric devices."""

from __future__ import annotations

import shutil
import subprocess
import time

import pytest

from fabric_tool import commands, model, plan as plan_mod, state
from fabric_tool.model import FabricError

MARKER = commands.MarkerConfig()


def _filter(k: int = 1) -> plan_mod.RelayFilter:
    return plan_mod.RelayFilter("enp1s0f1np1", k, "enp1s0f0np0", "02:00:00:00:00:03", 2, "enp1s0f0np1",
                                "cw", "primary")


def test_command_forms():
    relay_filter = _filter()
    assert commands.filter_add(relay_filter) == (
        "sudo -n tc filter add dev enp1s0f1np1 ingress protocol 0x88b5 pref 11 handle 1 flower skip_sw "
        "action pedit ex munge eth dst set 02:00:00:00:00:03 pipe "
        "action pedit ex munge eth type set 0x0800 pipe "
        "action mirred egress redirect dev enp1s0f0np0")
    assert commands.route_replace("192.0.2.17", "enp1s0f1np1", "192.0.2.26") == (
        "sudo -n ip route replace 192.0.2.17/32 dev enp1s0f1np1 src 192.0.2.26 scope link proto 82")
    assert commands.neigh_replace("192.0.2.17", "02:00:00:00:00:03", "enp1s0f1np1") == (
        "sudo -n ip neigh replace 192.0.2.17 lladdr 02:00:00:00:00:03 dev enp1s0f1np1 nud permanent proto 82")
    assert commands.qdisc_add("enp1s0f0np0") == "sudo -n tc qdisc add dev enp1s0f0np0 ingress"
    assert commands.qdisc_del("enp1s0f0np0") == "sudo -n tc qdisc del dev enp1s0f0np0 ingress"
    assert commands.filter_del("enp1s0f0np0", 11) == "sudo -n tc filter del dev enp1s0f0np0 ingress pref 11"
    assert commands.filter_del("enp1s0f0np0", 11, protocol=0x88b5, handle=1) == (
        "sudo -n tc filter del dev enp1s0f0np0 ingress protocol 0x88b5 pref 11 handle 1 flower")
    assert commands.marker_start(MARKER, "rocep1s0f0", [("192.0.2.10", 0x88b5)])[-1] == (
        "sudo -n setsid -f bash -c 'rm -f -- \"$0\" && set -C && exec \"$@\" < /dev/null > \"$0\" 2>&1' "
        "/var/lib/fabric-tool/logs/fabric-tool-marker-rocep1s0f0.log /usr/local/libexec/fabric-tool/fabric-tool-marker "
        "--device rocep1s0f0 --rule 192.0.2.10=0x88b5 --managed < /dev/null")
    stop, wait = commands.marker_stop(MARKER, 4100)
    assert stop == '[ "$(cat /proc/4100/comm 2>/dev/null)" != fabric-tool-mar ] || sudo -n kill 4100'
    assert wait.startswith("for i in $(seq 1 50)")
    assert commands.record_remove() == "sudo -n rm -f /run/fabric-table/relay-state.json"


def test_guard_and_script_header():
    script = commands.read_script(MARKER)
    assert script.startswith("set -u\n")
    assert "@@hostname" in script and "@@links" in script and "@@record" in script
    assert "@@sparkring-record" in script
    for netdev in model.FABRIC_NETDEVS:
        assert f"@@qdisc:{netdev}" in script and f"@@filters:{netdev}" in script
    lines = [commands.HEADER, commands.guard("node-0"), "sudo -n true"]
    assert lines[1] == ('test "$(hostname)" = node-0 || '
                        '{ echo \'refusing: this host is not node-0\' >&2; exit 3; }')
    with pytest.raises(FabricError):
        commands.guard("bad host; rm -rf /")


def test_refusals():
    with pytest.raises(FabricError, match="not a fabric network device"):
        commands.qdisc_add("eth0")
    with pytest.raises(FabricError, match="not a fabric RDMA device"):
        commands.marker_start(MARKER, "mlx5_99", [("192.0.2.10", 0x88b5)])
    with pytest.raises(FabricError, match="not an IPv4 address"):
        commands.route_replace("<h0:enp1s0f0np0>", "enp1s0f0np0", "192.0.2.1")
    with pytest.raises(FabricError, match="not a MAC address"):
        commands.neigh_replace("192.0.2.10", "placeholder", "enp1s0f0np0")
    with pytest.raises(FabricError, match="needs at least one rule"):
        commands.marker_start(MARKER, "rocep1s0f0", [])
    with pytest.raises(FabricError, match="absolute path"):
        commands.MarkerConfig("relative/marker")


def test_marker_default_path_is_root_owned_and_keeps_the_process_name():
    assert commands.DEFAULT_MARKER == "/usr/local/libexec/fabric-tool/fabric-tool-marker"
    assert MARKER.path == commands.DEFAULT_MARKER and MARKER.comm == "fabric-tool-mar"
    assert MARKER.record == "/usr/local/libexec/fabric-tool/fabric-tool-marker.sha256"
    assert MARKER.checked_paths == (MARKER.path, MARKER.record, "/usr/local/libexec/fabric-tool",
                                    "/usr/local/libexec", "/usr/local", "/usr", "/")
    assert commands.MarkerConfig("/opt/marker").checked_paths == ("/opt/marker", "/opt/marker.sha256", "/opt", "/")
    assert commands.MarkerConfig("/tmpfiles/marker").path == "/tmpfiles/marker"   # not under /tmp
    assert MARKER.log_dir == commands.DEFAULT_LOG_DIR == "/var/lib/fabric-tool/logs"
    assert MARKER.log("rocep1s0f0") == "/var/lib/fabric-tool/logs/fabric-tool-marker-rocep1s0f0.log"


@pytest.mark.parametrize("log_dir", ["/tmp", "/var/tmp/logs", "/dev/shm", "//tmp", "/var/log/", "logs"])
def test_marker_log_directories_in_shared_writable_or_relative_locations_are_refused(log_dir):
    with pytest.raises(FabricError, match="marker log directory"):
        commands.MarkerConfig(log_dir=log_dir)


@pytest.mark.parametrize("path", ["/tmp/fabric-tool-marker", "/var/tmp/fabric-tool-marker", "/dev/shm/marker",
                                  "/tmp", "/var/tmp/sub/marker", "//tmp/marker", "/var//tmp/marker",
                                  "/usr/./marker", "/usr/local/libexec/", "relative/marker", "marker",
                                  "./marker", "/usr/../tmp/marker"])
def test_marker_paths_in_shared_writable_or_relative_locations_are_refused(path):
    with pytest.raises(FabricError, match="marker path"):
        commands.MarkerConfig(path)


def test_every_marker_start_first_checks_ownership_and_digest():
    log_check, log_dir, owners, digest, start = commands.marker_start(MARKER, "rocep1s0f0", [("192.0.2.10", 0x88b5)])
    assert MARKER.log_dirs == ("/var/lib/fabric-tool/logs", "/var/lib/fabric-tool", "/var/lib", "/var", "/")
    assert log_check.startswith("for f in /var/lib/fabric-tool/logs /var/lib/fabric-tool /var/lib /var /; "
                                'do [ -e "$f" ] || continue; case ')
    assert "refusing to start the marker: $f must be owned by root and not be writable" in log_check
    assert log_dir == "sudo -n install -d -o root -g root -m 0755 /var/lib/fabric-tool/logs"
    assert (log_check, log_dir) == commands.log_dir_setup(MARKER)
    assert owners.startswith("for f in " + " ".join(MARKER.checked_paths) + "; do case ")
    assert "\"$(stat -c '%U %a' -- \"$f\" 2>/dev/null)\" in 'root '*[0145][0145]) ;;" in owners
    assert "refusing to start the marker: $f must exist, be owned by root" in owners and "exit 4;;" in owners
    assert digest == (f"printf '%s  %s\\n' \"$(head -c 64 {MARKER.record})\" {MARKER.path} | sha256sum -c --status "
                      f"|| {{ echo 'refusing to start the marker: {MARKER.path} differs from the SHA-256 recorded "
                      f"in {MARKER.record} when it was installed; reinstall the marker' >&2; exit 4; }}")
    assert start.startswith(f"sudo -n setsid -f bash -c {commands.q(commands.MARKER_LAUNCH)} "
                            f"{MARKER.log('rocep1s0f0')} {MARKER.path} --device rocep1s0f0 ")
    assert (owners, digest) == commands.marker_checks(MARKER)


def test_marker_build_script_installs_owned_by_root_and_records_the_digest():
    source = "int main(void) { return 0; }\n"
    script = commands.marker_build_script("node-0", MARKER, source)
    assert script.startswith("set -euo pipefail")
    lines = script.splitlines()
    compile_at = lines.index('cc -O2 -Wall -Wextra "$work/mesh_marker.c" -o "$work/marker" -libverbs -lmlx5')
    existing, make_dir, install, record, owners, digest, report = lines[compile_at + 1:]
    assert existing.startswith("for f in /usr/local/libexec/fabric-tool /usr/local/libexec /usr/local /usr /; "
                               'do [ -e "$f" ] || continue; case ')
    assert "refusing to install the marker" in existing
    assert make_dir == "sudo -n install -d -o root -g root -m 0755 /usr/local/libexec/fabric-tool"
    assert install == f'sudo -n install -o root -g root -m 0755 "$work/marker" {MARKER.path}'
    assert record == f"sha256sum {MARKER.path} | sudo -n tee {MARKER.record} >/dev/null"
    assert (owners, digest) == commands.marker_checks(MARKER, "install the marker")
    assert report == f"sha256sum {MARKER.path}"
    assert not any(line.startswith("install ") for line in lines)
    with pytest.raises(FabricError, match="here-document"):
        commands.marker_build_script("node-0", MARKER, source + commands.MARKER_SOURCE_END + "\n")


def test_every_read_section_ends_with_a_newline():
    for script in (commands.read_script(MARKER), commands.route_get_script(["192.0.2.10", "192.0.2.11"])):
        lines = script.splitlines()
        headers = [index for index, line in enumerate(lines) if line.startswith(f"echo {commands.SECTION}")]
        assert all(lines[index - 1] == commands.SECTION_END for index in headers[1:])


def test_read_script_reads_the_digest_record_and_the_owners():
    script = commands.read_script(MARKER)
    assert f"head -c 64 {MARKER.record} 2>/dev/null || true" in script
    assert (f"stat -c '%U %a %n' -- {' '.join(MARKER.checked_paths)} /var/lib/fabric-tool/logs /var/lib/fabric-tool "
            "/var/lib /var 2>/dev/null || true") in script
    assert f"head -c 4096 {MARKER.log('rocep1s0f0')} 2>/dev/null || true" in script
    assert "sudo" not in script


# -- the generated scripts under a real bash ---------------------------------------------------------------------

DIGEST = "ab" * 32
# Host commands as functions: the digest record holds 64 characters and no newline, as `install` writes it
# through `sha256sum ... | tee` and `head -c 64` reads it back.
READ_STUBS = f"""
hostname() {{ echo node-0; }}
ip() {{ echo '[]'; }}
tc() {{ echo '[]'; }}
ls() {{ :; }}
pgrep() {{ return 1; }}
sha256sum() {{ echo "{DIGEST}  $1"; }}
head() {{ [ "$3" = {MARKER.record} ] && printf %s {DIGEST}; return 0; }}
stat() {{ shift 3; for p; do echo "root 755 $p"; done; }}
cat() {{ :; }}
"""


def test_a_read_section_without_a_trailing_newline_keeps_the_next_header():
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash on this host")
    script = READ_STUBS + commands.read_script(MARKER)
    result = subprocess.run([bash, "-s"], input=script.encode("utf-8"), capture_output=True)
    stdout = result.stdout.decode("utf-8")
    assert f"{DIGEST}\n" in stdout and f"{DIGEST}@@" not in stdout
    parsed = state.parse(0, "node-0", "operator@example.invalid", returncode=result.returncode, stdout=stdout, stderr="")
    assert parsed.marker_binary == parsed.marker_recorded == DIGEST
    assert set(parsed.marker_owners) == set(MARKER.checked_paths) | set(MARKER.log_dirs)


def test_the_marker_start_leaves_no_descriptor_of_the_caller_open(tmp_path):
    """The start command returns while the marker runs: nothing holds the caller's stdout or stderr.

    An SSH session ends only when every holder of its stdout and stderr has
    exited, so a marker (or a sudo waiting for it) that kept them would hold
    the apply script open. ``sudo`` runs its command and waits for it, as the
    real one does; ``setsid -f`` forks and returns. The marker is ``sleep``.
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash on this host")
    start = commands.marker_start(MARKER, "rocep1s0f0", [("192.0.2.10", 0x88b5)])[-1]
    log = (tmp_path / "marker.log").as_posix()
    marker_words = f"{MARKER.log('rocep1s0f0')} {MARKER.path} --device rocep1s0f0 --rule 192.0.2.10=0x88b5 --managed"
    assert marker_words in start
    stubs = ('sudo() { [ "$1" = -n ] && shift; "$@"; }\n'
             'setsid() { if [ "$1" = -f ]; then shift; "$@" & else "$@"; fi; }\n')
    script = stubs + start.replace(marker_words, f"{log} sleep 4") + "\necho started\n"
    began = time.monotonic()
    result = subprocess.run([bash, "-c", script], capture_output=True, timeout=30)
    elapsed = time.monotonic() - began
    assert result.stdout.decode().strip() == "started" and not result.stderr
    assert elapsed < 3, f"the caller's output stayed open for {elapsed:.1f} s"
