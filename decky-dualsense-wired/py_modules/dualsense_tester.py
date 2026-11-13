"""
Controller test page backend: works with the controller the way a game does.

It opens the *virtual* (USB) controller that the plugin exposes, so everything it shows or
does goes through the whole chain: input reports Bluetooth -> virtual USB, output reports
(rumble, triggers, LEDs) virtual USB -> Bluetooth, sound into the controller's audio device
(the 4-channel sink games play into) and the controller microphone source.

The frontend polls state() a few dozen times a second; when it stops polling (page closed,
plugin panel gone) the tester shuts itself down.
"""

import array
import math
import os
import pwd
import select
import subprocess
import threading
import time

from dualsense_audio import GAME_AUDIO_SINK

RATE = 48000
FRAME = 480                  # 10 ms
PLAY_AHEAD = 0.02            # how much audio the player keeps written ahead of time
IDLE_TIMEOUT = 8.0           # no state() call for this long: the page is gone
RUMBLE_SECONDS = 1.5
MIC_RECORD_SECONDS = 5.0
HOLD_TO_EXIT = 1.5           # capture mode: hold circle this long to leave it

DS_INPUT_REPORT_USB = 0x01
DS_OUTPUT_REPORT_USB = 0x02
DS_OUTPUT_REPORT_USB_SIZE = 63

# USB input report 0x01 (offsets include the report ID byte).
BUTTONS = {
    "square": (8, 0x10), "cross": (8, 0x20), "circle": (8, 0x40), "triangle": (8, 0x80),
    "l1": (9, 0x01), "r1": (9, 0x02), "l2": (9, 0x04), "r2": (9, 0x08),
    "create": (9, 0x10), "options": (9, 0x20), "l3": (9, 0x40), "r3": (9, 0x80),
    "ps": (10, 0x01), "touchpad": (10, 0x02), "mute": (10, 0x04),
    # DualSense Edge
    "fn_left": (10, 0x10), "fn_right": (10, 0x20), "back_left": (10, 0x40), "back_right": (10, 0x80),
}
HAT = {0: "up", 1: "up right", 2: "right", 3: "down right", 4: "down", 5: "down left", 6: "left", 7: "up left"}
STATUS_OFFSET = 54           # bit0 headphones, bit1 headset mic (bit2 toggles every report)

# Legacy trigger effect modes (public DualSense documentation), parameters after the mode byte.
TRIGGER_EFFECTS = {
    "off": [0x05],
    "resistance": [0x01, 0x00, 0xFF],            # continuous resistance from the start
    "section": [0x02, 0x40, 0xA0],               # resistance between two positions
    "weapon": [0x25, 0x84, 0x00, 0x07],          # click: start zone 2, end zone 7, strength 8
    "vibration": [0x06, 0x20, 0xFF, 0x00],       # frequency, amplitude, start
}
LIGHTBAR = {
    "red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255),
    "white": (255, 255, 255), "off": (0, 0, 0),
}


def _int16(r, o):
    v = r[o] | r[o + 1] << 8
    return v - 0x10000 if v & 0x8000 else v


def parse_input(r):
    buttons = {name: bool(r[o] & bit) for name, (o, bit) in BUTTONS.items()}
    hat = r[8] & 0x0F
    direction = HAT.get(hat, "")
    for d in ("up", "down", "left", "right"):
        buttons["dpad_" + d] = d in direction.split()
    touch = []
    for o in (33, 37):
        if not r[o] & 0x80:
            touch.append({"x": r[o + 1] | (r[o + 2] & 0x0F) << 8, "y": r[o + 2] >> 4 | r[o + 3] << 4})
    battery = r[53]
    return {
        "buttons": buttons,
        "sticks": [r[1], r[2], r[3], r[4]],
        "triggers": [r[5], r[6]],
        "gyro": [_int16(r, 16), _int16(r, 18), _int16(r, 20)],
        "accel": [_int16(r, 22), _int16(r, 24), _int16(r, 26)],
        "touch": touch,
        "battery": min((battery & 0x0F) * 10 + 5, 100),
        "charging": battery >> 4 in (1, 2),
        "headphones": bool(r[STATUS_OFFSET] & 0x01),
        "headset_mic": bool(r[STATUS_OFFSET] & 0x02),
    }


