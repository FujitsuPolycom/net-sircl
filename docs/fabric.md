# Relay setup for paths

The plugin consumes a configured fabric. It does not change networking.
Direct cables require active RoCE v2 interfaces with IPv4 GIDs. Relayed peers
also require endpoint routes and intermediate ConnectX-7 hairpin forwarding
tables that preserve the destination address and PCI-function class, carrying
RoCE v2 with flow label 0. Reinstall transient relay state after reboot.

## SparkRing provider

SparkRing includes a fabric installer at `spark_transport/sircl`. Its input is
a `sircl-ring-site/v1` site file describing the owner's hosts and fabric; this
is different from the small `net-sircl` topology file. Use the SparkRing checkout's
fabric setup documentation to create that site file and match the physical layout.

For an existing SparkRing installation, these are the installer entry points:

```sh
cd /path/to/SparkRing/spark_transport/sircl
python3 -m sparkring_sircl.fabric up --help
python3 -m sparkring_sircl.fabric up --site /path/to/site.json --groups '0-3'
python3 -m sparkring_sircl.fabric up --site /path/to/site.json --groups '0-3' --apply
```

The second command inspects the selected hosts remotely and previews changes;
the third applies them. Choose groups for the actual site, review the plan and
follow SparkRing's maintenance procedure. `0-3` denotes the four positions of
the example path, not an instruction to change an arbitrary installation.
None of these commands is needed for a directly cabled pair.

## Standalone fabric tool: integration pending

A separate standalone fabric tool is being prepared. It is not included in
net-sircl 0.1.0. Add its repository link, supported release, installation steps,
verified plan/apply commands and reboot behavior here after integration is
validated. No command names for that tool are assumed by this release.
