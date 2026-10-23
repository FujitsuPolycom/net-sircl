"""Relay configuration for directly cabled DGX Sparks.

Reads topology and facts, derives forwarding rules and sender routes, inspects
hosts, and reconciles explicitly owned objects. Uses Python's standard library.
Status: research-only. HARDWARE-TEST-PLAN.md defines physical qualification.
See NOTICE for SparkRing attribution.
"""
