# net-sircl 0.1.0

net-sircl supplies NCCL's v12 network-plugin interface for directly cabled DGX
Sparks. It uses both cable functions, derives routes from a topology file and
limits bytes in flight toward peers reached through relays. Wire version 2:
all hosts in a job must use plugins with the same wire version.

The release includes transport-core 0.1.0 (ABI 1), fabric-tool 0.1.0, examples,
CPU tests and single-GPU NCCL emulation tests. fabric-tool configures relays,
sender routes and packet markers, and provides a boot restoration unit. Its
status is implemented, CPU-tested, research-only; it has not run on a DGX Spark.

The relay marker installs root-owned at
`/usr/local/libexec/fabric-tool/fabric-tool-marker` with an adjacent SHA-256
record. Before every start, the executable, digest record and every parent
directory through `/` must be root-owned and not writable by group or others,
and the executable must match the recorded digest. Logs use
`/var/lib/fabric-tool/logs` with the same full parent-directory ownership and
permission checks; log creation does not follow symbolic links. These checks
prevent unprivileged local users from replacing root-executed marker code or
redirecting its log writes. See [marker security](../fabric-tool/README.md#marker-security).

Plugin hardware evidence covers a pair, ring of eight and path of four with
NCCL 2.32.3. Rings and paths use `NCCL_ALGO=Ring`. Paths need relays installed
with fabric-tool or an equivalent installer. See [measurements](measurements.md)
for plugin versus stock NCCL results and limitations.

All C sources and headers retain their bytes; SOURCE.json retains the measured
hash values. The 0.1.0 binary has not run on hardware. Its logged core digest
differs from the measured builds because that digest includes Python tools.
[Source provenance](../SOURCE.json) identifies the originating project and
revisions. [Component boundaries](transport-core.md) explain retained
compatibility files; [testing](testing.md) states exactly what CI runs.
