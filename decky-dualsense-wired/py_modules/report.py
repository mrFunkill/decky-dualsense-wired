"""
Diagnostic report for bug reports: one zip in the user's Downloads folder with the plugin logs,
system and controller information, settings, drivers, audio devices and kernel messages.

MAC addresses are masked (only the last two bytes stay, enough to tell controllers apart).
Nothing is sent anywhere: the user attaches the file to a GitHub issue.
"""

import glob
import io
import json
import os
import platform
import pwd
import re
import subprocess
import time
import zipfile

MAX_LOG_BYTES = 2 * 1024 * 1024     # per log file: its last 2 MB
LOG_FILES = 3                       # newest plugin logs
KERNEL_PATTERN = re.compile(r"hid|playstation|dualsense|054c|bluetooth|dummy|gadget|udc|uhid", re.I)
MAC_COLON = re.compile(r"\b([0-9A-Fa-f]{2}[:_-]){5}[0-9A-Fa-f]{2}\b")


def _run(cmd, user=None, timeout=10):
    """Output of a system command (stdout and stderr), or the error; never raises."""
    # Decky's bundled Python sets LD_LIBRARY_PATH, which breaks system binaries.
    env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
    if user:
        pw = pwd.getpwnam(user)
        env = {"PATH": "/usr/bin:/bin", "XDG_RUNTIME_DIR": "/run/user/%d" % pw.pw_uid, "HOME": pw.pw_dir}
        if os.geteuid() == 0 and pw.pw_uid != 0:
            cmd = ["/usr/bin/runuser", "-u", user, "--", "/usr/bin/env"] + ["%s=%s" % kv for kv in env.items()] + cmd
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
        return (out.stdout + out.stderr).strip() or "(no output, exit %d)" % out.returncode
    except (OSError, subprocess.SubprocessError) as e:
        return "(%s: %s)" % (cmd[0], e)


def _read(path, limit=None):
    try:
        with open(path, "rb") as f:
            if limit:
                size = os.fstat(f.fileno()).st_size
                if size > limit:
                    f.seek(size - limit)
            return f.read().decode("utf-8", "replace")
    except OSError as e:
        return "(%s)" % e


class Masker:
    """Replaces MAC addresses, also the colon-less form used in gadget names."""

    def __init__(self, macs, user=None):
        # The account name shows in paths (/home/<user>/...); "deck" on a Steam Deck tells nothing.
        self.user = user if user and user not in ("deck", "root") else None
        self.plain = {}
        for mac in macs:
            hexes = re.sub(r"[^0-9a-f]", "", mac.lower())
            if len(hexes) == 12:
                self.plain[hexes] = self._mask(hexes)

    @staticmethod
    def _mask(hexes):
        return "xxxxxxxx" + hexes[8:]

    def __call__(self, text):
        def colon(m):
            hexes = re.sub(r"[^0-9a-fA-F]", "", m.group(0)).lower()
            return "xx:xx:xx:xx:%s:%s" % (hexes[8:10], hexes[10:12])
        text = MAC_COLON.sub(colon, text)
        for hexes, masked in self.plain.items():
            text = re.sub(hexes, masked, text, flags=re.I)
        if self.user:
            text = re.sub(r"\b%s\b" % re.escape(self.user), "<user>", text)
        return text


def _system_info(versions):
    lines = ["Generated: %s" % time.strftime("%Y-%m-%d %H:%M:%S %z")]
    lines += ["%s: %s" % kv for kv in versions.items()]
    lines.append("Kernel: %s" % platform.release())
    lines.append("Model: %s %s" % (_read("/sys/devices/virtual/dmi/id/sys_vendor").strip(),
                                   _read("/sys/devices/virtual/dmi/id/product_name").strip()))
    lines.append("")
    lines.append(_read("/etc/os-release").strip())
    return "\n".join(lines)


