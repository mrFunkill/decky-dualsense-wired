"""
Make a Bluetooth DualSense look like a wired (USB) one.

For every DualSense connected over Bluetooth a virtual HID device is created
through /dev/uhid with bus type USB and the real USB report descriptor.
Reports are translated in both directions:

  BT input 0x31 (78 bytes, CRC)   ->  USB input 0x01 (64 bytes)
  USB output 0x02 (48+ bytes)     ->  BT output 0x31 (78 bytes, CRC)
  feature reports                 ->  forwarded to the BT hidraw node

The original Bluetooth hidraw/evdev nodes are hidden (chmod 0600) so Steam and
games only see the virtual "wired" controller.

Report layouts follow drivers/hid/hid-playstation.c (Linux kernel).
"""

import errno
import glob
import os
import select
import signal
import struct
import subprocess
import threading
import time
import zlib

try:
    import fcntl

    def _ioctl(fd, request, buf):
        return fcntl.ioctl(fd, request, buf, True)
except ImportError:  # Decky's bundled Python may lack fcntl
    import ctypes

    def _ioctl(fd, request, buf):
        libc = ctypes.CDLL(None, use_errno=True)
        cbuf = (ctypes.c_char * len(buf)).from_buffer(buf)
        res = libc.ioctl(fd, ctypes.c_ulong(request), cbuf)
        if res < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        return res


SONY_VID = 0x054C
DUALSENSE_PIDS = {
    0x0CE6: "DualSense Wireless Controller",
    0x0DF2: "DualSense Edge Wireless Controller",
}
BUS_USB = 0x03
BUS_BLUETOOTH = 0x05

# linux/uhid.h
UHID_DESTROY = 1
UHID_OUTPUT = 6
UHID_GET_REPORT = 9
UHID_GET_REPORT_REPLY = 10
UHID_CREATE2 = 11
UHID_INPUT2 = 12
UHID_SET_REPORT = 13
UHID_SET_REPORT_REPLY = 14
UHID_FEATURE_REPORT = 0
UHID_OUTPUT_REPORT = 1
UHID_DATA_MAX = 4096
UHID_EVENT_SIZE = 4376  # sizeof(struct uhid_event)


def _hid_ioc(nr, size):
    # _IOC(_IOC_READ | _IOC_WRITE, 'H', nr, size)
    return (3 << 30) | (size << 16) | (ord("H") << 8) | nr


def HIDIOCSFEATURE(size):
    return _hid_ioc(0x06, size)


def HIDIOCGFEATURE(size):
    return _hid_ioc(0x07, size)


DS_INPUT_REPORT_USB = 0x01
DS_INPUT_REPORT_USB_SIZE = 64
DS_INPUT_REPORT_BT = 0x31
DS_INPUT_REPORT_BT_SIZE = 78
DS_OUTPUT_REPORT_USB = 0x02
DS_OUTPUT_REPORT_BT = 0x31
DS_OUTPUT_REPORT_BT_SIZE = 78
DS_OUTPUT_TAG = 0x10
DS_OUTPUT_COMMON_SIZE = 47
DS_FEATURE_REPORT_CALIBRATION = 0x05
DS_FEATURE_REPORT_PAIRING_INFO = 0x09

PS_OUTPUT_CRC32_SEED = 0xA2
# Feature reports sent to the controller are checked with the HID SET_REPORT|feature
# header byte (0x53), not 0xA3 (which is for feature reports read from it).
# Source: research/FINDINGS.md, capture 02-dsx-start.
PS_FEATURE_SET_CRC32_SEED = 0x53

# Feature report sizes (including report ID) from the USB report descriptor.
FEATURE_REPORT_SIZES = {
    0x05: 41, 0x08: 48, 0x09: 20, 0x0A: 27, 0x20: 64, 0x21: 5, 0x22: 64,
    0x80: 64, 0x81: 64, 0x82: 10, 0x83: 64, 0x84: 64, 0x85: 3, 0xA0: 2,
    0xE0: 64, 0xF0: 64, 0xF1: 64, 0xF2: 16, 0xF4: 64, 0xF5: 4,
}

# Feature reports that exist in the Bluetooth report descriptor. Requests for the
# USB-only ones (0x0A, 0x21, 0x84, 0x85, 0xA0, 0xE0, 0xF4, 0xF5) are never answered
# over Bluetooth: BlueZ times out ("HIDP GET_REPORT request timed out") and the
# controller disconnects, so they are refused locally instead.
BT_FEATURE_REPORTS = {0x05, 0x08, 0x09, 0x20, 0x22, 0x80, 0x81, 0x82, 0x83, 0xF0, 0xF1, 0xF2}

# SET_REPORT feature ids that must not reach the Bluetooth controller: Spider-Man sends
# SET 0x08 at start-up and the controller drops the Bluetooth link 9 ms later.
# They are acknowledged locally and their payload is logged.
SET_FEATURE_BLOCKED = {0x08}

