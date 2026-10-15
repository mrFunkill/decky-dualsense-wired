"""
Extract Bluetooth HID traffic of a DualSense from a USBPcap capture of a USB
Bluetooth adapter (pcap, LINKTYPE_USBPCAP).

USBPcap URB -> HCI ACL (bulk endpoints) -> L2CAP -> HID transaction.
Only the standard Bluetooth/HID framing is decoded here; report contents are
printed as hex for manual analysis.

Usage:
  python -I dsbt_pcap.py summary  <file.pcap>
  python -I dsbt_pcap.py dump     <file.pcap> [--dir out|in] [--id 0x31] [--limit 20]
  python -I dsbt_pcap.py timeline <file.pcap> [--bucket 1.0]
"""

import argparse
import collections
import struct
import sys

LINKTYPE_USBPCAP = 249
URB_BULK = 3
PSM_NAMES = {0x0001: "SDP", 0x0011: "HID-ctrl", 0x0013: "HID-intr"}
HID_TRANSACTIONS = {
    0x0: "HANDSHAKE", 0x1: "HID_CONTROL", 0x4: "GET_REPORT", 0x5: "SET_REPORT",
    0x6: "GET_PROTOCOL", 0x7: "SET_PROTOCOL", 0x8: "GET_IDLE", 0x9: "SET_IDLE", 0xA: "DATA",
}
HID_REPORT_TYPES = {0: "other", 1: "input", 2: "output", 3: "feature"}


def read_pcap(path):
    with open(path, "rb") as f:
        data = f.read()
    magic = data[:4]
    if magic == b"\xd4\xc3\xb2\xa1":
        endian = "<"
    elif magic == b"\xa1\xb2\xc3\xd4":
        endian = ">"
    else:
        raise SystemExit("not a pcap file (pcapng is not supported, save as pcap)")
    linktype = struct.unpack_from(endian + "I", data, 20)[0]
    if linktype != LINKTYPE_USBPCAP:
        raise SystemExit("linktype %d is not USBPcap" % linktype)
    off = 24
    t0 = None
    while off + 16 <= len(data):
        sec, usec, incl, _orig = struct.unpack_from(endian + "IIII", data, off)
        off += 16
        pkt = data[off:off + incl]
        off += incl
        t = sec + usec / 1e6
        if t0 is None:
            t0 = t
        yield t - t0, pkt


def parse_urb(pkt):
    """Return (endpoint, from_device, payload) for bulk transfers carrying data."""
    if len(pkt) < 27:
        return None
    hdr_len, = struct.unpack_from("<H", pkt, 0)
    info = pkt[16]
    endpoint = pkt[21]
    transfer = pkt[22]
    data_len, = struct.unpack_from("<I", pkt, 23)
    if transfer != URB_BULK or data_len == 0:
        return None
    from_device = bool(endpoint & 0x80)
    completion = bool(info & 0x01)
    # OUT data travels in the submission, IN data in the completion.
    if from_device != completion:
        return None
    return endpoint, from_device, pkt[hdr_len:hdr_len + data_len]


class Decoder:
    def __init__(self):
        self.acl_buf = {}          # (direction, handle) -> bytearray being reassembled
        self.acl_expect = {}       # (direction, handle) -> expected L2CAP total length
        self.usb_buf = {False: bytearray(), True: bytearray()}
        self.cid_psm = {}          # (handle, cid) -> psm, for both local and remote CIDs
        self.pending = {}          # (handle, source cid) -> psm from a connection request

    def feed_usb(self, from_device, payload):
        """USB bulk transfers may split or join ACL packets; yield complete ACL packets."""
        buf = self.usb_buf[from_device]
        buf += payload
        while len(buf) >= 4:
            length, = struct.unpack_from("<H", buf, 2)
            if len(buf) < 4 + length:
                break
            pkt = bytes(buf[:4 + length])
            del buf[:4 + length]
            yield pkt

    def feed_acl(self, from_device, acl):
        """Reassemble L2CAP frames from ACL fragments; yield (handle, cid, payload)."""
        hf, length = struct.unpack_from("<HH", acl, 0)
        handle, pb = hf & 0x0FFF, (hf >> 12) & 0x3
        frag = acl[4:4 + length]
        key = (from_device, handle)
        if pb in (0, 2):  # first fragment
            if len(frag) < 4:
                return
            l2len, = struct.unpack_from("<H", frag, 0)
            self.acl_buf[key] = bytearray(frag)
            self.acl_expect[key] = l2len + 4
        elif pb == 1 and key in self.acl_buf:  # continuation
            self.acl_buf[key] += frag
        else:
            return
        if len(self.acl_buf[key]) >= self.acl_expect[key]:
            frame = bytes(self.acl_buf.pop(key)[:self.acl_expect.pop(key)])
            l2len, cid = struct.unpack_from("<HH", frame, 0)
            yield handle, cid, frame[4:4 + l2len]

    def feed_signalling(self, handle, payload):
        off = 0
        while off + 4 <= len(payload):
            code, _ident, length = struct.unpack_from("<BBH", payload, off)
            body = payload[off + 4:off + 4 + length]
            if code == 0x02 and len(body) >= 4:  # connection request: psm, source cid
                psm, scid = struct.unpack_from("<HH", body, 0)
                self.pending[(handle, scid)] = psm
                self.cid_psm[(handle, scid)] = psm
            elif code == 0x03 and len(body) >= 8:  # connection response: dest cid, source cid, result
                dcid, scid, result = struct.unpack_from("<HHH", body, 0)
                psm = self.pending.get((handle, scid))
                if psm is not None and result == 0:
                    self.cid_psm[(handle, dcid)] = psm
            off += 4 + length


