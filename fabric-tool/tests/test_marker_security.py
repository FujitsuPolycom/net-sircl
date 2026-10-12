"""Standalone security regressions using synthetic hosts and unprivileged Bash.

No SSH, sudo, NIC changes or parent-project simulator are used.
"""
from __future__ import annotations

import hashlib
import subprocess

import pytest

from fabric_tool import cli, commands, diff, facts, model, ops, plan, state, topology
from test_facts import RING4, _document

MARKER = commands.MarkerConfig()


@pytest.fixture
def environment(tmp_path, monkeypatch):
    topo = topology.parse(RING4)
    bound = facts.bind(_document(), topo)
    built = plan.build_plan(topo.group, bound)
    states = {}
    for position, name in enumerate(topo.hosts):
        ports = bound.host(position).ports
        states[position] = state.HostState(
            position, name, name, True, hostname=name,
            links={p.netdev: state.Link(p.netdev, p.mac, ((p.address, p.prefixlen),), 'UP') for p in ports},
            rdma=dict(zip(model.RDMA_DEVICES, model.FABRIC_NETDEVS)),
            marker_binary='a' * 64, marker_recorded='a' * 64,
            marker_owners={p: ('root', 0o755) for p in (*MARKER.checked_paths, *MARKER.log_dirs)})
    monkeypatch.setattr(ops, 'gather', lambda *args, **kwargs: states)
    path = tmp_path / 'ring.topology'
    path.write_text(RING4)
    argv = ['--topology', str(path)]
    for name in topo.hosts:
        argv += ['--ssh', f'{name}={name}']
    return topo, built, states, argv


def forbid_execution(target, script, timeout):
    # Apply may still read installed lane routes when reporting blockers.
    assert script.startswith('set -u\n') and 'ip -o route get ' in script
    assert all(line in ('set -u', 'echo @@end', commands.SECTION_END) or line.startswith(('echo @@route:', 'ip -o route get '))
               for line in script.splitlines()), 'blocked apply attempted a mutation'
    return ops.Result(0, '', '')


@pytest.mark.parametrize('path', tuple(dict.fromkeys((*MARKER.checked_paths, *MARKER.log_dirs))))
@pytest.mark.parametrize('owner,mode', [('operator', 0o755), ('root', 0o775), ('root', 0o777)])
def test_unsafe_executable_record_or_any_parent_blocks_apply(environment, capsys, path, owner, mode):
    _, _, states, argv = environment
    states[1].marker_owners[path] = (owner, mode)
    assert cli.main(['apply', *argv, '--execute'], executor=forbid_execution) == 3
    assert f'{path} is owned by {owner} with mode {mode:o}' in capsys.readouterr().out


@pytest.mark.parametrize('recorded', [None, 'b' * 64])
def test_missing_or_changed_digest_blocks_apply_and_selects_reinstall(environment, capsys, recorded):
    _, _, states, argv = environment
    states[2].marker_recorded = recorded
    assert cli.main(['apply', *argv, '--execute'], executor=forbid_execution) == 3
    capsys.readouterr()
    installed = []

    def install(target, script, timeout):
        installed.append(target)
        assert f'sudo -n tee {MARKER.record}' in script
        assert all(check in script for check in commands.marker_checks(MARKER, 'install the marker'))
        return ops.Result(0, '', '')

    assert cli.main(['install-marker', *argv, '--execute'], executor=install) == 0
    assert installed == ['node-2']


def test_missing_marker_blocks_apply(environment):
    _, _, states, argv = environment
    states[0].marker_binary = None
    assert cli.main(['apply', *argv, '--execute'], executor=forbid_execution) == 3


def test_plan_apply_and_boot_scripts_check_every_start(environment):
    topo, built, states, _ = environment
    scripts = list(cli._install_scripts(built, MARKER).values())
    for compare in (diff.compare_up, diff.compare_boot):
        for position, host_state in states.items():
            result = compare(host_state, built.host(position), built, layout_name=topo.group.name,
                             marker=MARKER, adopt=False)
            assert not result.blockers
            scripts.append('\n'.join(result.commands))
    starts = 0
    for script in scripts:
        lines = script.splitlines()
        for index, line in enumerate(lines):
            if line.startswith('sudo -n setsid -f '):
                starts += 1
                assert tuple(lines[index - 4:index]) == (*commands.log_dir_setup(MARKER),
                                                       *commands.marker_checks(MARKER))
    assert starts == 3 * 4 * 4


