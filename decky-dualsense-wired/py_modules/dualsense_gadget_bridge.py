"""
Bluetooth DualSense exposed as a *real* wired one: a USB gadget on the dummy_hcd virtual bus
(see dualsense_gadget.py) plus a 4-channel PipeWire sink that stands for the controller's USB
audio device. Unlike the uhid bridge this gives games under Proton a USB device tree, so they
find the controller's audio device and play speaker sounds and HD haptics into it; the plugin
streams that audio to the controller over Bluetooth (AudioStreamer, source "controller").

Findings behind the details are in research/FINDINGS.md and the plugin status notes:
- the HID function must be interface 3 like on a real DualSense (MI_03), hence the padding;
- no interrupt OUT endpoint: with one f_hid stalls SET_REPORT and games retry forever;
- SET feature 0x08 (sent by Proton games) is acknowledged but never forwarded, it powers
  the controller off;
- only hid-playstation's first 0x09 read gets an altered MAC (it refuses a second device
  with the BT controller's MAC); everyone else, Steam included, must see the real one;
- 0x09 must always be answered at once and the hidraw node opened up right away, or Steam
  falls back to the evdev joystick (wrong button map: PS dead, R3 = Steam button);
- games pick the controller's audio device only at start: when the Bluetooth link drops, the
  virtual device stays "plugged in" for PARK_TIMEOUT and the controller is re-attached to it
  when it comes back, so games keep their audio streams (and get their trigger effects again).
"""

import errno
import os
import select
import threading
import time

import dualsense_gadget as gadget
from dualsense_audio import GAME_AUDIO_SINK, _pactl, get_feature, set_feature
from dualsense_bridge import (
    BT_FEATURE_REPORTS, BUS_BLUETOOTH, DS_FEATURE_REPORT_CALIBRATION, DS_FEATURE_REPORT_PAIRING_INFO,
    DS_INPUT_REPORT_USB, DS_OUTPUT_COMMON_SIZE, DS_OUTPUT_REPORT_USB, DS_USB_RDESC, DUALSENSE_PIDS,
    FEATURE_REPORT_SIZES, MIN_OUTPUT_INTERVAL, SET_FEATURE_BLOCKED, OutputState, bt_input_to_usb,
    common_to_bt, device_nodes_of,
)

# Reports the host side reads while the driver probes; answered statically from the controller.
PRELOADED_FEATURES = (DS_FEATURE_REPORT_CALIBRATION, 0x20, 0x22)
HOST_POLL_INTERVAL = 0.1
EXPOSE_DELAYS = (0.0, 1.0)
STATS_INTERVAL = 30.0
# How long the virtual controller stays after its Bluetooth controller went away.
PARK_TIMEOUT = 120.0


def cleanup_stale(user):
    """Remove gadgets and sinks left behind by a crashed instance; set up the virtual ports."""
    for name in gadget.our_gadgets():
        gadget.destroy(name)
    for line in _pactl(user, "list", "short", "modules").splitlines():
        if "sink_name=" + GAME_AUDIO_SINK in line:
            _pactl(user, "unload-module", line.split()[0])
    gadget.ensure_dummy_hcd()


def neutral_input(last):
    """USB input report 0x01 with sticks centred and nothing pressed or touched."""
    report = bytearray(last) if last else bytearray(64)
    report[0] = DS_INPUT_REPORT_USB
    report[1:5] = b"\x80\x80\x80\x80"   # sticks
    report[5] = report[6] = 0           # triggers
    report[8] = 0x08                    # d-pad released, no face buttons
    report[9] = report[10] = 0
    report[33] |= 0x80                  # touch points: no contact
    report[37] |= 0x80
    return bytes(report)