def _devices():
    """HID devices from Sony with their drivers, hidraw nodes, virtual USB ports, kernel modules."""
    out = ["== Sony HID devices (bus:vendor:product.n -> driver, hidraw)"]
    for path in sorted(glob.glob("/sys/bus/hid/devices/*:054C:*")):
        driver = os.path.basename(os.path.realpath(path + "/driver")) if os.path.exists(path + "/driver") else "-"
        hidraw = " ".join(os.listdir(path + "/hidraw")) if os.path.isdir(path + "/hidraw") else "-"
        uevent = _read(path + "/uevent")
        name = re.search(r"HID_NAME=(.*)", uevent)
        phys = re.search(r"HID_PHYS=(.*)", uevent)
        out.append("%s -> %s, %s, %s, phys %s" % (os.path.basename(path), driver, hidraw,
                                                    name.group(1) if name else "?", phys.group(1) if phys else "?"))
    out.append("\n== hidraw nodes")
    out.append(_run(["/usr/bin/ls", "-l"] + sorted(glob.glob("/dev/hidraw*"))))
    out.append("\n== power supplies")
    out.append("\n".join(os.path.basename(p) for p in glob.glob("/sys/class/power_supply/*")))
    out.append("\n== UDCs (virtual USB ports) and gadgets")
    out.append(" ".join(os.listdir("/sys/class/udc")) if os.path.isdir("/sys/class/udc") else "(none)")
    out.append(" ".join(os.listdir("/sys/kernel/config/usb_gadget"))
               if os.path.isdir("/sys/kernel/config/usb_gadget") else "(no configfs gadgets)")
    out.append("\n== kernel modules")
    mods = _read("/proc/modules").splitlines()
    out.append("\n".join(m for m in mods if re.match(r"(dummy_hcd|libcomposite|usb_f_|uhid|hid_playstation|"
                                                       r"hid_generic|bluetooth|btusb|hidp)\b", m)))
    out.append("\n== ignore_special_drivers: " + _read("/sys/module/hid/parameters/ignore_special_drivers").strip())
    out.append("\n== udev rule")
    out.append(_read("/etc/udev/rules.d/72-decky-dualsense-wired.rules"))
    return "\n".join(out)


def _audio(user):
    out = ["== sinks", _run(["pactl", "list", "short", "sinks"], user),
           "\n== sources", _run(["pactl", "list", "short", "sources"], user),
           "\n== modules", _run(["pactl", "list", "short", "modules"], user),
           "\n== pipewire nodes (DualSense)",
           "\n".join(line for line in _run(["pw-cli", "ls", "Node"], user).splitlines()
                     if re.search(r"dualsense|decky", line, re.I))]
    return "\n".join(out)


def _bluetooth():
    return "\n".join(["== bluetoothctl devices Connected", _run(["bluetoothctl", "devices", "Connected"]),
                      "\n== bluetooth service", _run(["systemctl", "status", "bluetooth", "--no-pager", "-n", "0"])])


def _kernel():
    text = _run(["dmesg", "--ctime"], timeout=15)
    lines = [line for line in text.splitlines() if KERNEL_PATTERN.search(line)]
    return "\n".join(lines[-800:])


def build(out_dir, owner, log_dir, settings_file, status, versions, user="deck"):
    """
    Writes the report zip into out_dir (owned by `owner`) and returns its path.
    status: the Manager's status() dict (controllers, settings in use, streams).
    """
    macs = [c.get("mac", "") for c in status.get("controllers", [])]
    mask = Masker(macs, user)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = "dualsense-wired-report-%s" % stamp
    parts = {
        "README.txt": "DualSense Wired diagnostic report. Attach this zip to an issue at\n"
                      "https://github.com/mrFunkill/decky-dualsense-wired/issues\n"
                      "and describe what you did and what went wrong (and roughly when).\n"
                      "MAC addresses are masked.\n",
        "system.txt": _system_info(versions),
        "status.json": json.dumps(status, indent=2, ensure_ascii=False),
        "settings.json": _read(settings_file),
        "devices.txt": _devices(),
        "audio.txt": _audio(user),
        "bluetooth.txt": _bluetooth(),
        "kernel.txt": _kernel(),
    }
    logs = sorted(glob.glob(os.path.join(log_dir, "*.log")), key=os.path.getmtime, reverse=True)[:LOG_FILES]
    for path in logs:
        parts["logs/" + os.path.basename(path).replace(" ", "_")] = _read(path, MAX_LOG_BYTES)

    created = not os.path.isdir(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name + ".zip")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, text in parts.items():
            z.writestr("%s/%s" % (name, arc), mask(text))
    with open(path, "wb") as f:
        f.write(buf.getvalue())
    try:
        pw = pwd.getpwnam(owner)
        os.chown(path, pw.pw_uid, pw.pw_gid)
        if created:
            os.chown(out_dir, pw.pw_uid, pw.pw_gid)
    except (KeyError, OSError):
        pass
    return path