def _user_command(user, cmd):
    pw = pwd.getpwnam(user)
    env = {"PATH": "/usr/bin:/bin", "XDG_RUNTIME_DIR": "/run/user/%d" % pw.pw_uid, "HOME": pw.pw_dir}
    if os.geteuid() == 0 and pw.pw_uid != 0:
        cmd = ["/usr/bin/runuser", "-u", user, "--", "/usr/bin/env"] + ["%s=%s" % kv for kv in env.items()] + cmd
    return cmd, env


# -- test sounds: lists of per-channel sample functions, rendered to s16 interleaved --

def _render(seconds, channels, voices):
    """voices: [(channel, fn(t) -> -1..1)]; 5 ms fade in/out on everything."""
    n = int(seconds * RATE)
    out = array.array("h", bytes(2 * n * channels))
    fade = int(0.005 * RATE)
    for ch, fn in voices:
        if ch >= channels:
            continue
        for i in range(n):
            g = min(1.0, i / fade, (n - 1 - i) / fade)
            out[i * channels + ch] = int(max(-1.0, min(1.0, fn(i / RATE) * g)) * 32767)
    return out.tobytes()


def _tone(freq, amp):
    return lambda t: amp * math.sin(2 * math.pi * freq * t)


def _sweep(f0, f1, seconds, amp):
    k = (f1 - f0) / seconds
    return lambda t: amp * math.sin(2 * math.pi * (f0 * t + k * t * t / 2))


def _pulses(freq, amp, on, period):
    return lambda t: amp * math.sin(2 * math.pi * freq * t) if (t % period) < on else 0.0


def make_sound(name, channels):
    """
    channels 4: the controller's own device (1-2 speaker/headphones, 3-4 haptic actuators).
    channels 2: the Deck's output ("all Deck audio" mode): haptics come from the sound itself.
    """
    hl, hr = (2, 3) if channels == 4 else (0, 1)
    if name == "tone":
        return _render(1.0, channels, [(0, _tone(440, 0.3)), (1, _tone(440, 0.3))])
    if name == "left":
        return _render(0.8, channels, [(0, _tone(660, 0.3))])
    if name == "right":
        return _render(0.8, channels, [(1, _tone(660, 0.3))])
    if name == "sweep":
        return _render(2.5, channels, [(0, _sweep(150, 6000, 2.5, 0.25)), (1, _sweep(150, 6000, 2.5, 0.25))])
    if name == "haptic_left":
        return _render(0.9, channels, [(hl, _pulses(160, 0.9, 0.12, 0.3))])
    if name == "haptic_right":
        return _render(0.9, channels, [(hr, _pulses(160, 0.9, 0.12, 0.3))])
    if name == "haptic_sweep":
        return _render(2.0, channels, [(hl, _sweep(40, 400, 2.0, 0.9)), (hr, _sweep(40, 400, 2.0, 0.9))])
    if name == "click":
        return _render(0.04, channels, [(0, _pulses(2000, 0.5, 0.006, 1)), (1, _pulses(2000, 0.5, 0.006, 1)),
                                        (hl, _tone(170, 1.0)), (hr, _tone(170, 1.0))])
    raise ValueError("unknown sound " + name)


class Player(threading.Thread):
    """
    One playback stream into the controller's audio device for as long as the page is open
    (like a game keeps its stream), silence between sounds; written just ahead of time so
    a sound starts with little delay.
    """

    def __init__(self, user, sink, channels, logger):
        super().__init__(daemon=True, name="ds-tester-player")
        self.user = user
        self.sink = sink
        self.channels = channels
        self.log = logger
        self.error = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._sound = b""
        self._pos = 0
        self._proc = None
        self._cache = {}

    def play(self, name_or_pcm):
        if isinstance(name_or_pcm, str):
            if name_or_pcm not in self._cache:
                self._cache[name_or_pcm] = make_sound(name_or_pcm, self.channels)
            pcm = self._cache[name_or_pcm]
        else:
            pcm = name_or_pcm
        with self._lock:
            self._sound, self._pos = pcm, 0

    def playing(self):
        with self._lock:
            return self._pos < len(self._sound)

    def stop(self):
        self._stop_event.set()

    def run(self):
        cmd = ["pw-play", "--raw", "--rate", str(RATE), "--channels", str(self.channels), "--format", "s16",
               "--latency", "10ms", "-P", "{ node.name = decky-dualsense-tester }"]
        if self.sink:
            cmd += ["--target", self.sink]
        if self.channels == 4:
            cmd += ["--channel-map", "front-left,front-right,rear-left,rear-right"]
        cmd += ["-"]
        cmd, env = _user_command(self.user, cmd)
        try:
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        except OSError as e:
            self.error = "pw-play: %s" % e
            return
        frame_bytes = FRAME * self.channels * 2
        silence = bytes(frame_bytes)
        written = time.monotonic()
        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                if written < now:
                    written = now          # fell behind (or just started): no catching up
                if written - now > PLAY_AHEAD:
                    time.sleep(written - now - PLAY_AHEAD)
                    continue
                with self._lock:
                    chunk = self._sound[self._pos:self._pos + frame_bytes]
                    self._pos += len(chunk)
                self._proc.stdin.write(chunk.ljust(frame_bytes, b"\0") if chunk else silence)
                self._proc.stdin.flush()
                written += FRAME / RATE
        except (OSError, ValueError) as e:
            self.error = "pw-play: %s" % e
        finally:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()