# USB report descriptor of the DualSense,
# from https://github.com/nondebug/dualsense/blob/main/report-descriptor-usb.txt
DS_USB_RDESC = bytes([
    0x05, 0x01, 0x09, 0x05, 0xA1, 0x01, 0x85, 0x01, 0x09, 0x30, 0x09, 0x31,
    0x09, 0x32, 0x09, 0x35, 0x09, 0x33, 0x09, 0x34, 0x15, 0x00, 0x26, 0xFF,
    0x00, 0x75, 0x08, 0x95, 0x06, 0x81, 0x02, 0x06, 0x00, 0xFF, 0x09, 0x20,
    0x95, 0x01, 0x81, 0x02, 0x05, 0x01, 0x09, 0x39, 0x15, 0x00, 0x25, 0x07,
    0x35, 0x00, 0x46, 0x3B, 0x01, 0x65, 0x14, 0x75, 0x04, 0x95, 0x01, 0x81,
    0x42, 0x65, 0x00, 0x05, 0x09, 0x19, 0x01, 0x29, 0x0F, 0x15, 0x00, 0x25,
    0x01, 0x75, 0x01, 0x95, 0x0F, 0x81, 0x02, 0x06, 0x00, 0xFF, 0x09, 0x21,
    0x95, 0x0D, 0x81, 0x02, 0x06, 0x00, 0xFF, 0x09, 0x22, 0x15, 0x00, 0x26,
    0xFF, 0x00, 0x75, 0x08, 0x95, 0x34, 0x81, 0x02, 0x85, 0x02, 0x09, 0x23,
    0x95, 0x2F, 0x91, 0x02, 0x85, 0x05, 0x09, 0x33, 0x95, 0x28, 0xB1, 0x02,
    0x85, 0x08, 0x09, 0x34, 0x95, 0x2F, 0xB1, 0x02, 0x85, 0x09, 0x09, 0x24,
    0x95, 0x13, 0xB1, 0x02, 0x85, 0x0A, 0x09, 0x25, 0x95, 0x1A, 0xB1, 0x02,
    0x85, 0x20, 0x09, 0x26, 0x95, 0x3F, 0xB1, 0x02, 0x85, 0x21, 0x09, 0x27,
    0x95, 0x04, 0xB1, 0x02, 0x85, 0x22, 0x09, 0x40, 0x95, 0x3F, 0xB1, 0x02,
    0x85, 0x80, 0x09, 0x28, 0x95, 0x3F, 0xB1, 0x02, 0x85, 0x81, 0x09, 0x29,
    0x95, 0x3F, 0xB1, 0x02, 0x85, 0x82, 0x09, 0x2A, 0x95, 0x09, 0xB1, 0x02,
    0x85, 0x83, 0x09, 0x2B, 0x95, 0x3F, 0xB1, 0x02, 0x85, 0x84, 0x09, 0x2C,
    0x95, 0x3F, 0xB1, 0x02, 0x85, 0x85, 0x09, 0x2D, 0x95, 0x02, 0xB1, 0x02,
    0x85, 0xA0, 0x09, 0x2E, 0x95, 0x01, 0xB1, 0x02, 0x85, 0xE0, 0x09, 0x2F,
    0x95, 0x3F, 0xB1, 0x02, 0x85, 0xF0, 0x09, 0x30, 0x95, 0x3F, 0xB1, 0x02,
    0x85, 0xF1, 0x09, 0x31, 0x95, 0x3F, 0xB1, 0x02, 0x85, 0xF2, 0x09, 0x32,
    0x95, 0x0F, 0xB1, 0x02, 0x85, 0xF4, 0x09, 0x35, 0x95, 0x3F, 0xB1, 0x02,
    0x85, 0xF5, 0x09, 0x36, 0x95, 0x03, 0xB1, 0x02, 0xC0,
])


# --- report conversion (pure functions) -------------------------------------

def _crc32(seed, data):
    return zlib.crc32(bytes([seed]) + bytes(data)) & 0xFFFFFFFF


def bt_input_to_usb(report):
    """BT input report 0x31 -> USB input report 0x01, or None if not convertible."""
    if len(report) < 65 or report[0] != DS_INPUT_REPORT_BT:
        return None
    # BT: [0x31, seq/tag, 63 bytes common data, ..., crc32]; USB: [0x01, common data]
    return bytes([DS_INPUT_REPORT_USB]) + bytes(report[2:65])


def usb_output_to_bt(report, seq):
    """USB output report 0x02 -> BT output report 0x31, or None if not convertible."""
    if len(report) < 2 or report[0] != DS_OUTPUT_REPORT_USB:
        return None
    common = bytes(report[1:1 + DS_OUTPUT_COMMON_SIZE]).ljust(DS_OUTPUT_COMMON_SIZE, b"\0")
    out = bytearray(DS_OUTPUT_REPORT_BT_SIZE)
    out[0] = DS_OUTPUT_REPORT_BT
    out[1] = (seq & 0x0F) << 4
    out[2] = DS_OUTPUT_TAG
    out[3:3 + DS_OUTPUT_COMMON_SIZE] = common
    struct.pack_into("<I", out, 74, _crc32(PS_OUTPUT_CRC32_SEED, out[:74]))
    return bytes(out)


# Game output sent on its own (no audio stream) is merged and paced to at most 125 reports/s:
# BlueZ drops the HID device when its socket buffer to the controller overflows.
MIN_OUTPUT_INTERVAL = 0.008


class OutputState:
    """
    Merges the "common" part of output reports (47 bytes, hid-playstation's
    dualsense_output_report_common) the way the controller applies them: each field group
    only when its valid flag is set. Lets several game reports be sent as one, e.g. inside
    an audio stream report, without losing a trigger or LED change from an earlier one.
    """

    # (flag byte index in the report, flag bits, byte offsets of the field group)
    GROUPS = (
        (0, 0x03, range(2, 4)),     # rumble motors (compatible vibration / haptics select)
        (0, 0x04, range(10, 21)),   # right trigger effect
        (0, 0x08, range(21, 32)),   # left trigger effect
        (0, 0x10, (4,)),            # headphone volume
        (0, 0x20, (5,)),            # speaker volume
        (0, 0x40, (6,)),            # microphone volume
        (0, 0x80, (7,)),            # audio control
        (1, 0x01, (8,)),            # mute button LED
        (1, 0x02, (9,)),            # power save control
        (1, 0x04, (44, 45, 46)),    # lightbar colour
        (1, 0x10, (43,)),           # player LEDs
        (38, 0x02, (41,)),          # lightbar setup
        (38, 0x01, (42,)),          # LED brightness
    )
    FLAG_BYTES = (0, 1, 38)

    def __init__(self):
        self.state = bytearray(DS_OUTPUT_COMMON_SIZE)
        self.pending = {i: 0 for i in self.FLAG_BYTES}
        self.ever = {i: 0 for i in self.FLAG_BYTES}    # every flag seen so far
        self.lock = threading.Lock()

    def update(self, common):
        common = bytes(common[:DS_OUTPUT_COMMON_SIZE]).ljust(DS_OUTPUT_COMMON_SIZE, b"\0")
        grouped = set()
        with self.lock:
            for flag_index, bits, offsets in self.GROUPS:
                grouped.update(offsets)
                if common[flag_index] & bits:
                    for o in offsets:
                        self.state[o] = common[o]
            for o in range(DS_OUTPUT_COMMON_SIZE):
                if o not in grouped and o not in self.FLAG_BYTES:
                    self.state[o] = common[o]   # fields without a known flag: latest wins
            for i in self.FLAG_BYTES:
                self.pending[i] |= common[i]
                self.ever[i] |= common[i]

    def snapshot(self):
        """Everything set so far (all flags ever seen), e.g. for a controller that reconnected."""
        with self.lock:
            if not any(self.ever.values()):
                return None
            out = bytearray(self.state)
            for i in self.FLAG_BYTES:
                out[i] = self.ever[i]
            return bytes(out)

    def has_pending(self):
        return any(self.pending.values())

    def take(self):
        """Merged state with the flags accumulated since the last take(), or None."""
        with self.lock:
            if not self.has_pending():
                return None
            out = bytearray(self.state)
            for i in self.FLAG_BYTES:
                out[i] = self.pending[i]
                self.pending[i] = 0
            return bytes(out)


