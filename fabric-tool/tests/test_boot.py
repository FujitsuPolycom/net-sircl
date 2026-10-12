"""The boot unit: text, install and removal under a fake root."""

from __future__ import annotations

from fabric_tool import boot
from fabric_tool import cli


def test_unit_text():
    text = boot.unit_text()
    assert text.startswith("[Unit]")
    assert "After=network-online.target" in text
    assert f"ExecStart={boot.DEFAULT_TOOL} apply --boot --topology {boot.TOPOLOGY_INPUT} " \
           f"--facts {boot.FACTS_INPUT}" in text
    assert "WantedBy=multi-user.target" in text


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
        boot.TOPOLOGY_INPUT, boot.FACTS_INPUT, f"/etc/systemd/system/{boot.UNIT_NAME}"]
    written = boot.install(spec, root=str(tmp_path), execute=False)
    assert len(written) == 3
    unit = tmp_path / "etc/systemd/system" / boot.UNIT_NAME
    assert unit.read_text().startswith("[Unit]")
    assert (tmp_path / "etc/fabric-table/topology").read_text() == "hosts a b\ncabling ring\n"
    removed = boot.remove(root=str(tmp_path), execute=False)
    assert not unit.exists() and not (tmp_path / "etc/fabric-table").exists()
    assert len(removed) == 3


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
    assert calls == [["systemctl", "daemon-reload"], ["systemctl", "enable", boot.UNIT_NAME]]
