"""
Installs the DualSense Wired helper service (bin/dualsense_wired_helper.exe, source in
native/helper) into Proton prefixes, so games under Proton can pair a controller with its
audio device: the service creates the SWD\\MMDEVAPI device nodes Wine lacks.

Prefixes are only touched while no Wine process uses them: the registry files are plain text
that wineserver loads at start and rewrites on exit. Edits keep the file's owner and mode and
replace it atomically. Everything added is removed again by uninstall().
"""

import glob
import hashlib
import os
import re
import shutil
import time

SERVICE_NAME = "DualSenseWiredHelper"
HELPER_EXE = "dualsense_wired_helper.exe"
HELPER_SOURCE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", HELPER_EXE)

SERVICE_KEY = "System\\\\ControlSet001\\\\Services\\\\" + SERVICE_NAME
SWD_KEY_PREFIX = "System\\\\ControlSet001\\\\Enum\\\\SWD\\\\MMDEVAPI\\\\"
# Values written by the helper into each SWD node it creates.
HELPER_MARK = '"DualSenseWired"="1"'


def _service_block(now):
    return "\n".join([
        "[%s] %d" % (SERVICE_KEY, now),
        '"DisplayName"="DualSense Wired helper"',
        '"ErrorControl"=dword:00000001',
        '"ImagePath"="C:\\\\windows\\\\system32\\\\%s"' % HELPER_EXE,
        '"ObjectName"="LocalSystem"',
        '"Start"=dword:00000002',
        '"Type"=dword:00000010',
        "",
    ]) + "\n"


def steam_libraries(steam_root):
    """steamapps directories of all Steam library folders."""
    libraries = [os.path.join(steam_root, "steamapps")]
    try:
        with open(os.path.join(steam_root, "steamapps", "libraryfolders.vdf"), encoding="utf-8") as f:
            for path in re.findall(r'"path"\s+"([^"]+)"', f.read()):
                libraries.append(os.path.join(path.replace("\\\\", "\\"), "steamapps"))
    except OSError:
        pass
    seen, result = set(), []
    for lib in libraries:
        real = os.path.realpath(lib)
        if real not in seen and os.path.isdir(real):
            seen.add(real)
            result.append(real)
    return result


def find_prefixes(steam_root):
    prefixes = []
    for lib in steam_libraries(steam_root):
        for pfx in glob.glob(os.path.join(lib, "compatdata", "*", "pfx")):
            if os.path.isfile(os.path.join(pfx, "system.reg")):
                prefixes.append(pfx)
    return prefixes


def running_prefixes():
    """Prefixes that some process (wine, wineserver, a game) currently has in WINEPREFIX."""
    running = set()
    for environ in glob.glob("/proc/[0-9]*/environ"):
        try:
            with open(environ, "rb") as f:
                data = f.read()
        except OSError:
            continue
        for var in data.split(b"\0"):
            if var.startswith(b"WINEPREFIX="):
                running.add(os.path.realpath(var[11:].decode(errors="replace").rstrip("/")))
    return running