def common_to_bt(common, seq):
    """47-byte common output part -> BT output report 0x31."""
    return usb_output_to_bt(bytes([DS_OUTPUT_REPORT_USB]) + bytes(common), seq)


def usb_feature_to_bt(report):
    """Feature reports sent over BT carry a CRC32 in their last 4 bytes."""
    out = bytearray(report)
    if len(out) > 4:
        struct.pack_into("<I", out, len(out) - 4, _crc32(PS_FEATURE_SET_CRC32_SEED, out[:-4]))
    return bytes(out)


def patch_pairing_info(report):
    """
    hid-playstation refuses a second device with the same MAC address
    (the real BT controller is still registered), so the virtual device
    reports the MAC with the "locally administered" bit flipped.
    MAC is stored reversed in bytes 1..6, so byte 6 is the first octet.
    """
    out = bytearray(report)
    if len(out) >= 7:
        out[6] ^= 0x02
    return bytes(out)


# --- sysfs helpers -------------------------------------------------------------

def _read_uevent(path):
    info = {}
    try:
        with open(path) as f:
            for line in f:
                key, _, value = line.strip().partition("=")
                info[key] = value
    except OSError:
        pass
    return info


def _parse_hid_id(hid_id):
    # "0005:0000054C:00000CE6"
    try:
        bus, vid, pid = (int(x, 16) for x in hid_id.split(":"))
        return bus, vid, pid
    except ValueError:
        return None


VIRTUAL_PHYS_PREFIX = "usb-decky-dualsense-wired"


def find_dualsenses():
    """
    DualSense controllers to bridge: every Bluetooth one, and every real USB one
    (wired controllers are wrapped too, so commands that make the controller
    switch off under Proton can be filtered). Our own virtual devices are skipped.
    """
    found = []
    for sys_path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        info = _read_uevent(os.path.join(sys_path, "device", "uevent"))
        ids = _parse_hid_id(info.get("HID_ID", ""))
        if not ids:
            continue
        bus, vid, pid = ids
        if vid != SONY_VID or pid not in DUALSENSE_PIDS or bus not in (BUS_BLUETOOTH, BUS_USB):
            continue
        if info.get("HID_PHYS", "").startswith(VIRTUAL_PHYS_PREFIX):
            continue
        if "/dummy_hcd" in os.path.realpath(sys_path):
            continue  # our USB-gadget controller on the virtual USB bus
        name = os.path.basename(sys_path)
        found.append({
            "hidraw": name,
            "sys_path": sys_path,
            "pid": pid,
            "bus": bus,
            "name": info.get("HID_NAME", DUALSENSE_PIDS[pid]),
            "mac": info.get("HID_UNIQ", ""),
            "hid_device": os.path.basename(os.path.realpath(os.path.join(sys_path, "device"))),
        })
    return found


def find_hidraw_by_phys(phys):
    for sys_path in glob.glob("/sys/class/hidraw/hidraw*"):
        info = _read_uevent(os.path.join(sys_path, "device", "uevent"))
        if info.get("HID_PHYS") == phys:
            return os.path.basename(sys_path)
    return None


def _device_sys_nodes(sys_path):
    """(sysfs path, /dev path) of the hidraw, evdev and js nodes of a HID device."""
    nodes = [(sys_path, "/dev/" + os.path.basename(sys_path))]
    for input_dir in glob.glob(os.path.join(sys_path, "device", "input", "input*")):
        for child in os.listdir(input_dir):
            if child.startswith("event") or child.startswith("js"):
                nodes.append((os.path.join(input_dir, child), "/dev/input/" + child))
    return nodes


def device_nodes_of(sys_path):
    """/dev nodes (hidraw, evdev, js) belonging to a HID device."""
    return [dev for _, dev in _device_sys_nodes(sys_path)]


def node_holders(nodes):
    """
    Non-root processes (Steam, gamescope, games) that have any of the given /dev
    nodes open, as 'pid (comm)'. Root daemons such as systemd-logind keep evdev
    fds open for session management and are not consumers.
    """
    wanted = set(nodes)
    own = os.getpid()
    holders = []
    for pid_dir in glob.glob("/proc/[0-9]*"):
        pid = int(os.path.basename(pid_dir))
        if pid == own:
            continue
        try:
            if os.stat(pid_dir).st_uid == 0:
                continue
        except OSError:
            continue
        try:
            fds = os.listdir(os.path.join(pid_dir, "fd"))
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(pid_dir, "fd", fd))
            except OSError:
                continue
            if target in wanted:
                try:
                    with open(os.path.join(pid_dir, "comm")) as f:
                        comm = f.read().strip()
                except OSError:
                    comm = "?"
                # gamescope gets every input device from logind (bypassing
                # permissions) but does not pass gamepad input to games.
                if not comm.startswith("gamescope"):
                    holders.append("%d (%s)" % (pid, comm))
                break
    return holders


# --- hiding the Bluetooth device from Steam -------------------------------------
#
# chmod on an already opened node does not revoke the open file, and Steam opens a
# freshly connected controller immediately. So the nodes are hidden by a udev rule
# (applied before Steam is notified), and a controller Steam already holds is
# re-bound to its driver so its nodes are re-created under that rule.

