"""
Build the cfgmgr32 proxy for x86_64 and i386 with zig (pip install ziglang).

Every export of Proton's cfgmgr32 (proton11-exports.json, dumped from
files/lib/wine/<arch>/cfgmgr32.dll) is forwarded to cfgmgr32_wine.dll with the same ordinal,
except the functions implemented in cfgmgr32_shim.c.
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "build")
# name -> stdcall argument bytes (i386 decoration)
OWN = {"CM_Get_Parent": 12, "CM_Get_Parent_Ex": 16,
       "CM_Get_DevNode_PropertyW": 24, "CM_Get_DevNode_Property_ExW": 28,
       "CM_Get_DevNode_Registry_PropertyW": 24, "CM_Get_DevNode_Registry_Property_ExW": 28}
TARGETS = {"x86_64-windows": "x86_64-windows-gnu", "i386-windows": "x86-windows-gnu"}


def write_def(arch, exports, path):
    lines = ["LIBRARY cfgmgr32.dll", "EXPORTS"]
    for name, ordinal in sorted(exports, key=lambda e: e[1]):
        if name in OWN:
            target = name if arch.startswith("x86_64") else "%s@%d" % (name, OWN[name])
            lines.append("    %s=%s @%d" % (name, target, ordinal))
        else:
            lines.append("    %s=cfgmgr32_wine.%s @%d" % (name, name, ordinal))
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    with open(os.path.join(HERE, "proton11-exports.json"), encoding="utf-8-sig") as f:
        exports = json.load(f)
    os.makedirs(OUT, exist_ok=True)
    for arch, target in TARGETS.items():
        missing = set(OWN) - {name for name, _ in exports[arch]}
        if missing:
            sys.exit("%s: Proton's cfgmgr32 has no %s" % (arch, ", ".join(sorted(missing))))
        out_dir = os.path.join(OUT, arch)
        os.makedirs(out_dir, exist_ok=True)
        def_path = os.path.join(out_dir, "cfgmgr32.def")
        write_def(arch, exports[arch], def_path)
        cmd = [sys.executable, "-m", "ziglang", "cc", "-target", target, "-shared", "-O2", "-s",
               "-Wno-inconsistent-dllimport",   # cfgmgr32.h declares what we implement as imports
               "-municode", "-o", os.path.join(out_dir, "cfgmgr32.dll"),
               os.path.join(HERE, "cfgmgr32_shim.c"), def_path, "-lsetupapi", "-ladvapi32", "-lkernel32"]
        print(" ".join(cmd))
        subprocess.check_call(cmd)
        for leftover in ("cfgmgr32.lib", "cfgmgr32.pdb"):
            p = os.path.join(out_dir, leftover)
            if os.path.exists(p):
                os.remove(p)


if __name__ == "__main__":
    main()
