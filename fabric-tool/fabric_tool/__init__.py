"""The relay table of directly cabled DGX Sparks, as a standalone installer.

Derived from SparkRing's SIRCL relay plan installer
(``sparkring_sircl.fabric``) and route module (``sparkring_sircl.routes``);
see NOTICE. The tool reads the topology file of the NCCL network plugin
``net-sircl``, discovers and verifies the cabling, and installs, verifies,
removes and persists the relay table: ConnectX-7 hardware hairpin forwarding
rules, the routes that aim RoCE v2 traffic at them, and the RDMA-TX rules
that tag it. It needs Python 3's standard library alone.

Modules: :mod:`.topology` (the topology file and the one layout it names),
:mod:`.model` (cables, fabrics, lanes), :mod:`.plan` (the relay plan and its
checks), :mod:`.facts` (addresses and MACs), :mod:`.commands` (shell
commands and ownership marks), :mod:`.state` (what a host holds),
:mod:`.diff` (comparison and apply scripts), :mod:`.ops` (read, compare,
apply), :mod:`.report` (text), :mod:`.boot` (the systemd unit) and
:mod:`.cli` (``python3 -m fabric_tool``).

Status: implemented; CPU-tested. Equivalence with SparkRing's installer is
checked by the tests (``tests/test_equivalence.py``) on a ring of four, a
ring of eight, a path of four and a path of three. The tool has not run on
hardware; ``HARDWARE-TEST-PLAN.md`` is the plan for that validation.
"""