# Must sort after 60-steam-input/70-uaccess (which add the uaccess tag) and before
# 73-seat-late (which turns the tag into ACLs for the logged-in user).
UDEV_RULE_PATH = "/etc/udev/rules.d/72-decky-dualsense-wired.rules"
OLD_UDEV_RULE_PATHS = ["/etc/udev/rules.d/99-decky-dualsense-wired.rules"]
UDEV_RULE = """\
# Installed by the Decky plugin "DualSense Wired" and removed when it is disabled.
# Hides Bluetooth DualSense nodes from Steam and games; the plugin exposes the
# controller as a virtual wired (USB) DualSense instead.
KERNEL=="hidraw*", KERNELS=="0005:054C:0CE6.*|0005:054C:0DF2.*", MODE="0600", TAG-="uaccess"
SUBSYSTEM=="input", KERNEL=="event*|js*", KERNELS=="0005:054C:0CE6.*|0005:054C:0DF2.*", MODE="0600", TAG-="uaccess"
# Real wired controllers too (the audio card is a separate device and stays visible);
# the plugin's own virtual controllers live under /devices/virtual/misc/uhid.
KERNEL=="hidraw*", KERNELS=="0003:054C:0CE6.*|0003:054C:0DF2.*", DEVPATH!="*/uhid/*", DEVPATH!="*/dummy_hcd*", MODE="0600", TAG-="uaccess"
SUBSYSTEM=="input", KERNEL=="event*|js*", KERNELS=="0003:054C:0CE6.*|0003:054C:0DF2.*", DEVPATH!="*/uhid/*", DEVPATH!="*/dummy_hcd*", MODE="0600", TAG-="uaccess"
# The touchpad's evdev node would be picked up by gamescope/libinput as a mouse;
# Steam and games read the touchpad through hidraw, so libinput ignores it.
# (Matched by name only: KERNELS/ATTRS keys of one rule must match the same parent.)
SUBSYSTEM=="input", KERNEL=="event*", ATTRS{name}=="*DualSense*Touchpad", ENV{LIBINPUT_IGNORE_DEVICE}="1"
"""


