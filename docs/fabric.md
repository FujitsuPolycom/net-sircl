# Configure relays between Sparks

Use the same topology file for the plugin and fabric-tool. A pair needs no
relays. A ring needs them when communicating peers are not direct neighbours.
A path needs them because NCCL's ring algorithm connects the two end hosts,
which share no cable. A *layout* is the cabling and ordered hosts; a *group*
in the tool's output is the set of hosts described by one topology file.

## How relays work

The sender installs an IP route for a far host through its first adjacent
Spark, plus a permanent neighbour entry mapping the next hop to its MAC
address. A marker process on the sender's NIC changes each outgoing RDMA
packet's EtherType to `0x88b4 + k`, where k is the number of relays left.
Each intermediate NIC forwards the packet from ingress to egress (hardware
hairpin forwarding), changes the next-hop MAC and decrements the tag. The last
relay restores IPv4's EtherType, `0x0800`.

Queue pairs use flow label 0. The marker selects packets by destination address,
not UDP port. EtherType lies outside RDMA's invariant CRC (ICRC), so relays
can change it without invalidating the packet. Rewriting UDP fields would
affect that checksum. Egress can use the same function class as ingress, or
the sibling function on the other port; the default is same-class for a full ring and sibling for a path.

Each forwarding queue holds 512 KiB and cannot pause senders. The plugin's
relay window limits bytes in flight, but does not enforce an aggregate limit
across several senders. The plan reports lanes sharing each queue and a budget
based on 75% of its capacity divided by the reported load factor.

The tool does not assign addresses or bring links up. Each of the four RDMA
functions needs IPv4. Each cable's ends must share a subnet on the primary
functions (`rocep1...`) and another on the secondary functions (`roceP2...`).
All hosts must use the same function-to-network-interface mapping. By default
a lane may cross three relays (`--max-relays`); the plugin hardware measurements
cover at most two. Three relays have not been measured for this release.

## Plan, apply and verify

Run from `fabric-tool/` on a Linux workstation with SSH access to every host.
Replace the topology path, names and SSH targets below. Names must match each
host's `hostname`; the SSH account needs noninteractive sudo. Marker compilation
needs the build dependencies from the README on every host.

```sh
topology=/absolute/path/to/path4.topology
python3 -m fabric_tool --topology "$topology"  # offline plan; no host reads
fabric() {
  python3 -m fabric_tool "$@" --topology "$topology" \
    --ssh node-0=USER@HOST_0 --ssh node-1=USER@HOST_1 \
    --ssh node-2=USER@HOST_2 --ssh node-3=USER@HOST_3
}
# Read every host's function interfaces, MAC and IPv4 addresses.
fabric facts --output facts.json
# Preview, then compile the RDMA packet marker on every host.
fabric install-marker --scripts
fabric install-marker --execute
# Preview, then install routes, neighbour entries, forwarding rules and markers.
fabric apply --facts facts.json
fabric apply --facts facts.json --execute
# Check installed objects and every lane's IP route; require exit zero.
fabric verify --facts facts.json
```

Host-changing commands only execute with `--execute`. For complete scripts
without host access, run `python3 -m fabric_tool plan --topology FILE
--facts facts.json --commands`. Repeated `--facts` files merge, rejecting
contradictory entries. Facts must cover every topology host, even for local apply.
For other layouts, repeat `--ssh NAME=USER@HOST` once per topology host.

An executing apply rereads and verifies changed hosts. Always run `verify`
afterward, even if apply reports nothing to change. Identity mismatches,
contradictory facts, cabling mismatches and foreign ownership block changes.
A failure during execution can leave partial state: inspect `status` and
resolve the cause before using the relays. Read sections end with a newline
so digest records cannot hide following ownership observations. Marker starts
use util-linux `setsid -f` with stdin from `/dev/null` and stdout and stderr on
the root-created log, allowing the SSH command to finish while markers run.

For a manual route check on a sending host, substitute a far function's IPv4
address and the local sending function's address:

```sh
ip route get FAR_ADDRESS from LOCAL_ADDRESS
ethtool -S NETDEV | grep -Ei 'discard|drop|out_of_buffer'
cat /sys/class/infiniband/FUNCTION/hw_counters/out_of_sequence
cat /sys/class/infiniband/FUNCTION/hw_counters/packet_seq_err
```

Check the selected interface and compare counters before and after traffic.
See the [hardware procedure](../fabric-tool/HARDWARE-TEST-PLAN.md) for all counters.

## Ownership

The marker runs as root from
`/usr/local/libexec/fabric-tool/fabric-tool-marker`. Installation records its
SHA-256 in the adjacent `.sha256` file. Before every start, the executable,
record and every parent directory through `/` must be root-owned and not
writable by group or others, and the executable must match the digest.
Logs are created without following symbolic links under
`/var/lib/fabric-tool/logs`; every existing parent through `/` must pass the
same ownership and permission checks. An unprivileged user cannot replace
the marker or redirect root's log writes through writable parent directories.
See [marker security](../fabric-tool/README.md#marker-security) for custom paths
and refusal handling.

fabric-tool marks installed IP routes and neighbour entries with protocol 82,
and forwarding filters with `tc` ingress preferences 11Ã¢â‚¬â€œ17. Its ownership
record is `/run/fabric-table/relay-state.json`. `remove` targets marked objects,
owned ingress qdiscs, marker processes and that record.

It refuses hosts with `/run/sparkring-fabric/relay-state.json`, written by the
SparkRing project's relay installer. Remove those relays with that installer's
tooling and disable its restore service first. Keep its inputs for restoration.

Using the shell function above, run `fabric status`, then `fabric remove` to
preview and `fabric remove --execute` to remove. Inspect ownership before
using `--adopt`; routes outside the relay plan are not adopted.

## Boot restoration

Install the repository on every Spark. Copy the same topology and a `facts.json`
covering every host to each Spark. Run locally on **each Spark**:

```sh
sudo /opt/net-sircl/bin/fabric-tool install-boot \
  --topology /etc/net-sircl.topology --facts /absolute/path/to/facts.json
sudo /opt/net-sircl/bin/fabric-tool install-boot \
  --topology /etc/net-sircl.topology --facts /absolute/path/to/facts.json --execute
```

This writes topology and facts under `/etc/fabric-table/`, copies the tool to
`/usr/local/lib/fabric-tool` and `/usr/local/bin/fabric-tool` (separate from the
`/opt/net-sircl` install), enables `fabric-table.service` and applies the relays.
At boot the oneshot unit runs `apply --boot`, restoring missing objects and
markers without removing anything. It does not continuously supervise markers.
Addresses must be ready at `network-online.target`.

After reboot run `fabric verify --facts facts.json` from the workstation and
`systemctl status fabric-table.service` on the restarted host. To disable
restoration run `sudo /usr/local/bin/fabric-tool remove-boot` locally, then
repeat with `--execute`. This removes the unit and inputs but leaves live
relays and the tool copy; use `remove` for live relays.

## Validation boundary

Status: research-only. An earlier revision installed and verified ring and
path tables on a ring of four DGX Sparks, passed plugin test arms on the path
table, removed its table and restored SparkRing's table. Two defects found
during that exercise were fixed afterwards on CPU; this fixed version has not
yet been rerun on hardware. See the [release notes](release-0.1.0.md).
Standalone tests cover planning, facts, generated Bash scripts, marker security,
boot previews and mocked boot operations. They do not simulate an entire
network or establish reboot restoration. See [testing and CI](testing.md),
[design](../fabric-tool/DESIGN.md) and the
[hardware procedure](../fabric-tool/HARDWARE-TEST-PLAN.md).