def _rewrite(path, transform):
    """Apply transform(text) -> text to a registry file, atomically, keeping owner and mode."""
    with open(path, encoding="utf-8", errors="surrogateescape") as f:
        text = f.read()
    new = transform(text)
    if new == text:
        return False
    st = os.stat(path)
    tmp = path + ".dualsense-wired.tmp"
    with open(tmp, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(new)
    os.chown(tmp, st.st_uid, st.st_gid)
    os.chmod(tmp, st.st_mode & 0o7777)
    os.replace(tmp, path)
    return True


def _remove_blocks(text, keep):
    """Drop registry key blocks ("[key] ...\\n values...") for which keep(header, body) is False."""
    parts = re.split(r"(?m)^(?=\[)", text)
    return "".join(p for p in parts if not p.startswith("[") or keep(p.split("\n", 1)[0], p))


def _file_digest(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def install(pfx):
    """Install or update the helper in one (not running) prefix. True if anything changed."""
    changed = False
    system32 = os.path.join(pfx, "drive_c", "windows", "system32")
    if not os.path.isdir(system32):
        return False
    target = os.path.join(system32, HELPER_EXE)
    if _file_digest(target) != _file_digest(HELPER_SOURCE):
        st = os.stat(system32)
        tmp = target + ".tmp"
        shutil.copyfile(HELPER_SOURCE, tmp)
        os.chown(tmp, st.st_uid, st.st_gid)
        os.chmod(tmp, 0o755)
        os.replace(tmp, target)
        changed = True

    header = "[%s]" % SERVICE_KEY

    def add_service(text):
        if header in text:
            return text
        if not text.endswith("\n"):
            text += "\n"
        return text + "\n" + _service_block(int(time.time()))

    changed |= _rewrite(os.path.join(pfx, "system.reg"), add_service)
    return changed


def uninstall(pfx):
    """Remove the helper, its service and the SWD nodes it created from one prefix."""
    try:
        os.remove(os.path.join(pfx, "drive_c", "windows", "system32", HELPER_EXE))
    except OSError:
        pass

    def keep(header, body):
        if header.startswith("[%s]" % SERVICE_KEY):
            return False
        if header.startswith("[" + SWD_KEY_PREFIX) and HELPER_MARK in body:
            return False
        return True

    system_reg = os.path.join(pfx, "system.reg")
    if os.path.isfile(system_reg):
        # install() added a blank separator line before the service block.
        _rewrite(system_reg, lambda text: _remove_blocks(text, keep).rstrip("\n") + "\n")


class PrefixSync:
    """Keeps the helper installed in every prefix that is not in use (call sync() periodically)."""

    def __init__(self, logger, steam_root):
        self.log = logger
        self.steam_root = steam_root
        self._done = {}   # prefix -> helper digest installed

    def sync(self):
        if not os.path.isfile(HELPER_SOURCE):
            return
        digest = _file_digest(HELPER_SOURCE)
        busy = None
        for pfx in find_prefixes(self.steam_root):
            if self._done.get(pfx) == digest:
                continue
            if busy is None:
                busy = running_prefixes()
            if os.path.realpath(pfx) in busy:
                continue
            try:
                if install(pfx):
                    self.log.info("Installed the controller audio helper into %s", pfx)
                self._done[pfx] = digest
            except OSError as e:
                self.log.warning("Installing the helper into %s failed: %s", pfx, e)

    def uninstall_all(self):
        busy = running_prefixes()
        for pfx in find_prefixes(self.steam_root):
            if os.path.realpath(pfx) in busy:
                self.log.warning("%s is in use, helper left installed", pfx)
                continue
            try:
                uninstall(pfx)
                self.log.info("Removed the helper from %s", pfx)
            except OSError as e:
                self.log.warning("Removing the helper from %s failed: %s", pfx, e)


def uninstall_all_detached(steam_root, logger):
    """
    Plugin uninstall: Decky kills the plugin process a few seconds after stopping it and
    deletes the plugin directory, too soon to rewrite the registry of every prefix. Run the
    cleanup from a copy of this module in a separate process instead.
    """
    import subprocess
    script = "/tmp/decky-dualsense-wired-uninstall.py"
    shutil.copyfile(os.path.abspath(__file__), script)
    log = open("/tmp/decky-dualsense-wired-uninstall.log", "w")
    # Decky's bundled libraries (LD_LIBRARY_PATH) must not leak into the system python.
    env = {k: v for k, v in os.environ.items() if k not in ("LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME")}
    subprocess.Popen(["/usr/bin/python3", "-I", script, steam_root], stdout=log, stderr=log, env=env,
                     stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    logger.info("Removing the helper from Proton prefixes in the background (log: %s)", log.name)


if __name__ == "__main__":
    import logging
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    PrefixSync(logging.getLogger("uninstall"), sys.argv[1]).uninstall_all()
    if os.path.abspath(__file__).startswith("/tmp/"):
        os.remove(os.path.abspath(__file__))
