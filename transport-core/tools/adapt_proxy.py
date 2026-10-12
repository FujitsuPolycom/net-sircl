#!/usr/bin/env python3
"""Produce a guarded core compilation of a pristine SIRCL ABI9 proxy.

Only mechanical setup/provider operations change. The original source stays
byte-identical to the serving package. Unexpected source or match counts fail
before any output is written. The generated file is a build artifact.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def one(text: str, pattern: str, replacement: str) -> str:
    text, count = re.subn(pattern, lambda _: replacement, text, flags=re.S)
    if count != 1:
        raise ValueError(f"expected one reviewed region, got {count}: {pattern[:60]}")
    return text


def adapt(path: Path) -> str:
    data = path.read_bytes()
    identity = hashlib.sha256(data).hexdigest()
    expected = json.loads((ROOT / "PROXY_INPUTS.json").read_text())[path.name]
    if identity != expected or b"\r" in data:
        raise ValueError(f"unreviewed proxy bytes: {path.name} sha256={identity}")
    s = data.decode()
    prefix = "P2P" if "p2p" in path.name else "ROCE"
    marker = '#include <infiniband/verbs.h>'
    if s.count(marker) != 1:
        raise ValueError("provider include changed")
    s = s.replace(marker, marker + '\n#include <sircl_core/core.h>\n#include <sircl_core/verbs.h>')
    s = one(s, r'    int num = 0;\n    struct ibv_device \*\*list = ibv_get_device_list\(&num\);.*?    ibv_free_device_list\(list\);\n    if \(dev->ctx == NULL\)',
            '''    snprintf(dev->name, sizeof(dev->name), "%s", name);
    dev->gid_index = gid_index;
    dev->ctx = sircl_core_open_named_device(name);
    if (dev->ctx == NULL)''')
    s = one(s, r'        struct ibv_qp_init_attr attr;\n.*?        dev->qp\[p\] = ibv_create_qp\(dev->pd, &attr\);',
            f'        dev->qp[p] = sircl_core_create_rc_qp(dev->pd, dev->cq, {prefix}_SEND_DEPTH, 16);')
    s = one(s, r'        struct ibv_qp_attr init;\n.*?        int rc = ibv_modify_qp\(dev->qp\[p\], &init,\n.*?IBV_QP_ACCESS_FLAGS\);',
            f'        int rc = sircl_core_qp_init(dev->qp[p], {prefix}_PORT, IBV_ACCESS_REMOTE_WRITE);')
    s = one(s, r'    struct ibv_qp_attr rtr;\n.*?    int rc = ibv_modify_qp\(qp, &rtr,\n.*?IBV_QP_MIN_RNR_TIMER\);',
            f'''    struct sircl_core_qp_peer core_peer = {{
        .qpn = peer->qp_num[b][c->rank],
        .mtu = peer->mtu[b] < (uint32_t)dev->mtu ? peer->mtu[b] : (uint32_t)dev->mtu,
        .lid = peer->lid[b], .port = {prefix}_PORT, .gid_index = (uint8_t)dev->gid_index,
        .hop_limit = 64, .traffic_class = (uint8_t)c->traffic_class
    }};
    memcpy(core_peer.gid, peer->gid[b], 16);
    int rc = sircl_core_qp_rtr(qp, &core_peer);''')
    s = one(s, r'    struct ibv_qp_attr rts;\n.*?    rc = ibv_modify_qp\(qp, &rts,\n.*?IBV_QP_MAX_QP_RD_ATOMIC\);',
            '    rc = sircl_core_qp_rts(qp, 14, 7);')
    # Replace code identifiers, never log strings/comments. Protocol WR fields,
    # linked-list construction, flags, counters and fences remain original.
    names = {f"ibv_{n}": f"sircl_core_{n}" for n in (
        "query_port", "query_gid", "alloc_pd", "reg_mr", "dereg_mr", "destroy_qp",
        "destroy_cq", "dealloc_pd", "close_device", "post_send", "poll_cq")}
    tokens = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\n]*|\b(' +
                        '|'.join(names) + r')(?=\s*\()', re.S)
    s = tokens.sub(lambda m: names[m.group(1)] if m.group(1) else m.group(0), s)
    # CQ creation is a setup operation; its dimensions and sharing do not change.
    s = s.replace(f'ibv_create_cq(dev->ctx, {prefix}_CQ_DEPTH, NULL, NULL, 0)',
                  f'sircl_core_create_cq(dev->ctx, {prefix}_CQ_DEPTH)')
    if prefix == "P2P":
        old = 'dev->unacked[p] < window ? window - dev->unacked[p] : 0'
        new = 'sircl_core_window_room(window, dev->unacked[p])'
    else:
        old = 'dev->unacked[s->peer] < window ? (window - dev->unacked[s->peer]) / 16u * 16u : 0'
        new = 'sircl_core_window_room(window, dev->unacked[s->peer]) / 16u * 16u'
    if s.count(old) != 1:
        raise ValueError("reviewed nonzero-window room calculation changed")
    s = s.replace(old, new)
    return f'/* Generated from {path.name} sha256 {identity}; see transport-core/DESIGN.md. */\n' + s


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    result = adapt(args.input).encode()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if not args.output.exists() or args.output.read_bytes() != result:
        args.output.write_bytes(result)


if __name__ == "__main__":
    main()
