# Install and maintain the relay table

Follow the [rings and paths quickstart](../README.md#rings-and-paths) to run
the bundled tool. Use the same topology file for the plugin and tool. A pair
needs no relay table. A full ring needs one whenever communicating peers are
not direct neighbours; a path needs one for its closing connection.

The tool configures forwarding, endpoint routes, permanent neighbours and
RDMA transmit markers. It does not assign interface addresses or bring links
up. Each of the four RDMA functions needs an IPv4 address, and each cable's
ends must share a subnet per function class. All hosts must use the same
function-to-network-device mapping. The default lane relay limit is three.

## Plan, apply and verify

`plan` is the default and performs no host reads. Without facts it uses
placeholders. For exact install scripts, collect facts from all hosts and run:

```sh
python3 -m fabric_tool plan --topology "$topology" --facts facts.json --commands
```

Run from `fabric-tool/`, or use the installed `fabric-tool` entry point.
The README's `fabric` shell function supplies a topology and an SSH target for
every host. Its `facts` command creates a complete document in one read. Local
per-host `facts` outputs can instead be merged by passing repeated `--facts`
arguments to `plan`, `apply`, `verify` or `install-boot`.

`apply` reads hosts and previews reconciliation; only `--execute` changes
them. An executing apply rereads and verifies changed hosts. Always run
`verify` afterward, including when apply reports nothing to change. Verify
checks forwarding objects and that each installed lane destination resolves
over its expected device. Host identity, contradictory facts, cabling mismatch
and foreign ownership can block changes. A failure after execution starts can
leave partial state: inspect `status`, resolve the cause and reconcile again.

## Ownership and removal

Do not run two fabric installers on the same hosts. The tool refuses a
SparkRing ownership record. If SparkRing manages the table, follow its own
maintenance/removal procedure and disable its automatic restore before using
this tool. Keep the prior inputs so its table can be restored later.

For a table installed by this tool, using the README's shell function:

```sh
fabric status
fabric remove                         # preview
fabric remove --execute
```

Removal targets protocol-82 routes/neighbours, reserved-preference filters,
owned qdiscs, markers and the tool's record. Inspect ownership before using
the optional `--adopt` flag. Routes outside the relay plan are not adopted.

## Boot unit

`install-boot` runs locally on each Spark with root privileges; it refuses
`--ssh`. It writes `fabric-table.service`, topology and complete facts under
`/etc/fabric-table/`, a tool copy under `/usr/local/lib/fabric-tool`, and
`/usr/local/bin/fabric-tool`. The unit runs `apply --boot`: restore missing
objects and markers, remove nothing. It is a oneshot unit, not continuous
marker supervision. Network addresses must be ready at `network-online.target`.

After reboot, run `fabric verify` from the controlling workstation and inspect
`systemctl status fabric-table.service` on the restarted host. To disable
restoration, run locally on every Spark:

```sh
sudo /usr/local/bin/fabric-tool remove-boot
sudo /usr/local/bin/fabric-tool remove-boot --execute
```

This removes the unit and saved inputs. It leaves the installed tool copy and
live table; use `remove` to remove the table itself.

## Validation boundary

The tool is equivalence-tested against SparkRing's planner on CPU, including
exact command strings. Its simulated-host tests cover apply, verify, remove
and boot restore. **Hardware validation is pending.** Neither CPU tests nor
earlier plugin measurements establish this installer's real forwarding,
systemd reboot behavior or performance. See [design](../fabric-tool/DESIGN.md),
[hardware test plan](../fabric-tool/HARDWARE-TEST-PLAN.md) and
[testing](testing.md) for scope and reproducible CPU commands.