def hid_records(path):
    """Yield dicts for every HID transaction found in the capture."""
    dec = Decoder()
    for t, pkt in read_pcap(path):
        urb = parse_urb(pkt)
        if not urb:
            continue
        _ep, from_device, payload = urb
        for acl in dec.feed_usb(from_device, payload):
            for handle, cid, l2 in dec.feed_acl(from_device, acl):
                if cid == 0x0001:
                    dec.feed_signalling(handle, l2)
                    continue
                psm = dec.cid_psm.get((handle, cid))
                if psm not in (0x11, 0x13) or not l2:
                    continue
                hdr = l2[0]
                ttype, param = hdr >> 4, hdr & 0x0F
                body = l2[1:]
                yield {
                    "t": t,
                    "dir": "in" if from_device else "out",
                    "channel": PSM_NAMES[psm],
                    "transaction": HID_TRANSACTIONS.get(ttype, "0x%x" % ttype),
                    "rtype": HID_REPORT_TYPES.get(param & 0x3, "?") if ttype in (0x4, 0x5, 0xA) else "",
                    "id": body[0] if body else None,
                    "len": len(body),
                    "data": body,
                }


def cmd_summary(args):
    groups = collections.OrderedDict()
    for r in hid_records(args.file):
        key = (r["dir"], r["channel"], r["transaction"], r["rtype"], r["id"], r["len"])
        g = groups.setdefault(key, {"count": 0, "first": r["t"], "last": r["t"]})
        g["count"] += 1
        g["last"] = r["t"]
    print("%-4s %-9s %-11s %-8s %-5s %5s %8s %9s %9s" % (
        "dir", "channel", "transaction", "type", "id", "len", "count", "first,s", "last,s"))
    for (d, ch, tr, rt, rid, ln), g in sorted(groups.items(), key=lambda kv: kv[1]["first"]):
        print("%-4s %-9s %-11s %-8s %-5s %5d %8d %9.3f %9.3f" % (
            d, ch, tr, rt, "-" if rid is None else "0x%02x" % rid, ln, g["count"], g["first"], g["last"]))


def cmd_dump(args):
    shown = 0
    for r in hid_records(args.file):
        if args.dir and r["dir"] != args.dir:
            continue
        if args.id is not None and r["id"] != args.id:
            continue
        if args.transaction and r["transaction"] != args.transaction:
            continue
        print("%9.4f %-3s %-8s %-10s %-7s len=%-4d %s" % (
            r["t"], r["dir"], r["channel"], r["transaction"], r["rtype"], r["len"], r["data"].hex(" ")))
        shown += 1
        if args.limit and shown >= args.limit:
            break


def parse_subpackets(report):
    """
    Hypothesis from captures 02: extended output reports 0x32..0x39 are
    [id][seq<<4] then sub-packets [type|flags][length][data], zero padding,
    and a CRC32 in the last 4 bytes. Returns (list of (header, data), trailing bytes ok).
    """
    body = report[2:-4]
    subs = []
    off = 0
    while off + 2 <= len(body) and body[off] != 0:
        hdr, length = body[off], body[off + 1]
        total = length * (2 if hdr & 0x40 else 1)  # 0x40: two blocks of `length`
        data = body[off + 2:off + 2 + total]
        if len(data) < total:
            return subs, False
        subs.append((hdr, data))
        off += 2 + total
    return subs, not any(body[off:])


def cmd_subpackets(args):
    layouts = collections.Counter()
    bad = 0
    examples = {}
    for r in hid_records(args.file):
        if r["dir"] != "out" or r["transaction"] != "DATA" or r["id"] is None or not 0x32 <= r["id"] <= 0x39:
            continue
        subs, clean = parse_subpackets(r["data"])
        if not clean:
            bad += 1
        layout = "0x%02x/%d: %s" % (r["id"], r["len"], " + ".join("%02x(%d)" % (h, len(d)) for h, d in subs))
        layouts[layout] += 1
        examples.setdefault(layout, r)
    for layout, n in layouts.most_common():
        print("%6d  %s" % (n, layout))
    print("reports with non-zero bytes after the last sub-packet: %d" % bad)
    if args.verbose:
        for layout, r in examples.items():
            print("\n== %s (t=%.3f)" % (layout, r["t"]))
            for h, d in parse_subpackets(r["data"])[0]:
                print("  %02x len=%-3d %s" % (h, len(d), d.hex(" ")))


def cmd_timeline(args):
    buckets = collections.defaultdict(collections.Counter)
    for r in hid_records(args.file):
        b = int(r["t"] // args.bucket)
        buckets[b]["%s 0x%02x/%d" % (r["dir"], r["id"] or 0, r["len"])] += 1
    for b in sorted(buckets):
        items = ", ".join("%s x%d" % kv for kv in sorted(buckets[b].items()))
        print("%7.1f s: %s" % (b * args.bucket, items))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("summary")
    s.add_argument("file")
    s.set_defaults(func=cmd_summary)
    d = sub.add_parser("dump")
    d.add_argument("file")
    d.add_argument("--dir", choices=["in", "out"])
    d.add_argument("--id", type=lambda v: int(v, 0))
    d.add_argument("--transaction")
    d.add_argument("--limit", type=int, default=20)
    d.set_defaults(func=cmd_dump)
    sp = sub.add_parser("subpackets")
    sp.add_argument("file")
    sp.add_argument("-v", "--verbose", action="store_true")
    sp.set_defaults(func=cmd_subpackets)
    tl = sub.add_parser("timeline")
    tl.add_argument("file")
    tl.add_argument("--bucket", type=float, default=1.0)
    tl.set_defaults(func=cmd_timeline)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
