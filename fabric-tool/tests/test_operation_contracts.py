"""Configuration identity, persisted invocation and reconciliation ownership contracts."""
import dataclasses
import json
import shlex

import pytest

from fabric_tool import boot, cli, commands, diff, facts, model, ops, plan, state, topology
from test_facts import _document, RING4


def custom_document():
    document = _document()
    for host in document['hosts']:
        for index, function in enumerate(host['functions'].values()):
            function['netdev'] = f'fabric{index}'
    return document


def test_plans_keep_their_bindings_after_other_configurations_are_resolved():
    topo = topology.parse(RING4)
    customized = facts.bind(custom_document(), topo)
    custom_plan = plan.build_plan(topo.group, customized)
    before = custom_plan.to_json()
    ordinary = facts.bind(_document(), topo)
    plan.build_plan(topo.group, ordinary)
    assert custom_plan.to_json() == before
    assert any('fabric' in route.netdev for host in custom_plan.hosts for route in host.routes)
    assert topo.bindings == model.DEFAULT_BINDINGS
    assert customized.bindings != ordinary.bindings


@pytest.mark.parametrize('operation', ['apply', 'verify'])
def test_custom_facts_are_bound_before_the_first_host_read(tmp_path, monkeypatch, operation):
    path = tmp_path / 'ring.topology'
    path.write_text(RING4)
    document = tmp_path / 'facts.json'
    document.write_text(json.dumps(custom_document()))
    class ReadReached(Exception):
        pass
    def gather(*args, bindings, **kwargs):
        script = commands.read_script(commands.MarkerConfig(), bindings=bindings)
        assert 'tc -j qdisc show dev fabric0 ' in script
        assert 'tc -j qdisc show dev enp1s0f0np0 ' not in script
        raise ReadReached()
    monkeypatch.setattr(ops, 'gather', gather)
    with pytest.raises(ReadReached):
        cli.main([operation, '--topology', str(path), '--facts', str(document), '--host', 'node-0'])


def test_boot_preserves_preexisting_qdisc_ownership():
    topo = topology.parse(RING4)
    built = plan.build_plan(topo.group, facts.bind(_document(), topo))
    host = next(host for host in built.hosts if host.filter_netdevs)
    marker = commands.MarkerConfig()
    observed = state.HostState(host.position, host.name, '', True, hostname=host.name)
    for device in host.filter_netdevs:
        observed.ingress_qdisc[device] = 'ingress'
    restored = diff.compare_boot(observed, host, built, layout_name=topo.group.name, marker=marker, adopt=False)
    record_line = next(line for line in restored.commands if 'tee ' + commands.RECORD_PATH in line)
    observed.record = json.loads(shlex.split(record_line)[2])
    assert observed.record['qdiscs'] == []
    removed = diff.compare_down(observed, group=topo.group.label, marker=marker, adopt=False)
    assert not any('tc qdisc del' in line for line in removed.commands)


def test_restore_invocation_retains_configuration_and_execution():
    invocation = boot.RestoreInvocation(marker='/opt/fabric/marker', max_relays=5, host='node-2',
                                       skip_hostname_check=True)
    text = boot.unit_text(invocation=invocation)
    argv = shlex.split(next(line[len('ExecStart='):] for line in text.splitlines() if line.startswith('ExecStart=')))
    parsed = cli._parser().parse_args(argv[1:])
    assert parsed.execute and parsed.boot
    assert parsed.marker == invocation.marker and parsed.max_relays == 5 and parsed.host == 'node-2'
    assert parsed.skip_hostname_check


@pytest.mark.parametrize('identifier', ['bad;name', 'bad/name', 'bad$name', '-option', 'bad name'])
def test_invalid_device_identifiers_are_rejected_before_generation(identifier):
    with pytest.raises(model.FabricError, match='device identifier'):
        topology.parse('hosts a b\ncabling path\nport0 ' + identifier + ' secondary\n')


def test_systemd_argument_escaping():
    assert boot.systemd_arg('a%$"\\b') == '"a%%$$\\"\\\\b"'
    with pytest.raises(ValueError, match='control characters'):
        boot.systemd_arg('line\nbreak')