def test_marker_from_another_executable_is_replaced(environment):
    topo, built, states, _ = environment
    host = built.host(0)
    rules = host.markers[0]
    words = ' '.join(f'--rule {destination}=0x{value:04x}' for destination, value in rules.rules)
    states[0].markers = state.parse_markers(
        f'4100 /var/tmp/fabric-tool-marker --device {rules.device} {words} --managed')
    result = diff.compare_up(states[0], host, built, layout_name=topo.group.name, marker=MARKER, adopt=False)
    assert not result.blockers
    assert any(c.kind == 'marker' and c.action == 'replace' for c in result.changes)
    assert commands.marker_stop(MARKER, 4100)[0] in result.commands
    assert any(MARKER.path in line and line.startswith('sudo -n setsid -f ') for line in result.commands)


def test_read_parser_retains_security_observations():
    parsed = state.parse(0, 'node-0', 'node-0', returncode=0, stderr='', stdout=(
        '@@marker-binary\n' + 'a' * 64 + f'  {MARKER.path}\n@@marker-record\n' + 'b' * 64 +
        f'\n@@marker-owners\nroot 755 /\noperator 775 {MARKER.directory}\n@@end\n'))
    assert parsed.marker_binary == 'a' * 64 and parsed.marker_recorded == 'b' * 64
    assert parsed.marker_owners == {'/': ('root', 0o755), MARKER.directory: ('operator', 0o775)}


def run_shell(script):
    return subprocess.run(['bash', '-c', script], capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize('path', MARKER.checked_paths)
@pytest.mark.parametrize('ownership', ['operator 755', 'root 775', 'root 777', ''])
def test_shell_refuses_unsafe_or_missing_marker_paths_before_launch(path, ownership):
    # Inject only stat observations; execute the generated Bash guard itself.
    stat = (f'stat() {{ if [ "${{@: -1}}" = {commands.q(path)} ]; then '
            f'printf "%s\\n" {commands.q(ownership)}; else echo "root 755"; fi; }}\n')
    result = run_shell(commands.HEADER + '\n' + stat + commands.marker_checks(MARKER)[0] + '\necho LAUNCHED')
    assert result.returncode == commands.REFUSE_EXIT and 'LAUNCHED' not in result.stdout
    assert path in result.stderr


@pytest.mark.parametrize('path', ['/var/lib', '/var', '/'])
def test_shell_refuses_unsafe_log_ancestors_before_directory_creation(path):
    stat = (f'stat() {{ if [ "${{@: -1}}" = {commands.q(path)} ]; then '
            'echo "root 775"; else echo "root 755"; fi; }\n')
    result = run_shell(commands.HEADER + '\n' + stat + commands.log_dir_setup(MARKER)[0] + '\necho CREATED')
    assert result.returncode == commands.REFUSE_EXIT and 'CREATED' not in result.stdout
    assert path in result.stderr


def test_shell_checks_actual_digest_and_refuses_changed_bytes(tmp_path):
    executable = tmp_path / 'marker'
    record = tmp_path / 'marker.sha256'
    executable.write_bytes(b'installed marker bytes')
    record.write_text(hashlib.sha256(executable.read_bytes()).hexdigest() + '\n')
    check = commands.marker_checks(MARKER)[1].replace(MARKER.record, str(record)).replace(MARKER.path, str(executable))
    assert run_shell(check).returncode == 0
    executable.write_bytes(b'changed marker bytes')
    result = run_shell(check)
    assert result.returncode == commands.REFUSE_EXIT
    assert 'differs from the SHA-256' in result.stderr


def test_log_launch_does_not_follow_a_symlink(tmp_path):
    victim = tmp_path / 'unrelated'
    victim.write_text('untouched')
    log = tmp_path / 'marker.log'
    log.symlink_to(victim)
    result = subprocess.run(['bash', '-c', commands.MARKER_LAUNCH, str(log), 'printf', 'marker output'],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert victim.read_text() == 'untouched'
    assert not log.is_symlink() and log.read_text() == 'marker output'
