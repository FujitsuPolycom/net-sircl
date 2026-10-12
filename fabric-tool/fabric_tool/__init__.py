"""Relay installer for directly cabled DGX Sparks.

Derived from the SparkRing relay installer; see NOTICE. Reads a topology,
plans forwarding rules and sender routes, inspects hosts and applies changes.
Uses only Python's standard library. Implemented and CPU-tested, research-only;
not run on hardware. See HARDWARE-TEST-PLAN.md for the hardware procedure.
"""
