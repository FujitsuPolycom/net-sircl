"""The boot unit: text, install and removal under a fake root."""

from __future__ import annotations

from fabric_tool import boot
from fabric_tool import cli
import shlex


def test_unit_text():
    text = boot.unit_text()
    assert text.startswith("[Unit]")
    assert "After=network-online.target" in text
    argv = shlex.split(next(line.removeprefix("ExecStart=") for line in text.splitlines()
                            if line.startswith("ExecStart=")))
    assert argv == list(boot.RestoreInvocation().argv())
    assert "--execute" in argv
    assert "WantedBy=multi-user.target" in text


def test_recovery_reloads_the_retained_service_without_restarting_markers(tmp_path):
    invocation = boot.RestoreInvocation(marker="/opt/relay/bin/marker", max_relays=2, host="node-1")
    text = boot.unit_text(invocation=invocation)
    reload_argv = shlex.split(next(line.removeprefix("ExecReload=") for line in text.splitlines()
                                  if line.startswith("ExecReload=")))
    assert reload_argv == list(invocation.argv())
    assert "RemainAfterExit=yes" in text
    spec = boot.plan("topology", "facts", invocation=invocation, topology_text="hosts a b\n", facts_text="{}")
    files = dict(spec.files)
    recovery = files[f"/etc/systemd/system/{boot.RECOVERY_SERVICE}"]
    timer = files[f"/etc/systemd/system/{boot.RECOVERY_TIMER}"]
    assert f"ExecStart=/usr/bin/systemctl reload-or-restart {boot.UNIT_NAME}" in recovery
    assert "RemainAfterExit" not in recovery
    assert "OnUnitInactiveSec=15s" in timer
    assert f"Unit={boot.RECOVERY_SERVICE}" in timer
    boot.install(spec, root=str(tmp_path), execute=False)
    boot.remove(root=str(tmp_path), execute=False)
    assert not any((tmp_path / "etc/systemd/system").glob("fabric-table*"))


def test_wrapper_runs_the_copied_package():
    wrapper = boot.wrapper_text()
    assert wrapper.startswith("#!/usr/bin/env python3")
    assert f"sys.path.insert(0, {boot.LIB_DIR!r})" in wrapper
    assert "from fabric_tool.cli import main" in wrapper


def test_install_and_remove_under_a_fake_root(tmp_path):
    topology = tmp_path / "ring4.topology"
    topology.write_text("hosts a b\ncabling ring\n", encoding="utf-8")
    facts = tmp_path / "facts.json"
    facts.write_text('{"schema": "fabric-tool-facts/v1", "hosts": []}', encoding="utf-8")
    spec = boot.plan(str(topology), str(facts))
    assert [destination for destination, _ in spec.files] == [
        boot.TOPOLOGY_INPUT, boot.FACTS_INPUT, f"/etc/systemd/system/{boot.UNIT_NAME}",
        f"/etc/systemd/system/{boot.RECOVERY_SERVICE}", f"/etc/systemd/system/{boot.RECOVERY_TIMER}"]
    written = boot.install(spec, root=str(tmp_path), execute=False)
    assert len(written) == 5
    unit = tmp_path / "etc/systemd/system" / boot.UNIT_NAME
    assert unit.read_text().startswith("[Unit]")
    assert (tmp_path / "etc/fabric-table/topology").read_text() == "hosts a b\ncabling ring\n"
    removed = boot.remove(root=str(tmp_path), execute=False)
    assert not unit.exists() and not (tmp_path / "etc/fabric-table").exists()
    assert len(removed) == 5


def test_remove_boot_cli_preview_and_execute(tmp_path):
    unit = tmp_path / "etc/systemd/system" / boot.UNIT_NAME
    unit.parent.mkdir(parents=True)
    unit.write_text(boot.unit_text())
    assert cli.main(["remove-boot"]) == 0
    assert cli.main(["remove-boot", "--root", str(tmp_path)]) == 0
    assert unit.exists()
    assert cli.main(["remove-boot", "--root", str(tmp_path), "--execute"]) == 0
    assert not unit.exists()


def test_install_boot_cli_default_is_preview(tmp_path, monkeypatch):
    import json
    from test_facts import _document, RING4

    topology = tmp_path / "ring.topology"
    topology.write_text(RING4)
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps(_document()))

    def forbid(*args, **kwargs):
        raise AssertionError("boot preview must not write files or contact hosts")

    monkeypatch.setattr(boot, "install", forbid)
    assert cli.main(["install-boot", "--topology", str(topology), "--facts", str(facts)],
                    executor=forbid) == 0


def test_systemd_enable_failure_is_not_success(tmp_path, monkeypatch):
    import subprocess
    import pytest

    spec = boot.BootPlan("topology", "facts", boot.UNIT_NAME, ())
    calls = []

    def systemctl(argv, **kwargs):
        calls.append(argv)
        assert kwargs["check"]
        if "enable" in argv:
            raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(boot.subprocess, "run", systemctl)
    with pytest.raises(subprocess.CalledProcessError):
        boot.install(spec, execute=True)
    assert calls == [["systemctl", "daemon-reload"],
                     ["systemctl", "enable", boot.UNIT_NAME, boot.RECOVERY_TIMER]]


def test_removal_keeps_inputs_until_the_manager_job_is_idle(tmp_path, monkeypatch):
    from pathlib import Path
    import subprocess

    spec = boot.plan('topology', 'facts', topology_text='hosts a b\n', facts_text='{}')
    boot.install(spec, root=str(tmp_path), execute=False)
    files = [tmp_path / name.lstrip('/') for name, _ in spec.files]
    states = iter(['ActiveState=active\nJob=42\n', 'ActiveState=reloading\nJob=42\n',
                   'ActiveState=active\nJob=\n'])
    observations = []

    def systemctl(argv, **kwargs):
        output = ''
        if argv[1] == 'show':
            assert all(path.exists() for path in files)
            output = next(states)
            observations.append(output)
        return subprocess.CompletedProcess(argv, 0, stdout=output)

    monkeypatch.setattr(boot, 'Path', lambda value: tmp_path / str(value).lstrip('/')
                        if str(value).startswith('/') else Path(value))
    monkeypatch.setattr(boot.subprocess, 'run', systemctl)
    monkeypatch.setattr(boot.time, 'sleep', lambda _: None)
    boot.remove(execute=True)
    assert len(observations) == 3
    assert not any(path.exists() for path in files)
