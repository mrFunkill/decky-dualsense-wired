"""Print a PE file's imported DLLs and its exports (marking forwarders): pe_info.py <dll>."""

import struct
import sys


def parse(path):
    d = open(path, "rb").read()
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    magic = struct.unpack_from("<H", d, pe + 24)[0]
    opt = pe + 24
    ddir = opt + (96 if magic == 0x10B else 112)
    nsec = struct.unpack_from("<H", d, pe + 6)[0]
    soh = struct.unpack_from("<H", d, pe + 20)[0]
    secs = []
    for i in range(nsec):
        vs, va, rawsz, rawp = struct.unpack_from("<IIII", d, opt + soh + 40 * i + 8)
        secs.append((va, max(vs, rawsz), rawp))

    def off(rva):
        for va, sz, raw in secs:
            if va <= rva < va + sz:
                return rva - va + raw

    def cstr(rva):
        o = off(rva)
        return d[o:d.index(b"\0", o)].decode()

    erva, esize = struct.unpack_from("<II", d, ddir)
    exports = []
    if erva:
        e = off(erva)
        base, nfun, nnames, afun, anames, aord = struct.unpack_from("<IIIIII", d, e + 16)
        for i in range(nnames):
            name = cstr(struct.unpack_from("<I", d, off(anames) + 4 * i)[0])
            ordi = struct.unpack_from("<H", d, off(aord) + 2 * i)[0]
            frva = struct.unpack_from("<I", d, off(afun) + 4 * ordi)[0]
            fwd = cstr(frva) if erva <= frva < erva + esize else ""
            exports.append((name, ordi + base, fwd))
    irva = struct.unpack_from("<I", d, ddir + 8)[0]
    imports = []
    o = off(irva) if irva else None
    while o is not None:
        name_rva = struct.unpack_from("<I", d, o + 12)[0]
        if not name_rva:
            break
        imports.append(cstr(name_rva))
        o += 20
    return magic, imports, exports


if __name__ == "__main__":
    magic, imports, exports = parse(sys.argv[1])
    print("PE32+" if magic == 0x20B else "PE32", "imports:", ", ".join(imports))
    own = [(n, o) for n, o, f in exports if not f]
    fwd = [f for n, o, f in exports if f]
    print("exports: %d, forwarded: %d, own: %s" % (len(exports), len(fwd), own))
    print("forward targets:", sorted({f.split(".")[0] for f in fwd}))