class GadgetBridge(threading.Thread):
    """One Bluetooth DualSense <-> virtual USB DualSense (only one at a time: one dummy UDC)."""

    mode = "usb-gadget"
    is_bt = True

    def __init__(self, controller, logger, user="deck", padding=gadget.PADDING_INTERFACES, out_endpoint=False):
        super().__init__(daemon=True, name="ds-gadget-" + controller["hidraw"])
        self.controller = controller
        self.mac = controller["mac"]
        # One gadget and one audio sink per controller (sink name set once a port is known).
        self._gadget_name = "%s_%s" % (gadget.GADGET_NAME, (self.mac or controller["hidraw"]).replace(":", ""))
        self._udc = None
        self.sink_name = None
        self.log = logger
        self.user = user
        self.padding = padding
        self.out_endpoint = out_endpoint
        self.state = "starting"
        self.error = None
        self.virtual_hidraw = None
        self.sink_ready = False
        self.setup_failed = False
        # bt_reports: input reports from the controller; inputs: delivered to the host side
        self.stats = {"bt_reports": 0, "inputs": 0, "outputs": 0, "dropped_inputs": 0}
        self._stop_event = threading.Event()
        self._bt_fd = -1
        self._gfd = -1
        self._hidden_modes = {}
        self._sink_module = None
        self._sink_thread = None
        self._pairing_requests = 0
        self._seq = 0
        self._last_mode_switch = 0.0
        self._seen_features = set()
        self._feature_cache = {}
        self._last_input = None
        self._host = None
        self._expose_at = []
        self._next_host_check = 0.0
        self._returning = None   # controller handed over by attach()
        # Set by the Manager while an audio stream runs for this controller: game output then
        # goes into the stream (see AudioStreamer.submit_output), otherwise it is sent here.
        self.output_sink = None
        self._direct = OutputState()
        self._last_direct = 0.0
        # Everything the game has set (triggers, LEDs ...), to restore it on a new connection.
        self._history = OutputState()

    def stop(self):
        self._stop_event.set()

    def attach(self, controller):
        """The Bluetooth controller is back (same MAC): continue with it."""
        self._returning = controller

    def waiting(self):
        return self.state == "waiting"

    def status(self):
        return {
            "hidraw": self.controller["hidraw"],
            "name": self.controller["name"],
            "mac": self.controller["mac"],
            "connection": "bluetooth",
            "mode": self.mode,
            "state": self.state,
            "error": self.error,
            "virtual_hidraw": self.virtual_hidraw,
            "game_audio_sink": self.sink_ready,
        }

    # -- lifecycle --

    def run(self):
        step = "start"
        try:
            step = "open /dev/" + self.controller["hidraw"]
            self._open_controller()
            step = "create USB gadget"
            hidg, self._udc = gadget.create(DS_USB_RDESC, pid=self.controller["pid"], name=self._gadget_name,
                                            product="%s" % DUALSENSE_PIDS[self.controller["pid"]],
                                            padding_interfaces=self.padding, out_endpoint=self.out_endpoint)
            self.sink_name = "%s_%s" % (GAME_AUDIO_SINK, self._udc.rsplit(".", 1)[1])
            self._gfd = os.open(hidg, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
            step = "preload feature reports"
            for rid in PRELOADED_FEATURES:
                try:
                    data = get_feature(self._bt_fd, rid, FEATURE_REPORT_SIZES[rid])
                    self._feature_cache[rid] = data
                    gadget.preload_report(self._gfd, data)
                except OSError as e:
                    self.log.warning("Preloading feature 0x%02x failed: %s", rid, e)
            self.state = "active"
            self.log.info("Exposing %s (%s) as a USB DualSense on %s", self.controller["hidraw"], self.mac, hidg)
            step = "bridge loop"
            while not self._stop_event.is_set():
                self._loop()
                if self._stop_event.is_set() or not self._wait_for_controller():
                    break
            self.state = "stopped"
        except Exception as e:  # noqa: BLE001 - report any failure to the UI
            # Failing before the gadget worked means this backend is unusable here.
            self.setup_failed = self.state not in ("active", "waiting")
            self.state = "error"
            self.error = "%s: %s" % (step, e)
            self.log.exception("USB gadget bridge for %s failed at step '%s'", self.controller["hidraw"], step)
        finally:
            self._close()

    def _open_controller(self):
        self._bt_fd = os.open("/dev/" + self.controller["hidraw"], os.O_RDWR | os.O_CLOEXEC)
        # Reading calibration also switches the controller to the full 0x31 input report.
        get_feature(self._bt_fd, DS_FEATURE_REPORT_CALIBRATION, FEATURE_REPORT_SIZES[DS_FEATURE_REPORT_CALIBRATION])
        for node in device_nodes_of(self.controller["sys_path"]):
            try:
                self._hidden_modes[node] = os.stat(node).st_mode & 0o7777
                os.chmod(node, 0o600)
            except OSError:
                pass

    def _close_controller(self):
        if self._bt_fd >= 0:
            os.close(self._bt_fd)
            self._bt_fd = -1
        for node, mode in self._hidden_modes.items():
            try:
                os.chmod(node, mode)
            except OSError:
                pass
        self._hidden_modes.clear()

    def _close(self):
        self.sink_ready = False
        if self._sink_thread:
            self._sink_thread.join(timeout=10)
        if self._sink_module:
            _pactl(self.user, "unload-module", self._sink_module)
            self._sink_module = None
        if self._gfd >= 0:
            os.close(self._gfd)
            self._gfd = -1
        # The gadget goes before the Bluetooth node is restored: Steam powers a Bluetooth
        # controller off when it sees the same one connected over "USB".
        gadget.destroy(self._gadget_name)
        self._close_controller()
        self.virtual_hidraw = None

    def _wait_for_controller(self):
        """
        Bluetooth link lost: keep the virtual controller (and its audio device) for
        PARK_TIMEOUT. True when the controller came back and was re-attached.
        """
        self._close_controller()
        self.state = "waiting"
        self.log.info("%s gone, keeping the virtual controller for %.0f s", self.mac, PARK_TIMEOUT)
        try:
            os.write(self._gfd, neutral_input(self._last_input))
        except OSError:
            pass
        poller = select.poll()
        poller.register(self._gfd, select.POLLIN | select.POLLPRI)
        deadline = time.monotonic() + PARK_TIMEOUT
        while not self._stop_event.is_set() and time.monotonic() < deadline:
            controller, self._returning = self._returning, None
            if controller:
                try:
                    self.controller = controller
                    self._open_controller()
                except OSError as e:
                    self.log.warning("Re-attaching %s failed: %s", controller["hidraw"], e)
                    self._close_controller()
                    continue
                # The controller forgot everything: restore what the game had set.
                restore = self._history.snapshot()
                if restore:
                    self._direct.update(restore)
                self.state = "active"
                self.log.info("%s is back as %s, re-attached to the virtual controller",
                              self.mac, controller["hidraw"])
                return True
            for fd, ev in poller.poll(200):
                if ev & select.POLLPRI:
                    self._answer_get_report()
                elif ev & select.POLLIN:
                    try:
                        self._handle_host_report(os.read(self._gfd, 128))
                    except BlockingIOError:
                        pass
            self._host_side_tasks()
        self.log.info("%s did not come back, removing the virtual controller", self.mac)
        return False

    # -- host side --

    def _load_sink(self, host):
        props = gadget.make_sink_properties(host["path"], pid=self.controller["pid"],
                                            description=DUALSENSE_PIDS[self.controller["pid"]])
        prop_str = " ".join('%s="%s"' % kv for kv in props.items())
        module = _pactl(self.user, "load-module", "module-null-sink", "sink_name=" + self.sink_name,
                        "channels=4", "channel_map=front-left,front-right,rear-left,rear-right",
                        "rate=48000", "format=s16le", "sink_properties='%s'" % prop_str).strip()
        if not module.isdigit():
            self.log.warning("Creating the controller audio sink failed: %s", module or "no output")
            return
        self._sink_module = module
        if self._stop_event.is_set():
            return
        self.sink_ready = True
        self.log.info("Controller audio device: sink %s (%s)", self.sink_name, props["sysfs.path"])

    def _expose(self, host):
        # The node appears as root 0600 and udev grants access a moment later; if Steam looks
        # in between it skips HIDAPI and uses the evdev joystick (wrong button map). Opening
        # it ourselves (twice) also makes Steam re-check it.
        for node in host["hidraw"]:
            try:
                os.chmod("/dev/" + node, 0o666)
            except OSError:
                pass

    def _host_side_tasks(self):
        """Find the host-side device once it enumerates: open it up, create the audio sink."""
        now = time.monotonic()
        if self._host is None and now >= self._next_host_check:
            self._next_host_check = now + HOST_POLL_INTERVAL
            found = gadget.host_side_devices(pid=self.controller["pid"], udc=self._udc)
            if found and found[0]["hidraw"]:
                self._host = found[0]
                self.virtual_hidraw = self._host["hidraw"][0]
                self._expose_at = [now + d for d in EXPOSE_DELAYS]
                self._sink_thread = threading.Thread(target=self._load_sink, args=(self._host,), daemon=True,
                                                     name=self.name + "-sink")
                self._sink_thread.start()
        if self._expose_at and now >= self._expose_at[0]:
            self._expose_at.pop(0)
            self._expose(self._host)

    # -- feature reports --

    def _answer_get_report(self):
        rid = gadget.pending_get_report_id(self._gfd)
        size = FEATURE_REPORT_SIZES.get(rid, 64)
        data = self._feature_cache.get(rid, bytes([rid]) + bytes(size - 1))
        if rid in BT_FEATURE_REPORTS and self._bt_fd >= 0:
            try:
                data = get_feature(self._bt_fd, rid, size)
                self._feature_cache[rid] = data
            except OSError:
                pass
        self._note_feature("GET", rid)
        userspace = False
        if rid == DS_FEATURE_REPORT_PAIRING_INFO:
            self._pairing_requests += 1
            if self._pairing_requests == 1:
                # Only hid-playstation's probe: it refuses a second device with the MAC of
                # the Bluetooth controller. Different bit than the uhid bridge uses.
                data = bytearray(data)
                data[6] ^= 0x04
                data = bytes(data)
            # Ask us once more (that answer is the real MAC); after that the kernel answers
            # by itself, so Steam's read can never time out.
            userspace = self._pairing_requests < 2
        gadget.preload_report(self._gfd, data, userspace_req=userspace)

    def _handle_host_report(self, data):
        if data and data[0] in FEATURE_REPORT_SIZES:
            # SET feature (control SET_REPORT); f_hid has already acknowledged it.
            rid = data[0]
            self._note_feature("SET", rid)
            if rid in SET_FEATURE_BLOCKED:
                self.log.info("SET feature 0x%02x not forwarded: %s", rid, data[:8].hex(" "))
            elif rid in BT_FEATURE_REPORTS and self._bt_fd >= 0:
                size = FEATURE_REPORT_SIZES[rid]
                try:
                    set_feature(self._bt_fd, bytes(data[:size]).ljust(size, b"\0"))
                except OSError as e:
                    self.log.warning("SET feature 0x%02x failed: %s", rid, e)
            return
        if len(data) < 2 or data[0] != DS_OUTPUT_REPORT_USB:
            return
        self.stats["outputs"] += 1
        common = data[1:1 + DS_OUTPUT_COMMON_SIZE]
        self._history.update(common)
        sink = self.output_sink
        if sink and self._bt_fd >= 0:
            sink(common)        # rides in the audio stream's next report
        else:
            self._direct.update(common)

    def _flush_direct(self):
        """Send merged game output on its own, at most every MIN_OUTPUT_INTERVAL."""
        now = time.monotonic()
        if now - self._last_direct < MIN_OUTPUT_INTERVAL:
            return
        common = self._direct.take()
        if common:
            os.write(self._bt_fd, common_to_bt(common, self._seq))   # ENODEV/EIO: handled by _loop
            self._seq = (self._seq + 1) & 0x0F
            self._last_direct = now

    def _note_feature(self, kind, rid):
        if (kind, rid) not in self._seen_features:
            self._seen_features.add((kind, rid))
            self.log.info("%s feature 0x%02x", kind, rid)

    # -- controller side --

    def _handle_bt_report(self, report):
        self.stats["bt_reports"] += 1
        usb = bt_input_to_usb(report)
        if usb:
            self._last_input = usb
            try:
                os.write(self._gfd, usb)
                self.stats["inputs"] += 1
            except OSError:
                # EAGAIN: the host has not polled the previous report yet; ESHUTDOWN:
                # not enumerated yet. Input reports are state snapshots, dropping is fine.
                self.stats["dropped_inputs"] += 1
        elif report and report[0] == DS_INPUT_REPORT_USB:
            # Controller fell back to the simple Bluetooth report; ask for full mode again.
            now = time.monotonic()
            if now - self._last_mode_switch > 1.0:
                self._last_mode_switch = now
                try:
                    get_feature(self._bt_fd, DS_FEATURE_REPORT_CALIBRATION,
                                FEATURE_REPORT_SIZES[DS_FEATURE_REPORT_CALIBRATION])
                except OSError:
                    pass

    def _poll_once(self, poller):
        timeout = int(MIN_OUTPUT_INTERVAL * 1000) if self._direct.has_pending() else 200
        for fd, ev in poller.poll(timeout):
            if fd == self._bt_fd:
                report = os.read(self._bt_fd, 128)
                if not report:
                    self._bt_gone = True
                    return
                self._handle_bt_report(report)
            elif ev & select.POLLPRI:
                self._answer_get_report()
            elif ev & select.POLLIN:
                try:
                    data = os.read(self._gfd, 128)
                except BlockingIOError:
                    continue
                self._handle_host_report(data)
        if self._direct.has_pending():
            self._flush_direct()

    def _loop(self):
        """Bridge until the Bluetooth controller goes away (returns) or stop()."""
        self._bt_gone = False
        poller = select.poll()
        poller.register(self._bt_fd, select.POLLIN)
        poller.register(self._gfd, select.POLLIN | select.POLLPRI)
        next_stats = time.monotonic() + STATS_INTERVAL
        last_stats = dict(self.stats)
        while not self._stop_event.is_set():
            try:
                self._poll_once(poller)
            except OSError as e:
                if e.errno in (errno.ENODEV, errno.EIO):
                    self.log.info("%s disconnected", self.controller["hidraw"])
                    return
                raise
            if self._bt_gone:
                return
            self._host_side_tasks()
            now = time.monotonic()
            if now >= next_stats:
                next_stats = now + STATS_INTERVAL
                self._log_stats(last_stats)
                last_stats = dict(self.stats)

    def _log_stats(self, last):
        d = {k: v - last.get(k, 0) for k, v in self.stats.items()}
        if d["bt_reports"] and not d["inputs"]:
            # Controller talks but nothing reaches Steam/games: the case to diagnose.
            self.log.warning("USB gadget: %d reports from the controller in %.0f s, none delivered "
                             "(dropped %d, host %s)", d["bt_reports"], STATS_INTERVAL, d["dropped_inputs"],
                             self.virtual_hidraw)
        else:
            self.log.info("USB gadget: controller reports %d, delivered %d, dropped %d, outputs %d in %.0f s",
                          d["bt_reports"], d["inputs"], d["dropped_inputs"], d["outputs"], STATS_INTERVAL)


def can_use_gadget(controller):
    """Bluetooth controllers get the USB gadget; wired ones already are real USB devices."""
    return controller.get("bus") == BUS_BLUETOOTH and controller.get("pid") in DUALSENSE_PIDS
