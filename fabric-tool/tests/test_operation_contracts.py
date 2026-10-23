"""Configuration identity, persisted invocation and reconciliation ownership contracts."""
import dataclasses
import json
import shlex

import pytest

from fabric_tool import boot, cli, commands, diff, facts, model, ops, plan, state, topology
from test_facts import _document, RING4
from test_marker_security import environment


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


def test_restore_honors_explicit_hostname_policy():
    topo = topology.parse(RING4)
    built = plan.build_plan(topo.group, facts.bind(_document(), topo))
    host = built.hosts[0]
    observed = state.HostState(host.position, host.name, '', True, hostname='different-host')
    arguments = dict(layout_name=topo.group.name, marker=commands.MarkerConfig(), adopt=False)
    guarded = diff.compare_boot(observed, host, built, **arguments)
    assert any('different-host' in blocker for blocker in guarded.blockers)
    permitted = diff.compare_boot(observed, host, built, check_hostname=False, **arguments)
    assert not any('different-host' in blocker for blocker in permitted.blockers)
    assert '$(hostname)' not in (permitted.script or '')


@pytest.mark.parametrize('mode,removes_stale', [('up', True), ('boot', False), ('down', True)])
def test_resource_policy_preserves_restoration_and_removal_contracts(mode, removes_stale):
    topo = topology.parse(RING4)
    built = plan.build_plan(topo.group, facts.bind(_document(), topo))
    host = built.hosts[0]
    observed = state.HostState(host.position, host.name, '', True, hostname=host.name)
    observed.routes.append(state.RouteEntry('198.51.100.19', 32, host.bindings.netdevs[0],
                                            '192.0.2.1', 'link', 82, '82', 0, None, 'unicast'))
    marker = commands.MarkerConfig()
    if mode == 'down':
        result = diff.compare_down(observed, group=topo.group.label, marker=marker, adopt=False)
    else:
        compare = diff.compare_up if mode == 'up' else diff.compare_boot
        result = compare(observed, host, built, layout_name=topo.group.name, marker=marker, adopt=False)
    removed = any(change.kind == 'route' and change.action == 'remove' and
                  change.key == '198.51.100.19/32' for change in result.changes)
    assert removed == removes_stale


def test_remove_preserves_owned_qdisc_with_unrelated_filters():
    marker = commands.MarkerConfig()
    observed = state.HostState(0, 'node-0', '', True, hostname='node-0')
    device = observed.bindings.netdevs[0]
    observed.record = {'group': 'path:0-1', 'qdiscs': [device]}
    observed.ingress_qdisc[device] = 'ingress'
    observed.filters[device] = [state.FilterEntry(device, 99, 0, 'flower', 0x0800, 1,
                                                 True, True, None, None, None, None)]
    result = diff.compare_down(observed, group='path:0-1', marker=marker, adopt=False)
    assert not any(change.kind == 'qdisc' and change.action == 'remove' for change in result.changes)


def test_service_invocation_dispatches_restoration(environment, tmp_path, monkeypatch):
    _, _, _, arguments = environment
    topology_path = arguments[arguments.index('--topology') + 1]
    facts_path = tmp_path / 'boot-facts.json'
    facts_path.write_text(json.dumps(_document()))
    monkeypatch.setattr(boot, 'TOPOLOGY_INPUT', topology_path)
    monkeypatch.setattr(boot, 'FACTS_INPUT', str(facts_path))
    monkeypatch.setattr(cli, '_route_check', lambda *args, **kwargs: 0)
    service = boot.unit_text(invocation=boot.RestoreInvocation(host='node-0'))
    argv = shlex.split(next(line[len('ExecStart='):] for line in service.splitlines()
                            if line.startswith('ExecStart=')))
    class RestorationDispatched(Exception):
        pass
    def executor(target, script, timeout):
        assert 'sudo -n tc filter add' in script
        raise RestorationDispatched()
    with pytest.raises(RestorationDispatched):
        cli.main(argv[1:], executor=executor)
