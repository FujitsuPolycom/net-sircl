# net-sircl 0.1.0

net-sircl supplies NCCL's v12 network-plugin interface for directly cabled DGX
Sparks. It uses both cable functions, derives routes from a topology file and
limits bytes in flight toward peers reached through relays. Wire version 2:
all hosts in a job must use plugins with the same wire version.

The release includes transport-core 0.1.0 (ABI 1), fabric-tool 0.1.0, examples,
CPU tests and single-GPU NCCL emulation tests. fabric-tool configures relays,
sender routes and packet markers, and provides a boot restoration unit. Its
status is research-only. The fabric tool's hardware test plan ran on a ring
of four DGX Sparks using upstream revision `760413a8`: the ring table and path
table were installed and verified, plugin test arms passed on the path table,
the tool's table was removed, and SparkRing's table was restored. This is
evidence for those operations, not for reboot restoration or the entire
two-ring qualification procedure.

That hardware exercise exposed two defects, fixed afterwards on CPU in
upstream revision `47de221a` and included here:

- Read-script sections lacked a terminating newline. The 64-character marker
  digest record swallowed the next section header, hiding ownership data and
  blocking every apply. Every read section, including route checks, now ends
  with a newline.
- The backgrounded marker start held the SSH session's output open, so
  `apply --execute` waited for the 180-second timeout even after installation.
  Starts now use foreground `sudo -n setsid -f`, with stdin from `/dev/null`
  and marker stdout and stderr directed to a log created by root.

CPU regression tests exercise both generated scripts under Bash. The fixed
version has not yet been rerun on hardware. Linux hosts require util-linux
`setsid` with `-f` support; marker paths and command-line arguments are unchanged.

The relay marker installs root-owned at
`/usr/local/libexec/fabric-tool/fabric-tool-marker` with an adjacent SHA-256
record. Before every start, the executable, digest record and every parent
directory through `/` must be root-owned and not writable by group or others,
and the executable must match the recorded digest. Logs use
`/var/lib/fabric-tool/logs` with the same full parent-directory ownership and
permission checks; log creation does not follow symbolic links. These checks
prevent unprivileged local users from replacing root-executed marker code or
redirecting its log writes. See [marker security](../fabric-tool/README.md#marker-security).

Plugin hardware evidence covers a pair, a ring of four, a ring of eight and a
path of four with NCCL 2.32.3. On a ring of four DGX Sparks every test line
passed with bus bandwidth 0.99-1.01 of stock NCCL (all-reduce 23.46 against
23.28 GB/s at 256 MiB); on a path of four through two relays, with fabric-tool's
path table, every line passed at 17.67 GB/s all-reduce at 256 MiB, where stock
NCCL alone timed out, and a PyTorch DDP check passed on all four ranks.
`NET_SIRCL_RELAXED_ORDERING=0` against `=1` measured no difference on a pair
(ratios 0.98-1.02).

Known issue: on one cabled pair, one run in four of the all-gather line under
CUDA-graph capture stopped with `transport retry counter exceeded` on one PCI
function; three further runs passed. The cause is not yet established.

Rings and paths use `NCCL_ALGO=Ring`. Paths need relays installed
with fabric-tool or an equivalent installer. See [measurements](measurements.md)
for plugin versus stock NCCL results and limitations.

All C sources and headers retain their bytes; SOURCE.json retains the measured
hash values. The 0.1.0 binary has not run on hardware. Its logged core digest
differs from the measured builds because that digest includes Python tools.
[Source provenance](../SOURCE.json) identifies the originating project and
revisions. [Component boundaries](transport-core.md) explain retained
compatibility files; [testing](testing.md) states exactly what CI runs.