class MicMonitor(threading.Thread):
    """Records the controller microphone source: level meter, and a short recording."""

    def __init__(self, user, source, logger):
        super().__init__(daemon=True, name="ds-tester-mic")
        self.user = user
        self.source = source
        self.log = logger
        self.error = None
        self.level = 0.0          # peak of the last 50 ms, 0..1
        self.recorded = None      # finished recording, mono s16
        self.recording = False
        self._buffer = bytearray()
        self._stop_event = threading.Event()
        self._proc = None

    def record(self):
        self._buffer = bytearray()
        self.recorded = None
        self.recording = True

    def stop(self):
        self._stop_event.set()
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()

    def run(self):
        cmd, env = _user_command(self.user, ["pw-record", "--raw", "--rate", str(RATE), "--channels", "1",
                                             "--format", "s16", "--latency", "20ms", "--target", self.source,
                                             "-P", "{ node.name = decky-dualsense-tester-mic }", "-"])
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        except OSError as e:
            self.error = "pw-record: %s" % e
            return
        chunk_bytes = RATE // 20 * 2
        out = self._proc.stdout
        while not self._stop_event.is_set():
            chunk = out.read(chunk_bytes)
            if not chunk:
                break
            samples = array.array("h", chunk[:len(chunk) // 2 * 2])
            self.level = max((abs(s) for s in samples), default=0) / 32768
            if self.recording:
                self._buffer += chunk
                if len(self._buffer) >= MIC_RECORD_SECONDS * RATE * 2:
                    self.recorded = bytes(self._buffer)
                    self.recording = False
        if self._proc.poll() is None:
            self._proc.terminate()
        self._proc.wait()


class Tester:
    """The test page's session: one controller, opened like a game opens it."""

    def __init__(self, manager, logger, user="deck"):
        self.manager = manager
        self.log = logger
        self.user = user
        self._lock = threading.Lock()
        self._thread = None
        self._stop_event = threading.Event()
        self._last_poll = 0.0
        self._fd = -1
        self._target = None
        self._wanted_mac = None      # controller picked on the page (None: the first one)
        self._input = None
        self._rate = 0
        self._reports = 0
        self._player = None
        self._mic = None
        self._rumble_until = 0.0
        self._click_mode = False
        self._cross = False
        self._circle_since = None
        self._capture = False
        self._exit_capture = False
        self._play_back_pending = False
        self._play_back_until = 0.0
        self._next_mic_check = 0.0
        self._switch = False
        self.error = None

    # -- session --

    def start(self):
        with self._lock:
            self._last_poll = time.monotonic()
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="ds-tester")
            self._thread.start()
        self.log.info("Controller test page opened")

    def stop(self):
        self._stop_event.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3)

    def select(self, mac):
        """Work with another controller (the page lists them when there are several)."""
        self._wanted_mac = mac or None
        target = self._target
        if target and target["bridge"].controller.get("mac") != self._wanted_mac:
            self._switch = True

    def set_capture(self, on):
        self._capture = bool(on)
        self._exit_capture = False
        self._circle_since = None

    def set_click_mode(self, on):
        self._click_mode = bool(on)
        if on:
            self._ensure_player()

    def state(self):
        self._last_poll = time.monotonic()
        target = self._target
        player, mic = self._player, self._mic
        audio = self.manager.audio
        hold = 0.0
        if self._circle_since is not None:
            hold = min(1.0, (time.monotonic() - self._circle_since) / HOLD_TO_EXIT)
        result = {
            "running": bool(self._thread and self._thread.is_alive()),
            "error": self.error,
            "controller": None,
            "input": self._input,
            "rate": self._rate,
            "capture": self._capture,
            "exit_capture": self._exit_capture,
            "exit_hold": hold,
            "click_mode": self._click_mode,
            "audio": {
                "mode": "deck" if audio.get("enabled") else "game" if audio.get("game_audio") else "off",
                "playing": bool(player and player.playing()),
                "error": player.error if player else None,
            },
            "mic": {
                "enabled": bool(audio.get("mic")),
                # The host keeps the mute state (the controller only reports the button).
                "muted": bool(target and target["streamer"] and target["streamer"]._mic_muted),
                "source": None,
                "level": mic.level if mic else 0.0,
                "recording": bool(mic and mic.recording),
                "playing_back": time.monotonic() < self._play_back_until,
                "frames": 0,
                "error": mic.error if mic else None,
            },
        }
        result["controllers"] = [
            {"mac": t["bridge"].controller.get("mac", ""), "name": t["bridge"].controller["name"]}
            for t in self.manager.tester_targets()]
        if target:
            bridge = target["bridge"]
            result["controller"] = {"name": bridge.controller["name"], "mode": bridge.mode,
                                    "mac": bridge.controller.get("mac", ""),
                                    "virtual_hidraw": bridge.virtual_hidraw, "edge": bridge.controller.get("pid") == 0x0DF2}
            source = getattr(bridge, "_mic", None)
            if source:
                result["mic"]["source"] = source.name
                result["mic"]["frames"] = source.frames
        return result

    # -- outputs (as a game sends them: USB output report 0x02 to the virtual controller) --

    def _write_output(self, common):
        report = bytearray(DS_OUTPUT_REPORT_USB_SIZE)
        report[0] = DS_OUTPUT_REPORT_USB
        report[1:1 + len(common)] = common
        fd = self._fd
        if fd < 0:
            raise OSError("controller is not open")
        os.write(fd, bytes(report))

    def rumble(self, left, right):
        common = bytearray(47)
        common[0] = 0x01 | 0x02          # compatible vibration, rumble instead of haptics
        common[2] = max(0, min(255, int(right)))
        common[3] = max(0, min(255, int(left)))
        self._write_output(common)
        self._rumble_until = time.monotonic() + RUMBLE_SECONDS if (left or right) else 0.0

    def trigger(self, which, effect):
        params = TRIGGER_EFFECTS[effect]
        common = bytearray(47)
        for side, flag, offset in (("right", 0x04, 10), ("left", 0x08, 21)):
            if which in (side, "both"):
                common[0] |= flag
                common[offset:offset + len(params)] = bytes(params)
        self._write_output(common)

    def lightbar(self, color):
        r, g, b = LIGHTBAR[color]
        common = bytearray(47)
        common[1] = 0x04
        common[44:47] = bytes((r, g, b))
        self._write_output(common)

    def player_leds(self, mask):
        common = bytearray(47)
        common[1] = 0x10
        common[43] = int(mask) & 0x1F
        self._write_output(common)

    def mute_led(self, mode):
        common = bytearray(47)
        common[1] = 0x01
        common[8] = {"off": 0, "on": 1, "pulse": 2}[mode]
        self._write_output(common)

    def reset_outputs(self):
        common = bytearray(47)
        common[0] = 0x01 | 0x02 | 0x04 | 0x08
        common[10] = common[21] = 0x05
        self._write_output(common)

    # -- sound --

    def _sink(self):
        """(sink or None for the default output, channels) for the current audio mode."""
        audio = self.manager.audio
        target = self._target
        if audio.get("enabled"):
            return None, 2
        sink = getattr(target["bridge"], "sink_name", None) if target else None
        if audio.get("game_audio") and sink and getattr(target["bridge"], "sink_ready", False):
            return sink, 4
        raise RuntimeError("no controller audio device (turn on game audio in the plugin panel)")

    def _ensure_player(self):
        sink, channels = self._sink()
        player = self._player
        if player and player.is_alive() and (player.sink, player.channels) == (sink, channels):
            return player
        if player:
            player.stop()
        player = Player(self.user, sink, channels, self.log)
        player.start()
        self._player = player
        return player

    def play(self, name):
        self._ensure_player().play(name)

    def mic_record(self):
        mic = self._ensure_mic()
        self._ensure_player()
        mic.record()
        self._play_back_pending = True

    def _ensure_mic(self):
        target = self._target
        source = getattr(target["bridge"], "_mic", None) if target else None
        if not source:
            raise RuntimeError("the controller microphone is off")
        mic = self._mic
        if mic and mic.is_alive() and mic.source == source.name:
            return mic
        if mic:
            mic.stop()
        mic = MicMonitor(self.user, source.name, self.log)
        mic.start()
        self._mic = mic
        return mic

    def _play_back(self):
        mic, player = self._mic, self._player
        if not (self._play_back_pending and mic and mic.recorded and player):
            return
        self._play_back_pending = False
        mono = array.array("h", mic.recorded)
        channels = player.channels
        out = array.array("h", bytes(2 * len(mono) * channels))
        for i, s in enumerate(mono):
            out[i * channels] = out[i * channels + 1] = s
        player.play(out.tobytes())
        self._play_back_until = time.monotonic() + len(mono) / RATE + 0.2

    # -- main loop --

    def _find_target(self):
        target = self.manager.tester_target(self._wanted_mac)
        if not target:
            return None
        node = target["bridge"].virtual_hidraw
        try:
            self._fd = os.open("/dev/" + node, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as e:
            self.error = "/dev/%s: %s" % (node, e)
            return None
        self.error = None
        self.log.info("Controller test: %s on %s", target["bridge"].controller["name"], node)
        return target

    def _close_target(self):
        if self._fd >= 0:
            try:
                self.reset_outputs()
            except OSError:
                pass
            os.close(self._fd)
            self._fd = -1
        self._target = None
        self._input = None

    def _handle_input(self, report):
        state = parse_input(report)
        self._input = state
        self._reports += 1
        b = state["buttons"]
        if self._click_mode and b["cross"] and not self._cross and self._player:
            self._player.play("click")
        self._cross = b["cross"]
        if self._capture and b["circle"]:
            if self._circle_since is None:
                self._circle_since = time.monotonic()
            elif time.monotonic() - self._circle_since >= HOLD_TO_EXIT:
                self._capture = False
                self._exit_capture = True
                self._circle_since = None
        else:
            self._circle_since = None

    def _run(self):
        rate_since = time.monotonic()
        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                if now - self._last_poll > IDLE_TIMEOUT:
                    self.log.info("Controller test page closed")
                    break
                if self._switch:
                    # Another controller picked: its own audio device and microphone too.
                    self._switch = False
                    self._close_target()
                    for worker in (self._player, self._mic):
                        if worker:
                            worker.stop()
                    self._player = self._mic = None
                    self._click_mode = False
                if self._target is None:
                    self._target = self._find_target()
                    if self._target is None:
                        self._stop_event.wait(0.5)
                        continue
                    rate_since, self._reports = now, 0
                bridge = self._target["bridge"]
                if not bridge.is_alive() or bridge.virtual_hidraw is None:
                    self._close_target()
                    continue
                try:
                    ready = select.select([self._fd], [], [], 0.05)[0]
                    while ready:
                        report = os.read(self._fd, 128)
                        if len(report) >= 64 and report[0] == DS_INPUT_REPORT_USB:
                            self._handle_input(report)
                        ready = select.select([self._fd], [], [], 0)[0]
                except BlockingIOError:
                    pass
                except OSError as e:
                    self.log.info("Controller test: %s", e)
                    self._close_target()
                    continue
                if now - rate_since >= 1.0:
                    self._rate = round(self._reports / (now - rate_since))
                    rate_since, self._reports = now, 0
                if self._rumble_until and now >= self._rumble_until:
                    try:
                        self.rumble(0, 0)
                    except OSError:
                        pass
                self._play_back()
                if now >= self._next_mic_check:
                    # Level meter: listen to the microphone source while the page is open.
                    self._next_mic_check = now + 1.0
                    try:
                        self._ensure_mic()
                    except RuntimeError:
                        pass
        except Exception as e:  # noqa: BLE001 - shown on the page
            self.error = str(e)
            self.log.exception("Controller test failed")
        finally:
            self._close_target()
            for worker in (self._player, self._mic):
                if worker:
                    worker.stop()
            self._player = self._mic = None
            self._click_mode = self._capture = False
            self._rate = 0
