"""
Virtual *USB* DualSense built with the kernel USB gadget framework on the dummy_hcd
virtual bus: a composite device with a UAC2 audio function (4-channel output like a
wired DualSense, plus a microphone) and a HID function with the DualSense report
descriptor.

Unlike a uhid device it has a real USB parent, so Wine (winebus for HID, winepulse
for audio) derives the same Container ID for both, and games can find "their"
controller's audio device — native speaker sounds and HD haptics.
See research/FINDINGS.md ("Container ID в Wine").
"""

import glob
import os
import subprocess
import threading

CONFIGFS = "/sys/kernel/config/usb_gadget"
GADGET_NAME = "decky_dualsense"

# include/uapi/linux/usb/g_hid.h
MAX_REPORT_LENGTH = 64
USB_HIDG_REPORT_SIZE = 1 + 1 + 2 + MAX_REPORT_LENGTH + 4

# Interfaces placed before the HID one so it gets number 3, like on a real DualSense.
PADDING_INTERFACES = 3


def _ioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (ord("g") << 8) | nr


GADGET_HID_READ_GET_REPORT_ID = _ioc(2, 0x41, 1)
GADGET_HID_WRITE_GET_REPORT = _ioc(1, 0x42, USB_HIDG_REPORT_SIZE)


def _write(path, value):
    with open(path, "wb" if isinstance(value, bytes) else "w") as f:
        f.write(value)