def _udevadm(*args):
    # Decky's bundled Python sets LD_LIBRARY_PATH, which breaks system binaries.
    env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
    subprocess.run(["/usr/bin/udevadm", *args], env=env, check=False, timeout=15,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def install_hide_rule():
    for old in OLD_UDEV_RULE_PATHS:
        if os.path.exists(old):
            os.remove(old)
    try:
        with open(UDEV_RULE_PATH) as f:
            if f.read() == UDEV_RULE:
                return
    except OSError:
        pass
    os.makedirs(os.path.dirname(UDEV_RULE_PATH), exist_ok=True)
    with open(UDEV_RULE_PATH, "w") as f:
        f.write(UDEV_RULE)
    _reload_rules()


def _reload_rules():
    """
    Make udev use the current rule files before anything else happens: the reload is
    asynchronous, and nodes created right after it would get the old permissions.
    """
    _udevadm("control", "--reload")
    _udevadm("settle", "--timeout=5")
    time.sleep(0.2)


def remove_hide_rule():
    removed = False
    for path in [UDEV_RULE_PATH] + OLD_UDEV_RULE_PATHS:
        if os.path.exists(path):
            os.remove(path)
            removed = True
    if removed:
        _reload_rules()


PLAYSTATION_DRIVER = "/sys/bus/hid/drivers/playstation"
PID_FILE = "/run/decky-dualsense-wired.pid"
# While this file exists the manager keeps controllers hidden but runs no bridges
# (used by experiments that expose the controller another way).
PAUSE_FILE = "/run/decky-dualsense-wired.pause"


def pause_requested():
    """True while PAUSE_FILE exists and the process that wrote its PID is still alive."""
    try:
        pid = int(open(PAUSE_FILE).read().strip() or 0)
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True
    if pid and not os.path.exists("/proc/%d" % pid):
        try:
            os.remove(PAUSE_FILE)
        except OSError:
            pass
        return False
    return True


def rebind_driver(hid_device):
    """Unbind and bind hid-playstation, so the device nodes are re-created."""
    if os.path.exists(os.path.join(PLAYSTATION_DRIVER, hid_device)):
        with open(os.path.join(PLAYSTATION_DRIVER, "unbind"), "w") as f:
            f.write(hid_device)
    with open(os.path.join(PLAYSTATION_DRIVER, "bind"), "w") as f:
        f.write(hid_device)


def return_to_steam(log):
    """
    Give the controllers back to Steam (plugin disabled or unloaded) so it takes them through
    HIDAPI, like on a normal connection. Steam's HIDAPI only looks at a hidraw node when it is
    added, while its evdev path also reacts to permission changes; so the nodes must be
    re-created after the hide rule is gone, and accessible from the start (otherwise Steam uses
    the evdev joystick: no gyro, touchpad or LEDs). The Bluetooth link stays up.
    """
    remove_hide_rule()      # waits until udev applies it
    for c in find_dualsenses():
        try:
            rebind_driver(c["hid_device"])
        except OSError:
            log.exception("Failed to rebind %s", c["hid_device"])
            reapply_udev_rules(c["sys_path"])


def read_battery(mac):
    """
    Battery of a controller, from the power supply hid-playstation creates for it:
    {"capacity": percent, "status": "Discharging" | "Charging" | "Full" | ...}, or None.
    Steam shows the battery only for wireless controllers, i.e. not for the virtual wired one.
    """
    if not mac:
        return None
    path = "/sys/class/power_supply/ps-controller-battery-" + mac.lower()
    try:
        with open(path + "/capacity") as f:
            capacity = int(f.read().strip())
        with open(path + "/status") as f:
            status = f.read().strip()
    except (OSError, ValueError):
        return None
    return {"capacity": capacity, "status": status}


# Low battery notifications: (threshold %, re-arm when back above threshold + margin)
LOW_BATTERY_THRESHOLDS = (20, 10)
LOW_BATTERY_REARM = 5


def reapply_udev_rules(sys_path):
    """Re-run udev rules on the device nodes (restores Steam's access after the rule is removed)."""
    for node_sys, _ in _device_sys_nodes(sys_path):
        _udevadm("trigger", "--action=change", node_sys)


# --- bridge --------------------------------------------------------------------

class Bridge(threading.Thread):
    """Translates between one BT DualSense and its virtual USB twin."""

    mode = "uhid"

    def __init__(self, controller, logger, hide_original=True):
        super().__init__(daemon=True, name="ds-bridge-" + controller["hidraw"])
        self.controller = controller
        self.log = logger
        self.hide_original = hide_original
        self.state = "starting"
        self.error = None
        self.virtual_hidraw = None
        # Source connection: Bluetooth (reports are translated) or a real USB controller
        # (reports pass through; the bridge only filters harmful feature requests).
        self.is_bt = controller.get("bus", BUS_BLUETOOTH) == BUS_BLUETOOTH
        self.phys = "%s-%s/input3" % (VIRTUAL_PHYS_PREFIX, controller["mac"] or controller["hidraw"])
        self._stop_event = threading.Event()
        self._bt_fd = -1
        self._uhid_fd = -1
        self._out_seq = 0
        self._hidden_modes = {}
        self._last_mode_switch = 0.0
        self._seen_features = set()
        self._pairing_patched = False
        self._last_triggers = None
        self._outputs = 0
        # See GadgetBridge: game output goes into the audio stream while one runs.
        self.output_sink = None
        self._direct = OutputState()
        self._last_direct = 0.0

    def stop(self):
        self._stop_event.set()

    def _flush_direct(self):
        now = time.monotonic()
        if now - self._last_direct < MIN_OUTPUT_INTERVAL:
            return
        common = self._direct.take()
        if common:
            os.write(self._bt_fd, common_to_bt(common, self._out_seq))
            self._out_seq = (self._out_seq + 1) & 0x0F
            self._last_direct = now

    def status(self):
        return {
            "hidraw": self.controller["hidraw"],
            "name": self.controller["name"],
            "mac": self.controller["mac"],
            "connection": "bluetooth" if self.is_bt else "usb",
            "mode": self.mode,
            "state": self.state,
            "error": self.error,
            "virtual_hidraw": self.virtual_hidraw,
        }

    # -- lifecycle --

    def run(self):
        self._step = "start"
        try:
            self._open()
            self.state = "active"
            self.log.info("Bridging %s (%s, %s) as wired controller", self.controller["hidraw"],
                          self.controller["mac"], "bluetooth" if self.is_bt else "usb")
            self._step = "bridge loop"
            self._loop()
            self.state = "stopped"
        except Exception as e:  # noqa: BLE001 - report any failure to the UI
            self.state = "error"
            self.error = "%s: %s" % (self._step, e)
            self.log.exception("Bridge for %s failed at step '%s'", self.controller["hidraw"], self._step)
        finally:
            self._close()

    def _open(self):
        bt_node = "/dev/" + self.controller["hidraw"]
        self._step = "open " + bt_node
        self._bt_fd = os.open(bt_node, os.O_RDWR | os.O_CLOEXEC)
        if self.is_bt:
            # Reading calibration switches the controller from the simple 0x01 report
            # to the full 0x31 report (hid-playstation already does it, but be sure).
            self._step = "read calibration from " + bt_node
            self._get_bt_feature(DS_FEATURE_REPORT_CALIBRATION)
        self._step = "open /dev/uhid"
        self._uhid_fd = os.open("/dev/uhid", os.O_RDWR | os.O_CLOEXEC)
        self._step = "create virtual device"
        self._uhid_create()
        if self.hide_original:
            self._step = "hide original device"
            self._hide_original()

    def _close(self):
        if self._uhid_fd >= 0:
            try:
                os.write(self._uhid_fd, struct.pack("<I", UHID_DESTROY).ljust(UHID_EVENT_SIZE, b"\0"))
            except OSError:
                pass
            os.close(self._uhid_fd)
            self._uhid_fd = -1
        if self._bt_fd >= 0:
            os.close(self._bt_fd)
            self._bt_fd = -1
        self._restore_original()
        self.virtual_hidraw = None

    def _hide_original(self):
        for node in device_nodes_of(self.controller["sys_path"]):
            try:
                self._hidden_modes[node] = os.stat(node).st_mode & 0o7777
                # chmod also narrows the ACL mask, which revokes uaccess ACLs.
                os.chmod(node, 0o600)
            except OSError:
                pass

    def _restore_original(self):
        for node, mode in self._hidden_modes.items():
            try:
                os.chmod(node, mode)
            except OSError:
                pass
        self._hidden_modes.clear()

    def _expose_virtual(self):
        """Make the virtual hidraw accessible to Steam/games (user 'deck')."""
        if not self.virtual_hidraw:
            self.virtual_hidraw = find_hidraw_by_phys(self.phys)
        if self.virtual_hidraw:
            node = "/dev/" + self.virtual_hidraw
            try:
                if os.stat(node).st_mode & 0o777 != 0o666:
                    os.chmod(node, 0o666)
            except OSError:
                pass

    # -- uhid --

    def _uhid_write(self, data):
        os.write(self._uhid_fd, data.ljust(UHID_EVENT_SIZE, b"\0"))

    def _uhid_create(self):
        name = ("Sony Interactive Entertainment " + DUALSENSE_PIDS[self.controller["pid"]]).encode()
        event = struct.pack(
            "<I128s64s64sHHIIII",
            UHID_CREATE2,
            name[:127],
            self.phys.encode()[:63],
            b"",
            len(DS_USB_RDESC),
            BUS_USB,
            SONY_VID,
            self.controller["pid"],
            0x0100,
            0,
        ) + DS_USB_RDESC
        self._uhid_write(event)

    def _uhid_input(self, report):
        self._uhid_write(struct.pack("<IH", UHID_INPUT2, len(report)) + report)

    def _handle_uhid_event(self, ev):
        (ev_type,) = struct.unpack_from("<I", ev, 0)
        if ev_type == UHID_OUTPUT:
            size, rtype = struct.unpack_from("<HB", ev, 4 + UHID_DATA_MAX)
            data = ev[4:4 + size]
            if rtype == UHID_OUTPUT_REPORT:
                self._outputs += 1
                self._note_output(data)
                if not self.is_bt:
                    os.write(self._bt_fd, data)  # wired source: same USB format
                elif len(data) > 1 and data[0] == DS_OUTPUT_REPORT_USB:
                    common = data[1:1 + DS_OUTPUT_COMMON_SIZE]
                    sink = self.output_sink
                    if sink:
                        sink(common)    # rides in the audio stream's next report
                    else:
                        self._direct.update(common)
        elif ev_type == UHID_GET_REPORT:
            req_id, rnum, rtype = struct.unpack_from("<IBB", ev, 4)
            err, data = 0, b""
            self._note_feature("GET", rnum)
            if rtype != UHID_FEATURE_REPORT or not self._feature_supported(rnum):
                err = errno.EIO
            else:
                try:
                    data = self._get_bt_feature(rnum)
                    if rnum == DS_FEATURE_REPORT_PAIRING_INFO and not self._pairing_patched:
                        # Only hid-playstation's probe (the first request) gets the altered
                        # MAC. Steam/SDL read it later as the serial number and must see the
                        # real one, so they treat this as the same controller (same gyro
                        # calibration and config) and prefer it over the Bluetooth twin.
                        data = patch_pairing_info(data)
                        self._pairing_patched = True
                except OSError as e:
                    err = e.errno or errno.EIO
            self._uhid_write(struct.pack("<IIHH", UHID_GET_REPORT_REPLY, req_id, err, len(data)) + data)
        elif ev_type == UHID_SET_REPORT:
            req_id, rnum, rtype, size = struct.unpack_from("<IBBH", ev, 4)
            data = ev[12:12 + size]
            err = 0
            self._note_feature("SET", rnum)
            try:
                if rtype == UHID_FEATURE_REPORT and rnum in SET_FEATURE_BLOCKED:
                    self.log.info("SET feature 0x%02x not forwarded: %s", rnum, data.hex(" "))
                elif rtype == UHID_FEATURE_REPORT and self._feature_supported(rnum):
                    buf = bytearray(usb_feature_to_bt(data) if self.is_bt else data)
                    _ioctl(self._bt_fd, HIDIOCSFEATURE(len(buf)), buf)
                else:
                    err = errno.EIO
            except OSError as e:
                err = e.errno or errno.EIO
            self._uhid_write(struct.pack("<IIH", UHID_SET_REPORT_REPLY, req_id, err))
        # UHID_START / STOP / OPEN / CLOSE need no action.

    def _note_feature(self, kind, rnum):
        """Log the first request of each feature report, to learn what games ask for."""
        key = (kind, rnum)
        if key not in self._seen_features:
            self._seen_features.add(key)
            supported = self._feature_supported(rnum)
            self.log.info("%s feature 0x%02x%s", kind, rnum, "" if supported else " (not supported, refused)")

    def _note_output(self, data):
        """
        Log each new adaptive-trigger setting a game sends (USB output report 0x02:
        valid_flag0 bits 0x04/0x08 = right/left trigger, effect type at bytes 11 and 22).
        """
        if len(data) < 23 or data[0] != DS_OUTPUT_REPORT_USB or not data[1] & 0x0C:
            return
        key = (data[11], data[22])
        if key != self._last_triggers:
            self._last_triggers = key
            self.log.info("Trigger effects from game: right 0x%02x %s, left 0x%02x %s",
                          data[11], data[12:21].hex(" "), data[22], data[23:32].hex(" "))

    def _feature_supported(self, rnum):
        if self.is_bt:
            return rnum in BT_FEATURE_REPORTS
        return rnum in FEATURE_REPORT_SIZES

    # -- source controller side (Bluetooth or real USB) --

    def _get_bt_feature(self, rnum):
        size = FEATURE_REPORT_SIZES.get(rnum)
        if not size:
            raise OSError(errno.EINVAL, "unknown feature report 0x%02x" % rnum)
        buf = bytearray(size)
        buf[0] = rnum
        n = _ioctl(self._bt_fd, HIDIOCGFEATURE(size), buf)
        return bytes(buf[:n if n > 0 else size])

    def _handle_bt_report(self, report):
        if not self.is_bt:
            if report:
                self._uhid_input(bytes(report))  # wired source: already USB format
            return
        usb = bt_input_to_usb(report)
        if usb:
            self._uhid_input(usb)
        elif report and report[0] == DS_INPUT_REPORT_USB:
            # Controller fell back to the simple BT report; ask for full mode again.
            now = time.monotonic()
            if now - self._last_mode_switch > 1.0:
                self._last_mode_switch = now
                try:
                    self._get_bt_feature(DS_FEATURE_REPORT_CALIBRATION)
                except OSError:
                    pass

    def _loop(self):
        last_expose = 0.0
        last_stats = time.monotonic()
        while not self._stop_event.is_set():
            now = time.monotonic()
            if now - last_expose > 2.0:
                last_expose = now
                self._expose_virtual()
            if now - last_stats >= 5.0:
                if self._outputs:
                    self.log.info("Game output reports: %d in %.0f s", self._outputs, now - last_stats)
                self._outputs = 0
                last_stats = now
            pending = self._direct.has_pending()
            readable, _, _ = select.select([self._bt_fd, self._uhid_fd], [], [],
                                           MIN_OUTPUT_INTERVAL if pending else 0.5)
            if pending:
                try:
                    self._flush_direct()
                except OSError as e:
                    if e.errno in (errno.ENODEV, errno.EIO):
                        self.log.info("%s disconnected", self.controller["hidraw"])
                        return
                    raise
            if self._bt_fd in readable:
                try:
                    report = os.read(self._bt_fd, 128)
                except OSError as e:
                    if e.errno in (errno.ENODEV, errno.EIO):
                        self.log.info("%s disconnected", self.controller["hidraw"])
                        return
                    raise
                if not report:
                    return
                self._handle_bt_report(report)
            if self._uhid_fd in readable:
                self._handle_uhid_event(os.read(self._uhid_fd, UHID_EVENT_SIZE))


# --- manager -------------------------------------------------------------------

class Manager:
    """
    Watches for DualSense controllers and keeps a bridge per controller, plus an audio
    stream (Deck audio -> speaker/haptics) for each bridged Bluetooth controller.
    """

    RETRY_DELAY = 10.0
    REBIND_DELAY = 10.0

    PREFIX_SYNC_INTERVAL = 60.0

    def __init__(self, logger, enabled=True, audio=None, user="deck", steam_root=None, notify_low_battery=None):
        from dualsense_audio import DEFAULT_STREAM_OPTIONS
        self.log = logger
        # notify_low_battery(name, capacity): called from the manager thread
        self._notify_low_battery = notify_low_battery
        self._battery_warned = {}   # MAC -> thresholds already announced
        # Proton prefixes get the helper service that lets games find the controller's
        # audio device (see proton_prefixes.py).
        self._prefix_sync = None
        if steam_root:
            from proton_prefixes import PrefixSync
            self._prefix_sync = PrefixSync(logger, steam_root)
        self._next_prefix_sync = 0.0
        self.enabled = enabled
        self.user = user
        # "enabled": stream what the Deck plays (system audio) to the controller.
        # "game_audio": stream what games play into the controller's own audio device
        # (USB gadget backend only); system audio wins when both are on.
        # The rest are AudioStreamer options.
        self.audio = dict(DEFAULT_STREAM_OPTIONS, enabled=False, game_audio=True)
        self.audio.update(audio or {})
        self._gadget_failed = False   # USB gadget backend unusable: uhid bridges only
        self._streamers = {}  # hidraw -> AudioStreamer
        self._stream_failed_at = {}  # hidraw -> monotonic time of the last failed stream
        self._bridges = {}
        self._parked = {}  # MAC -> GadgetBridge waiting for its controller to come back
        self._failed = {}  # hidraw -> (monotonic time, last status)
        self._rebound_at = {}  # HID device name -> monotonic time
        self._rule_installed = False
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._controllers = []

    def start(self):
        self._take_over_instance()
        if not pause_requested():
            try:
                from dualsense_gadget_bridge import cleanup_stale
                cleanup_stale(self.user)
            except Exception:  # noqa: BLE001
                self.log.exception("Cleaning up a stale USB gadget failed")
        self._parent = os.getppid()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ds-manager")
        self._thread.start()

    def _take_over_instance(self):
        """
        Decky can leave an old plugin process running after a restart; two instances
        would fight over the controller, so stop the previous one.
        """
        try:
            with open(PID_FILE) as f:
                old = int(f.read().strip())
            if old != os.getpid():
                with open("/proc/%d/cmdline" % old, "rb") as f:
                    if b"decky-dualsense-wired" in f.read():
                        self.log.info("Stopping previous plugin instance %d", old)
                        os.kill(old, signal.SIGTERM)
                        for _ in range(20):
                            if not os.path.exists("/proc/%d" % old):
                                break
                            time.sleep(0.1)
                        else:
                            os.kill(old, signal.SIGKILL)
        except (OSError, ValueError):
            pass
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))

    def _owns_pid_file(self):
        try:
            with open(PID_FILE) as f:
                return int(f.read().strip()) == os.getpid()
        except (OSError, ValueError):
            return False

    def shutdown(self):
        """Plugin unload: stop bridging and give Steam its access back without reconnecting."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._stop_all()
        if pause_requested():
            # The controller is exposed another way (gadget experiment). Steam must not get
            # the Bluetooth node back: seeing the same serial over "USB" it powers BT off.
            return
        self._return_to_steam()

    def _return_to_steam(self):
        try:
            return_to_steam(self.log)
        except OSError:
            self.log.exception("Giving the controllers back to Steam failed")
            self._remove_rule()
        self._rule_installed = False
        # Steam opens them again now; the next enable must take them away again (re-bind).
        self._rebound_at.clear()

    def set_enabled(self, enabled):
        with self._lock:
            self.enabled = enabled
        if not enabled:
            self._stop_all()
            self._return_to_steam()

    def set_audio(self, options):
        with self._lock:
            self.audio.update(options)
            self._stream_failed_at.clear()
            streamers = list(self._streamers.values())
        for s in streamers:
            s.update(self._stream_options(s.options["source"], s.options.get("sink")))
        # Streams whose source changed are restarted, new ones started, by the next scan.

    def _stream_options(self, source="system", sink=None):
        options = {k: v for k, v in self.audio.items() if k not in ("enabled", "game_audio")}
        options["source"] = source
        if sink:
            options["sink"] = sink
        return options

    def _stop_streamers(self, keys=None):
        with self._lock:
            victims = [(k, s) for k, s in self._streamers.items() if keys is None or k in keys]
            for k, s in victims:
                del self._streamers[k]
                bridge = self._bridges.get(k)
                if bridge is not None and bridge.output_sink == s.submit_output:
                    bridge.output_sink = None   # game output goes direct again
        for _, s in victims:
            s.stop()
        for _, s in victims:
            s.join(timeout=3)

    def _wanted_source(self, bridge):
        """Audio stream source for an active bridge, or None for no stream."""
        if not self.enabled or not bridge.is_bt or bridge.state != "active":
            return None
        if self.audio["enabled"]:
            return "system"
        if self.audio["game_audio"] and getattr(bridge, "sink_ready", False):
            return "controller"
        return None

    def _sync_streamers(self):
        """Run one audio stream per active Bluetooth bridge that wants one (see _wanted_source)."""
        from dualsense_audio import AudioStreamer
        with self._lock:
            wanted = {}
            for k, b in self._bridges.items():
                source = self._wanted_source(b)
                if source:
                    wanted[k] = source
            stale = [k for k, s in self._streamers.items()
                     if wanted.get(k) != s.options["source"] or not s.is_alive()]
            for k in stale:
                s = self._streamers[k]
                if not s.is_alive() and s.state == "error":
                    self._stream_failed_at[k] = time.monotonic()
        if stale:
            self._stop_streamers(stale)
        with self._lock:
            for key, source in wanted.items():
                if key in self._streamers:
                    continue
                if time.monotonic() - self._stream_failed_at.get(key, -self.RETRY_DELAY) < self.RETRY_DELAY:
                    continue
                sink = getattr(self._bridges[key], "sink_name", None) if source == "controller" else None
                streamer = AudioStreamer("/dev/" + key, self.log, user=self.user,
                                         options=self._stream_options(source, sink))
                self._streamers[key] = streamer
                streamer.start()
                # One steady stream to the controller: game output rides in it.
                self._bridges[key].output_sink = streamer.submit_output

    def _remove_rule(self):
        try:
            remove_hide_rule()
        except OSError:
            self.log.exception("Failed to remove %s", UDEV_RULE_PATH)
        self._rule_installed = False

    def _ensure_exclusive(self, c):
        """
        True if nobody but the plugin uses the BT controller. Otherwise re-bind its
        driver (the hide rule then applies to the new nodes) and return False.
        """
        holders = node_holders(device_nodes_of(c["sys_path"]))
        if not holders:
            return True
        last = self._rebound_at.get(c["hid_device"])
        if last is None:
            self.log.info("%s is open by %s, re-binding it to hide it", c["hid_device"], ", ".join(holders))
            self._rebound_at[c["hid_device"]] = time.monotonic()
            try:
                rebind_driver(c["hid_device"])
            except OSError:
                self.log.exception("Failed to rebind %s", c["hid_device"])
            return False
        if time.monotonic() - last < self.REBIND_DELAY:
            return False
        # Re-binding once did not help; bridge anyway rather than loop forever.
        self.log.warning("%s is still open by %s, bridging anyway", c["hid_device"], ", ".join(holders))
        return True

    def status(self):
        with self._lock:
            bridges = dict(self._bridges)
            failed = dict(self._failed)
            controllers = list(self._controllers)
        with self._lock:
            streamers = dict(self._streamers)
            audio = dict(self.audio)
        result = []
        for c in controllers:
            bridge = bridges.get(c["hidraw"])
            if bridge:
                entry = bridge.status()
                streamer = streamers.get(c["hidraw"])
                entry["audio"] = streamer.status() if streamer else None
                result.append(entry)
            elif c["hidraw"] in failed:
                result.append(failed[c["hidraw"]][1])
            else:
                result.append({
                    "hidraw": c["hidraw"], "name": c["name"], "mac": c["mac"],
                    "connection": "bluetooth" if c["bus"] == BUS_BLUETOOTH else "usb",
                    "state": "idle", "error": None, "virtual_hidraw": None,
                })
            result[-1] = dict(result[-1], battery=read_battery(c["mac"]))
        return {
            "enabled": self.enabled,
            "uhid_available": os.path.exists("/dev/uhid"),
            "is_root": os.geteuid() == 0,
            "audio": audio,
            "controllers": result,
        }

    def _stop_all(self):
        self._stop_streamers()
        with self._lock:
            bridges = list(self._bridges.values()) + list(self._parked.values())
            self._bridges.clear()
            self._parked.clear()
        for b in bridges:
            b.stop()
        for b in bridges:
            b.join(timeout=3)

    def _check_batteries(self):
        """Announce each low battery threshold once per discharge."""
        if not self._notify_low_battery:
            return
        with self._lock:
            controllers = list(self._controllers)
        for c in controllers:
            battery = read_battery(c["mac"])
            if not battery:
                continue
            warned = self._battery_warned.setdefault(c["mac"], set())
            capacity = battery["capacity"]
            if battery["status"] != "Discharging":
                warned.clear()
                continue
            for threshold in LOW_BATTERY_THRESHOLDS:
                if capacity > threshold + LOW_BATTERY_REARM:
                    warned.discard(threshold)
            due = [t for t in LOW_BATTERY_THRESHOLDS if capacity <= t and t not in warned]
            if due:
                warned.update(t for t in LOW_BATTERY_THRESHOLDS if capacity <= t)
                self.log.info("%s battery low: %d%%", c["mac"], capacity)
                self._notify_low_battery(c["name"], capacity)

    def _run(self):
        while not self._stop_event.is_set():
            if os.getppid() != self._parent:
                # Decky's loader is gone but this process was left behind.
                self.log.info("Decky loader exited, stopping orphaned plugin instance")
                self._stop_all()
                if self._owns_pid_file():
                    self._remove_rule()
                os._exit(0)
            try:
                self._scan()
                self._sync_streamers()
                self._check_batteries()
            except Exception:  # noqa: BLE001
                self.log.exception("Controller scan failed")
            if self._prefix_sync and self.enabled and time.monotonic() >= self._next_prefix_sync:
                self._next_prefix_sync = time.monotonic() + self.PREFIX_SYNC_INTERVAL
                try:
                    self._prefix_sync.sync()
                except Exception:  # noqa: BLE001
                    self.log.exception("Updating Proton prefixes failed")
            self._stop_event.wait(2.0)

    def _scan(self):
        controllers = find_dualsenses()
        present = {c["hidraw"] for c in controllers}
        with self._lock:
            self._controllers = controllers
            # Forget bridges whose controller is gone or whose thread has ended.
            for key, bridge in list(self._bridges.items()):
                if not bridge.is_alive():
                    if getattr(bridge, "setup_failed", False):
                        # The gadget could not be created: bridge the uhid way from now on.
                        self.log.warning("USB gadget backend failed (%s), using uhid bridges", bridge.error)
                        self._gadget_failed = True
                    elif bridge.state == "error":
                        self._failed[key] = (time.monotonic(), bridge.status())
                    del self._bridges[key]
                elif key not in present:
                    del self._bridges[key]
                    if getattr(bridge, "mode", "") == "usb-gadget":
                        # Keeps the virtual controller "plugged in" for a while; the
                        # controller is re-attached to it if it comes back (see _new_bridge).
                        self._parked[bridge.mac] = bridge
                    else:
                        bridge.stop()
            for mac, bridge in list(self._parked.items()):
                if not bridge.is_alive():
                    del self._parked[mac]
            for key in list(self._failed):
                if key not in present:
                    del self._failed[key]
            if not self.enabled:
                return
            if not self._rule_installed:
                install_hide_rule()
                self._rule_installed = True
            paused = pause_requested()
            if paused:
                victims = list(self._bridges.values())
                self._bridges.clear()
        if paused:
            for b in victims:
                b.stop()
            return
        with self._lock:
            for c in controllers:
                key = c["hidraw"]
                if key in self._bridges:
                    continue
                if key in self._failed:
                    if time.monotonic() - self._failed[key][0] < self.RETRY_DELAY:
                        continue
                    del self._failed[key]
                if not self._ensure_exclusive(c):
                    continue
                parked = self._parked.pop(c["mac"], None)
                if parked is not None and parked.is_alive():
                    # Same controller back: continue on its virtual controller, so games
                    # keep the device and the audio streams they opened.
                    parked.attach(c)
                    self._bridges[key] = parked
                    continue
                bridge = self._new_bridge(c)
                self._bridges[key] = bridge
                bridge.start()

    def _new_bridge(self, c):
        """
        The first Bluetooth controller becomes a USB gadget (a real wired DualSense to games,
        with its audio device); there is only one virtual UDC, so further controllers, wired
        ones, and everything after a gadget failure use uhid bridges. Caller holds the lock.
        """
        import dualsense_gadget
        from dualsense_gadget_bridge import GadgetBridge, can_use_gadget
        in_use = sum(1 for b in list(self._bridges.values()) + list(self._parked.values())
                     if getattr(b, "mode", "") == "usb-gadget" and b.is_alive())
        ports = len(glob.glob("/sys/class/udc/dummy_udc.*")) or dualsense_gadget.DUMMY_UDC_COUNT
        if not self._gadget_failed and in_use < ports and can_use_gadget(c):
            return GadgetBridge(c, self.log, user=self.user)
        return Bridge(c, self.log)