def _modprobe(*modules):
    env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
    for m in modules:
        subprocess.run(["/usr/bin/modprobe", m], env=env, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def gadget_path(name=GADGET_NAME):
    return os.path.join(CONFIGFS, name)


# Virtual USB ports: one per controller exposed at the same time. Each dummy_hcd instance
# is its own host bus, so every controller also gets its own Container ID in Wine.
DUMMY_UDC_COUNT = 4
_udc_lock = threading.Lock()


def ensure_dummy_hcd(count=DUMMY_UDC_COUNT):
    """
    Load dummy_hcd with `count` virtual ports. An instance loaded with fewer (e.g. by an older
    version of the plugin) is reloaded, but only while none of our gadgets is bound to it.
    """
    env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
    try:
        with open("/sys/module/dummy_hcd/parameters/num") as f:
            loaded = int(f.read().strip())
    except (OSError, ValueError):
        loaded = 0
    if loaded >= count:
        return
    if loaded and bound_udcs():
        return
    if loaded:
        subprocess.run(["/usr/bin/modprobe", "-r", "dummy_hcd"], env=env, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["/usr/bin/modprobe", "dummy_hcd", "num=%d" % count], env=env, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def bound_udcs():
    """UDC names that some configfs gadget is bound to."""
    bound = set()
    for udc_file in glob.glob(os.path.join(CONFIGFS, "*", "UDC")):
        try:
            with open(udc_file) as f:
                name = f.read().strip()
        except OSError:
            continue
        if name:
            bound.add(name)
    return bound


def free_dummy_udc():
    busy = bound_udcs()
    udcs = sorted((os.path.basename(p) for p in glob.glob("/sys/class/udc/dummy_udc.*")),
                  key=lambda n: int(n.rsplit(".", 1)[1]))
    return next((u for u in udcs if u not in busy), None)


def host_bus_of(udc):
    """dummy_udc.N is the device side of the host controller dummy_hcd.N."""
    return "dummy_hcd." + udc.rsplit(".", 1)[1]


def our_gadgets():
    return [os.path.basename(p) for p in glob.glob(os.path.join(CONFIGFS, GADGET_NAME + "*"))]


def create(report_descriptor, vid=0x054C, pid=0x0CE6, name=GADGET_NAME,
           product="DualSense Wireless Controller", manufacturer="Sony Interactive Entertainment",
           with_audio=False, padding_interfaces=PADDING_INTERFACES, out_endpoint=False):
    """
    Create and bind the gadget to a free virtual port. Returns (/dev/hidgN path, UDC name).

    with_audio adds a UAC2 function, but dummy_hcd has no isochronous support, so it
    cannot bind there; audio is provided by a PipeWire sink pointing at this device
    instead (see make_sink_properties).
    """
    ensure_dummy_hcd()
    _modprobe("libcomposite", "usb_f_uac2", "usb_f_hid")
    if not os.path.isdir(CONFIGFS):
        raise OSError("configfs usb_gadget is not available")
    g = gadget_path(name)
    if os.path.isdir(g):
        destroy(name)
    os.makedirs(g)
    _write(g + "/idVendor", "0x%04x" % vid)
    _write(g + "/idProduct", "0x%04x" % pid)
    _write(g + "/bcdDevice", "0x0100")
    _write(g + "/bcdUSB", "0x0200")
    os.makedirs(g + "/strings/0x409")
    _write(g + "/strings/0x409/manufacturer", manufacturer)
    _write(g + "/strings/0x409/product", product)
    os.makedirs(g + "/configs/c.1/strings/0x409")
    _write(g + "/configs/c.1/MaxPower", "500")

    audio = g + "/functions/uac2.ds"
    if with_audio:
        # Audio first, so the HID interface ends up as interface 3 like on the real controller.
        os.makedirs(audio)
        _write(audio + "/c_chmask", "0x33")     # host playback: 4 channels FL FR BL BR (speaker + haptics)
        _write(audio + "/c_srate", "48000")
        _write(audio + "/c_ssize", "2")
        _write(audio + "/p_chmask", "0x3")      # host capture: stereo microphone
        _write(audio + "/p_srate", "48000")
        _write(audio + "/p_ssize", "2")
        if os.path.exists(audio + "/function_name"):
            _write(audio + "/function_name", product)

    # A real DualSense has its HID on interface 3 (0-2 are audio), and games under Wine see
    # that number ("MI_03", winebus takes it from the hidraw phys ".../input3"). UAC2 can't
    # bind on dummy_hcd, so pad with driverless bulk-only test functions instead.
    padding = []
    if not with_audio:
        _modprobe("usb_f_ss_lb")
        for i in range(padding_interfaces):
            pad = g + "/functions/SourceSink.pad%d" % i
            os.makedirs(pad)
            padding.append(pad)

    hid = g + "/functions/hid.ds"
    os.makedirs(hid)
    _write(hid + "/protocol", "0")
    _write(hid + "/subclass", "0")
    _write(hid + "/report_length", str(MAX_REPORT_LENGTH))
    # With an interrupt OUT endpoint f_hid stalls every SET_REPORT, and games treat a
    # failed SET feature as a broken controller. Without it, output and feature reports
    # both arrive through read() (told apart by report ID).
    _write(hid + "/no_out_endpoint", "0" if out_endpoint else "1")
    _write(hid + "/report_desc", bytes(report_descriptor))

    if with_audio:
        os.symlink(audio, g + "/configs/c.1/uac2.ds")
    # Interfaces are numbered in link order.
    for pad in padding:
        os.symlink(pad, g + "/configs/c.1/" + os.path.basename(pad))
    os.symlink(hid, g + "/configs/c.1/hid.ds")
    with _udc_lock:     # two controllers connecting at once must not pick the same port
        udc = free_dummy_udc()
        if not udc:
            raise OSError("no free dummy_hcd port")
        _write(g + "/UDC", udc)

    major_minor = open(hid + "/dev").read().strip()
    for node in glob.glob("/dev/hidg*"):
        st = os.stat(node)
        if "%d:%d" % (os.major(st.st_rdev), os.minor(st.st_rdev)) == major_minor:
            return node, udc
    raise OSError("hidg device node for %s not found" % major_minor)


def destroy(name=GADGET_NAME):
    g = gadget_path(name)
    if not os.path.isdir(g):
        return
    try:
        _write(g + "/UDC", "")
    except OSError:
        pass
    for link in glob.glob(g + "/configs/c.1/*.ds") + glob.glob(g + "/configs/c.1/SourceSink.*"):
        os.unlink(link)
    pads = glob.glob(g + "/functions/SourceSink.*")
    for d in [g + "/configs/c.1/strings/0x409", g + "/configs/c.1",
              g + "/functions/uac2.ds", g + "/functions/hid.ds"] + pads + [g + "/strings/0x409", g]:
        try:
            os.rmdir(d)
        except OSError:
            pass


def preload_report(fd, report, userspace_req=False):
    """Register a GET_REPORT answer (report includes its ID byte)."""
    import fcntl
    data = bytes(report[:MAX_REPORT_LENGTH])
    buf = bytearray(USB_HIDG_REPORT_SIZE)
    buf[0] = data[0]
    buf[1] = 1 if userspace_req else 0
    buf[2:4] = len(data).to_bytes(2, "little")
    buf[4:4 + len(data)] = data
    fcntl.ioctl(fd, GADGET_HID_WRITE_GET_REPORT, buf)


def pending_get_report_id(fd):
    import fcntl
    buf = bytearray(1)
    fcntl.ioctl(fd, GADGET_HID_READ_GET_REPORT_ID, buf, True)
    return buf[0]


def host_side_devices(vid=0x054C, pid=0x0CE6, udc=None):
    """
    USB devices with our VID/PID on a dummy_hcd host bus (only the one behind `udc` if given),
    with their hidraw/sound children.
    """
    bus = "/%s/" % host_bus_of(udc) if udc else "dummy_hcd"
    result = []
    for dev in glob.glob("/sys/bus/usb/devices/*"):
        try:
            if open(dev + "/idVendor").read().strip() != "%04x" % vid:
                continue
            if open(dev + "/idProduct").read().strip() != "%04x" % pid:
                continue
        except OSError:
            continue
        real = os.path.realpath(dev)
        if bus not in real:
            continue
        hidraw = [os.path.basename(p) for p in glob.glob(real + "/*/*/hidraw/hidraw*")]
        sound = [os.path.basename(p) for p in glob.glob(real + "/*/sound/card*")]
        result.append({"path": real, "hidraw": hidraw, "sound": sound,
                       "busnum": open(dev + "/busnum").read().strip(),
                       "devnum": open(dev + "/devnum").read().strip()})
    return result


def make_sink_properties(host_device_path, vid=0x054C, pid=0x0CE6, description="DualSense Wireless Controller"):
    """
    PipeWire/Pulse sink properties that make Wine's winepulse derive the same Container ID
    as winebus does for the gadget's hidraw: winepulse resolves "sysfs.path" and walks up to
    the parent usb_device (dlls/winepulse.drv/pulse.c get_container_id), winebus walks from
    the hidraw to the same usb_device (dlls/winebus.sys/bus_udev.c).

    The description becomes the endpoint's friendly name; as on Windows it must contain the
    controller's USB product string, which games compare with the HID product string.
    """
    interfaces = sorted(glob.glob(host_device_path + "/*:1.*"))
    target = interfaces[0] if interfaces else host_device_path
    sysfs_path = target[len("/sys"):] if target.startswith("/sys") else target
    return {
        "device.description": description,
        "device.bus": "usb",
        "device.vendor.id": "0x%04x" % vid,
        "device.product.id": "0x%04x" % pid,
        "sysfs.path": sysfs_path,
    }
