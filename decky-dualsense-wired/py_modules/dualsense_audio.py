"""
DualSense audio and haptics over Bluetooth: extended output reports 0x32..0x39.

Protocol (see research/FINDINGS.md; derived from captures of the user's own
controller and public research, no third-party code):

  report    = [id][seq << 4][sub-packet]...[zero padding][CRC32 LE, seed 0xA2]
  sub-packet = [0x80 | 0x40 if two blocks | type][block length][block x 1 or 2]

  type 0x10, 63 bytes: settings block ("common" output fields)
  type 0x11,  7 bytes: stream header "fe 30 30 30 30 30" + 8-bit counter
  type 0x12, 64 bytes: haptics, 32 stereo frames of signed 8-bit (~3 kHz)
  type 0x13, 200 bytes: one Opus frame (48 kHz stereo, 10 ms, CELT), zero padded

Test usage (root, controller connected over Bluetooth):
  python3 dualsense_audio.py haptics --freq 80 --seconds 3
  python3 dualsense_audio.py tone --freq 440 --seconds 3
  python3 dualsense_audio.py info
"""

import argparse
import collections
import ctypes
import glob
import math
import os
import struct
import subprocess
import sys
import threading
import time
import zlib

try:
    import fcntl

    def _ioctl(fd, request, buf):
        return fcntl.ioctl(fd, request, buf, True)
except ImportError:  # Decky's bundled Python may lack fcntl
    def _ioctl(fd, request, buf):
        libc = ctypes.CDLL(None, use_errno=True)
        cbuf = (ctypes.c_char * len(buf)).from_buffer(buf)
        res = libc.ioctl(fd, ctypes.c_ulong(request), cbuf)
        if res < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        return res


SONY_VID = 0x054C
DUALSENSE_PIDS = (0x0CE6, 0x0DF2)
BUS_BLUETOOTH = 0x05

# Report sizes from the Bluetooth report descriptor (match the captures).
REPORT_SIZES = {0x31: 78, 0x32: 142, 0x33: 206, 0x34: 270, 0x35: 334, 0x36: 398,
                0x37: 462, 0x38: 526, 0x39: 547}

SUB_SETTINGS = 0x10
SUB_STREAM = 0x11
SUB_HAPTICS = 0x12
SUB_OPUS = 0x13
SUB_OPUS_HEADPHONES = 0x16     # same Opus frame, played on the 3.5 mm jack (as in DS5Dongle)
INPUT_PLUG_OFFSET = 55         # Bluetooth input report 0x31: bit 0 = headphones plugged in
SUB_PRESENT = 0x80
SUB_TWO_BLOCKS = 0x40

SETTINGS_SIZE = 63
HAPTICS_BLOCK = 64          # 32 stereo frames, int8
HAPTICS_FRAMES = HAPTICS_BLOCK // 2
HAPTICS_RATE = 3000
OPUS_FRAME_BYTES = 200
OPUS_RATE = 48000
OPUS_FRAME_SAMPLES = 480    # 10 ms
OPUS_SILENCE = bytes([0xF4, 0xFF, 0xFE])  # as sent by the controller host when silent

OUTPUT_CRC_SEED = 0xA2
FEATURE_SET_CRC_SEED = 0x53


def _crc32(seed, data):
    return zlib.crc32(bytes([seed]) + bytes(data)) & 0xFFFFFFFF


# valid_flag0 bits for the audio fields only (headset volume, speaker volume, audio control),
# so a stream does not override lightbar/triggers/rumble set by a game.
AUDIO_ONLY_FLAGS = (0xB0, 0x00, 0x00)
DSX_FLAGS = (0xFD, 0xF7, 0x07)
# Flags DSX never sets while streaming: flag0 0x02 (rumble instead of haptics) and
# flag1 0x08 (release LEDs). Game output merged into the stream has them cleared.
STREAM_BLOCKED_FLAG0 = 0x02
STREAM_BLOCKED_FLAG1 = 0x08


def settings_block(speaker_volume=0x7F, headset_volume=0x7F, lightbar=(0x00, 0x00, 0xFF), player_leds=0x00,
                   flags=DSX_FLAGS):
    """
    63-byte settings sub-packet. Layout follows the "common" output report of
    hid-playstation.c; the values are those seen in captures while audio streams.
    flags: (valid_flag0, valid_flag1, valid_flag2) — which fields the controller applies.
    """
    b = bytearray(SETTINGS_SIZE)
    b[0] = flags[0]             # DSX: everything except "use rumble, not haptics"
    b[1] = flags[1]             # DSX: everything except "release LEDs"
    b[2] = b[3] = 0             # rumble motors (unused with haptics)
    b[4] = headset_volume
    b[5] = speaker_volume
    b[6] = 0xFF                 # microphone volume (as observed)
    b[7] = 0x09                 # audio control (as observed)
    b[9] = 0x0F                 # power save control (as observed)
    # b[10], b[21]: trigger effect types stay 0, as in the steady stream
    b[37] = 0x0A                # unknown, constant in captures
    b[38] = flags[2]            # valid_flag2
    b[41] = 0x02                # lightbar setup (as observed)
    b[43] = player_leds
    b[44], b[45], b[46] = lightbar
    return bytes(b)


def build_report(report_id, seq, subpackets):
    """subpackets: list of (type, [block, ...]) with one or two equally sized blocks."""
    size = REPORT_SIZES[report_id]
    out = bytearray(size)
    out[0] = report_id
    out[1] = (seq & 0x0F) << 4
    off = 2
    for sub_type, blocks in subpackets:
        length = len(blocks[0])
        header = SUB_PRESENT | sub_type | (SUB_TWO_BLOCKS if len(blocks) == 2 else 0)
        data = b"".join(blocks)
        if off + 2 + len(data) > size - 4:
            raise ValueError("sub-packets do not fit into report 0x%02x" % report_id)
        out[off] = header
        out[off + 1] = length
        out[off + 2:off + 2 + len(data)] = data
        off += 2 + len(data)
    struct.pack_into("<I", out, size - 4, _crc32(OUTPUT_CRC_SEED, out[:size - 4]))
    return bytes(out)


LATENCY_MIN, LATENCY_MAX = 16, 64     # ms; the controller drops its whole buffer at ~64+


STREAM_FLAG_MIC = 0x01         # stream header flag: the controller sends its microphone


def is_mic_report(report):
    """Bluetooth input report 0x31 carrying a microphone Opus frame instead of controller state."""
    return len(report) > 2 and report[0] == 0x31 and bool(report[1] & 0x02)


INPUT_MUTE_BUTTON = (11, 0x04)   # Bluetooth input report 0x31: the mute button


def mic_mute_state(muted):
    """
    Common output part that (un)mutes the controller microphone and sets the mute button LED.
    Over a cable the host does this when the mute button is pressed: the controller only reports
    the press. Also sets the microphone volume and automatic mic selection (headset mic first).
    """
    common = bytearray(47)
    common[0] = 0x40 | 0x80          # microphone volume, audio control
    common[1] = 0x01 | 0x02          # mute LED, mute control
    common[6] = 0x40                 # microphone volume (the controller tops out at ~64)
    common[7] = 0x08                 # audio control: mic auto select, noise cancelling
    common[8] = 0x01 if muted else 0x00   # mute LED on / off
    common[9] = 0x10 if muted else 0x00   # mute control: microphone muted, no power saving
    return bytes(common)


def mute_led_state(on):
    """Mute button LED only (turned off with the microphone)."""
    common = bytearray(47)
    common[1] = 0x01
    common[8] = 0x01 if on else 0x00
    return bytes(common)


def mic_status_report(seq, enabled):
    """Turns the controller microphone on/off without audio (a header-only 0x32 report)."""
    return build_report(0x32, seq, [(SUB_STREAM, [bytes([0x03 if enabled else 0x02])])])


def stream_header(counter, buffer_ms=48, mic=False):
    """
    0x11 block: flags, then the controller's audio buffer length in ms (DSX sends 0x30 = 48;
    the fields were identified in the open DS5Dongle project), then a packet counter. The
    controller keeps this much audio queued: less means less delay, more means fewer gaps.
    """
    b = max(LATENCY_MIN, min(LATENCY_MAX, int(buffer_ms)))
    flags = 0xFE | (STREAM_FLAG_MIC if mic else 0)
    return bytes([flags, b, b, b, b, b, counter & 0xFF])


def haptics_block(samples):
    """samples: 32 (left, right) pairs in -1.0..1.0 -> 64 bytes signed 8-bit interleaved."""
    out = bytearray()
    for left, right in samples:
        for v in (left, right):
            out.append(max(-128, min(127, int(round(v * 127)))) & 0xFF)
    return bytes(out)


def pad_opus(frame):
    if len(frame) > OPUS_FRAME_BYTES:
        raise ValueError("Opus frame too large: %d" % len(frame))
    return frame + bytes(OPUS_FRAME_BYTES - len(frame))


class OpusEncoder:
    """Minimal libopus binding: 48 kHz stereo, CELT-only, 10 ms frames, CBR."""

    OPUS_APPLICATION_RESTRICTED_LOWDELAY = 2051
    OPUS_SET_BITRATE = 4002
    OPUS_SET_VBR = 4006
    OPUS_RESET_STATE = 4028

    def __init__(self, bitrate=160000):
        self.lib = ctypes.CDLL("libopus.so.0")
        self.lib.opus_encoder_create.restype = ctypes.c_void_p
        self.lib.opus_encode.restype = ctypes.c_int32
        err = ctypes.c_int()
        self.enc = self.lib.opus_encoder_create(OPUS_RATE, 2, self.OPUS_APPLICATION_RESTRICTED_LOWDELAY,
                                                ctypes.byref(err))
        if err.value != 0 or not self.enc:
            raise OSError("opus_encoder_create failed: %d" % err.value)
        self.lib.opus_encoder_ctl(ctypes.c_void_p(self.enc), self.OPUS_SET_BITRATE, ctypes.c_int(bitrate))
        self.lib.opus_encoder_ctl(ctypes.c_void_p(self.enc), self.OPUS_SET_VBR, ctypes.c_int(0))
        self._out = (ctypes.c_ubyte * 1500)()

    def encode(self, pcm):
        """pcm: 960 interleaved int16 values (480 stereo frames)."""
        buf = (ctypes.c_int16 * len(pcm))(*pcm)
        n = self.lib.opus_encode(ctypes.c_void_p(self.enc), buf, OPUS_FRAME_SAMPLES, self._out, len(self._out))
        if n < 0:
            raise OSError("opus_encode failed: %d" % n)
        return bytes(self._out[:n])

    def encode_float(self, pcm):
        """pcm: array('f') of 960 interleaved floats (480 stereo frames), nominally -1..1."""
        self.lib.opus_encode_float.restype = ctypes.c_int32
        buf = (ctypes.c_float * len(pcm)).from_buffer(pcm)
        n = self.lib.opus_encode_float(ctypes.c_void_p(self.enc), buf, OPUS_FRAME_SAMPLES, self._out, len(self._out))
        if n < 0:
            raise OSError("opus_encode failed: %d" % n)
        return bytes(self._out[:n])

    def reset(self):
        """Start over as for a new stream (the controller's decoder starts fresh too)."""
        self.lib.opus_encoder_ctl(ctypes.c_void_p(self.enc), self.OPUS_RESET_STATE)

    def encode_raw(self, raw):
        """raw: 1920 bytes of s16le interleaved stereo (480 frames), as read from pw-record."""
        buf = (ctypes.c_int16 * (len(raw) // 2)).from_buffer_copy(raw)
        n = self.lib.opus_encode(ctypes.c_void_p(self.enc), buf, OPUS_FRAME_SAMPLES, self._out, len(self._out))
        if n < 0:
            raise OSError("opus_encode failed: %d" % n)
        return bytes(self._out[:n])

    def __del__(self):
        if getattr(self, "enc", None):
            self.lib.opus_encoder_destroy(ctypes.c_void_p(self.enc))


class OpusDecoder:
    """Minimal libopus decoder binding (the controller microphone: 48 kHz mono, 10 ms frames)."""

    def __init__(self, rate=48000, channels=1, max_samples=960):
        self.lib = ctypes.CDLL("libopus.so.0")
        self.lib.opus_decoder_create.restype = ctypes.c_void_p
        self.lib.opus_decode.restype = ctypes.c_int32
        err = ctypes.c_int()
        self.dec = self.lib.opus_decoder_create(rate, channels, ctypes.byref(err))
        if err.value != 0 or not self.dec:
            raise OSError("opus_decoder_create failed: %d" % err.value)
        self.channels = channels
        self.max_samples = max_samples
        self._out = (ctypes.c_int16 * (max_samples * channels))()

    def decode(self, packet):
        """One Opus packet -> s16le PCM bytes."""
        buf = (ctypes.c_ubyte * len(packet)).from_buffer_copy(packet)
        n = self.lib.opus_decode(ctypes.c_void_p(self.dec), buf, len(packet), self._out, self.max_samples, 0)
        if n < 0:
            raise OSError("opus_decode failed: %d" % n)
        return bytes(self._out)[:n * self.channels * 2]

    def __del__(self):
        if getattr(self, "dec", None):
            self.lib.opus_decoder_destroy(ctypes.c_void_p(self.dec))


# --- device access -----------------------------------------------------------------

def _hid_ioc(nr, size):
    return (3 << 30) | (size << 16) | (ord("H") << 8) | nr


def find_bt_hidraw():
    for sys_path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(sys_path, "device", "uevent")) as f:
                info = dict(line.strip().split("=", 1) for line in f if "=" in line)
            bus, vid, pid = (int(x, 16) for x in info.get("HID_ID", "").split(":"))
        except (OSError, ValueError):
            continue
        if bus == BUS_BLUETOOTH and vid == SONY_VID and pid in DUALSENSE_PIDS:
            return "/dev/" + os.path.basename(sys_path)
    return None


def get_feature(fd, report_id, size):
    buf = bytearray(size)
    buf[0] = report_id
    n = _ioctl(fd, _hid_ioc(0x07, size), buf)
    return bytes(buf[:n])


def set_feature(fd, report):
    buf = bytearray(report)
    struct.pack_into("<I", buf, len(buf) - 4, _crc32(FEATURE_SET_CRC_SEED, buf[:-4]))
    _ioctl(fd, _hid_ioc(0x06, len(buf)), buf)


def command(fd, group, cmd, args=b""):
    """Service command 0x80 -> reply 0x81 (64-byte feature reports)."""
    req = bytearray(64)
    req[0], req[1], req[2] = 0x80, group, cmd
    req[3:3 + len(args)] = args
    set_feature(fd, req)
    for _ in range(20):
        reply = get_feature(fd, 0x81, 64)
        if reply[1:3] == bytes([group, cmd]) and reply[3] in (0x02, 0x03):
            return reply
        time.sleep(0.01)
    return reply


# --- system audio -> controller stream ---------------------------------------------------

DEFAULT_STREAM_OPTIONS = {
    "speaker": True,        # play Deck audio on the controller speaker / headset jack
    "haptics": True,        # derive haptics from the same audio
    "volume": 80,           # controller speaker/headset volume, 0..100
    "haptics_gain": 100,    # haptics strength, 0..300 %
    "latency": 32,          # audio kept buffered in the controller, ms (DSX uses 48)
    "separate_outputs": True,  # game output (triggers, LEDs) as its own reports, not inside audio ones
    "mic": False,           # controller microphone as a system audio source
    "source": "system",     # "system": what the Deck plays; "controller": GAME_AUDIO_SINK
}

# PipeWire sink that stands for the wired controller's own audio device (4 channels like the
# USB DualSense: 1-2 speaker/headset, 3-4 the two haptic actuators). Games that support the
# controller's speaker and HD haptics play into it; "controller" source streams it as is.
GAME_AUDIO_SINK = "dualsense_gadget"     # name prefix: one sink per controller, "<prefix>_<port>"
# Real haptics signals carry content up to ~1 kHz; only guard against aliasing at 3 kHz.
GAME_HAPTICS_LOWPASS_HZ = 1200.0

# The controller plays one report per 32 haptics frames at 3 kHz = 10.667 ms, i.e. 93.75
# reports/s (DSX sends ~94/s; its buffer byte overflows and resets at 100/s). An Opus frame
# carries 480 samples, so the controller's audio runs at 480 * 93.75 = 45 kHz: capture is
# resampled to 45 kHz by PipeWire and the Opus encoder is simply fed 480-sample frames.
CONTROLLER_AUDIO_RATE = 45000
STREAM_PERIOD = OPUS_FRAME_SAMPLES / CONTROLLER_AUDIO_RATE      # 10.667 ms
PCM_FRAME_BYTES = OPUS_FRAME_SAMPLES * 2 * 4   # 480 stereo float frames
# Capture is float: PipeWire mixes in float and the sink monitor has no volume applied, so
# several sounds together go above full scale. As s16 that was clipped hard (a hoarse sound
# whenever a new sound started over loud music); now a limiter lowers the gain smoothly.
LIMIT_PEAK = 0.97
LIMIT_RELEASE = 0.02           # gain recovery per 10.7 ms frame (~0.5 s from half gain)
HAPTICS_DECIMATION = OPUS_FRAME_SAMPLES // HAPTICS_FRAMES  # 45 kHz / 15 = 3 kHz haptics
HAPTICS_LOWPASS_HZ = 400.0
# PipeWire delivers capture in quantum-sized bursts (512 frames idle, up to 2048 under
# load = 42 ms), so keep ~60 ms queued before playing (DSX recommends 48+ ms too).
STREAM_PREBUFFER = 6
STREAM_MAX_QUEUE = 24
STREAM_STATS_PERIOD = 5.0

# The controller reports how much audio it has buffered: byte 65 of the Bluetooth input
# report 0x31 (BT-only part), 0 when idle and 44..54 while DSX streams with its default
# "Playback Buffer Latency" of 48 ms (capture 02-dsx-start). The stream keeps it near the
# target: two frames in one 0x39 report when low, a skipped frame when high.
INPUT_AUDIO_LEVEL_OFFSET = 65
DEFAULT_TARGET_LEVEL = 32
LEVEL_LOW_MARGIN = 12
LEVEL_HIGH_MARGIN = 12        # the controller drops its whole buffer at ~64+
LEVEL_SMOOTHING = 0.05        # EMA per report
LEVEL_ADJUST_INTERVAL = 20    # reports between corrections (~200 ms)

# Audio reports are only sent while there is something to play (and for STREAM_HOLD after
# it): ~37 KB/s of silence competes with Wi-Fi for the shared radio, and a congested link
# makes BlueZ drop the controller (seen during game launches). Game output still goes out.
STREAM_HOLD = 1.5
STALL_THRESHOLD = 0.2         # a write blocked this long means the link is congested
STALL_BACKOFF = 2.0
RESYNC_LATENESS = 0.1         # behind schedule by more: drop the backlog, don't burst


class AudioStreamer(threading.Thread):
    """
    Capture what the Deck is playing (PipeWire, default output monitor) and stream it to a
    Bluetooth DualSense: Opus audio for the speaker/headset and haptics derived from it,
    one report 0x36 every 10 ms. The settings block only applies audio fields, so lightbar,
    triggers and rumble set by games through the bridge are left alone.
    """

    def __init__(self, hidraw_path, logger, user="deck", options=None):
        super().__init__(daemon=True, name="ds-audio-" + os.path.basename(hidraw_path))
        self.hidraw_path = hidraw_path
        self.log = logger
        self.user = user
        self.options = dict(DEFAULT_STREAM_OPTIONS, **(options or {}))
        # The source is fixed for the life of the stream (it sets the capture format).
        self.game_audio = self.options["source"] == "controller"
        self.channels = 4 if self.game_audio else 2
        self.frame_bytes = OPUS_FRAME_SAMPLES * self.channels * 4
        from dualsense_bridge import OutputState
        # Game output reports (rumble, triggers, LEDs) ride in the stream's settings block,
        # so the controller gets one steady stream: separate reports on top of ~94 audio
        # reports/s overflow BlueZ's socket, and BlueZ then drops the HID device.
        self._game_output = OutputState()
        self._game_audio_control = None
        self._headphones = False
        self._mute_pressed = False
        self._mic_muted = False
        self._mic_state_pending = False
        self._logged_outputs = 0
        self._stop_event = threading.Event()
        self._queue = collections.deque()
        self._queue_lock = threading.Lock()
        self._proc = None
        self.state = "starting"
        self.error = None
        self.stats = {"sent": 0, "underruns": 0, "dropped": 0, "stalls": 0}
        self._backoff_until = 0.0

    def update(self, options):
        self.options = dict(self.options, **options)

    def submit_output(self, common):
        """Queue the common part (47 bytes) of a game output report for the next report."""
        if self.options.get("mic") and common[1] & 0x03:
            # The plugin owns the microphone mute and its LED (the mute button toggles them);
            # hid-playstation on the virtual controller would toggle them a second time.
            common = bytearray(common)
            common[1] &= ~0x03 & 0xFF
        self._game_output.update(common)

    def _settings(self, volume):
        """Settings block: audio fields from the plugin, everything else from the game."""
        block = bytearray(settings_block(speaker_volume=volume, headset_volume=volume, flags=AUDIO_ONLY_FLAGS))
        if self.options.get("separate_outputs"):
            # The audio fields repeat in every audio report: keep the game's audio routing.
            if self._game_audio_control is not None:
                block[7] = self._game_audio_control
            elif self.options.get("mic"):
                block[7] = 0x08     # microphone: automatic selection (a headset mic when plugged in)
            return bytes(block)
        game = self._game_output.take()
        if game:
            masked = (game[0] & STREAM_BLOCKED_FLAG0, game[1] & STREAM_BLOCKED_FLAG1)
            if self._logged_outputs < 5 or masked != (0, 0):
                self._logged_outputs += 1
                self.log.info("Game output into stream: %s%s", game.hex(" "),
                              " (dropped flags %02x %02x)" % masked if masked != (0, 0) else "")
            audio_control = game[7] if game[0] & 0x80 else block[7]
            block[:len(game)] = game
            block[0] = (block[0] & ~STREAM_BLOCKED_FLAG0 & 0xFF) | AUDIO_ONLY_FLAGS[0]
            block[1] &= ~STREAM_BLOCKED_FLAG1 & 0xFF
            block[4] = block[5] = volume
            block[7] = audio_control
        return bytes(block)

    def _mic_controls(self, fd, seq, mic):
        """
        The host owns the microphone mute state, like hid-playstation over a cable: the mute
        button toggles it, the LED shows it. The controller's own status bit (input byte 55
        bit 2) is no use here: with the microphone streaming over Bluetooth it flips ~50 times
        a second, and mirroring it made the LED flicker.
        """
        from dualsense_bridge import common_to_bt
        if self._mic_state_pending:
            self._mic_state_pending = False
            self._write(fd, common_to_bt(mic_mute_state(self._mic_muted) if mic else mute_led_state(False), seq))
            seq += 1
            if mic:
                self.log.info("Controller microphone %s", "muted" if self._mic_muted else "live")
        return seq

    def _opus_block(self):
        return SUB_OPUS_HEADPHONES if self._headphones else SUB_OPUS

    def _send_game_output(self, fd, seq):
        """
        Game output as its own 0x31 report, right after an audio report: inside an audio report
        the controller seems to apply it only when that audio plays, a buffer later. At most one
        per audio report, so the link carries no more than twice the audio report rate.
        Returns the next sequence number.
        """
        if not self.options.get("separate_outputs"):
            return seq
        game = self._game_output.take()
        if not game:
            return seq
        game = bytearray(game)
        if game[0] & 0x80:
            self._game_audio_control = game[7]
        # Same rule as inside the stream: no "rumble instead of haptics", no LED release.
        game[0] &= ~STREAM_BLOCKED_FLAG0 & 0xFF
        game[1] &= ~STREAM_BLOCKED_FLAG1 & 0xFF
        if not (game[0] or game[1] or game[38]):
            return seq
        from dualsense_bridge import common_to_bt
        self._write(fd, common_to_bt(bytes(game), seq))
        return seq + 1

    def stop(self):
        self._stop_event.set()
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()

    def status(self):
        return {"state": self.state, "error": self.error, "source": self.options["source"], **self.stats}

    # -- capture --

    def _capture_command(self):
        import pwd
        pw = pwd.getpwnam(self.user)
        cmd = ["pw-record", "--raw", "--rate", str(CONTROLLER_AUDIO_RATE), "--channels", str(self.channels),
               "--format", "f32", "--quality", "10", "--latency", "20ms",
               "-P", "{ stream.capture.sink = true node.name = decky-dualsense-audio }"]
        if self.game_audio:
            # Monitor of the controller's sink, channels in the device's own order (no remix).
            cmd += ["--target", self.options.get("sink") or GAME_AUDIO_SINK,
                    "--channel-map", "front-left,front-right,rear-left,rear-right"]
        cmd += ["-"]
        env = {"PATH": "/usr/bin:/bin", "XDG_RUNTIME_DIR": "/run/user/%d" % pw.pw_uid, "HOME": pw.pw_dir}
        if os.geteuid() == 0 and pw.pw_uid != 0:
            cmd = ["/usr/bin/runuser", "-u", self.user, "--"] + ["/usr/bin/env"] + \
                  ["%s=%s" % kv for kv in env.items()] + cmd
        return cmd, env

    def _capture_loop(self):
        while not self._stop_event.is_set():
            cmd, env = self._capture_command()
            try:
                self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
            except OSError as e:
                self.error = "pw-record: %s" % e
                self._stop_event.wait(2.0)
                continue
            out = self._proc.stdout
            while not self._stop_event.is_set():
                chunk = out.read(self.frame_bytes)
                if len(chunk) < self.frame_bytes:
                    break
                with self._queue_lock:
                    self._queue.append(chunk)
                    while len(self._queue) > STREAM_MAX_QUEUE:
                        self._queue.popleft()
                        self.stats["dropped"] += 1
            if self._proc.poll() is None:
                self._proc.terminate()
            self._proc.wait()
            if not self._stop_event.is_set():
                self.log.info("Audio capture ended, restarting")
                self._stop_event.wait(1.0)

    # -- haptics --

    def _haptics_from_pcm(self, pcm, state):
        """Decimate 48 kHz stereo to 32 frames, low-pass, apply gain -> 64-byte haptics block."""
        gain = self.options["haptics_gain"] / 100.0
        alpha = state["alpha"]
        out = bytearray(HAPTICS_BLOCK)
        for ch in (0, 1):
            samples = pcm[ch::2]
            y = state["y"][ch]
            for i in range(HAPTICS_FRAMES):
                start = i * HAPTICS_DECIMATION
                x = sum(samples[start:start + HAPTICS_DECIMATION]) / HAPTICS_DECIMATION
                y += alpha * (x - y)
                v = int(y * gain * 127)
                out[2 * i + ch] = max(-128, min(127, v)) & 0xFF
            state["y"][ch] = y
        return bytes(out)

    # -- main loop: one report every 10 ms --

    def _write(self, fd, report):
        started = time.monotonic()
        try:
            os.write(fd, report)
        except BlockingIOError:
            self.stats["dropped"] += 1
        took = time.monotonic() - started
        if took > STALL_THRESHOLD:
            # The Bluetooth link is congested (Wi-Fi shares the radio). BlueZ drops the
            # HID device as soon as its socket buffer overflows, so give it time to drain.
            self._backoff_until = time.monotonic() + STALL_BACKOFF
            self.stats["stalls"] += 1
            self.log.warning("Bluetooth write took %.0f ms, pausing the audio stream for %.0f s",
                             took * 1000, STALL_BACKOFF)

    def _read_level(self, fd, level):
        """Drain pending input reports; return the latest controller audio buffer level."""
        while True:
            try:
                report = os.read(fd, 128)
            except BlockingIOError:
                return level
            if len(report) == 78 and report[0] == 0x31 and not is_mic_report(report):
                level = report[INPUT_AUDIO_LEVEL_OFFSET]
                pressed = bool(report[INPUT_MUTE_BUTTON[0]] & INPUT_MUTE_BUTTON[1])
                if pressed and not self._mute_pressed and self.options.get("mic"):
                    self._mic_muted = not self._mic_muted
                    self._mic_state_pending = True
                self._mute_pressed = pressed
                headphones = bool(report[INPUT_PLUG_OFFSET] & 0x01)
                if headphones != self._headphones:
                    self._headphones = headphones
                    self.log.info("Headphones %s: audio goes to the %s", "plugged in" if headphones else "unplugged",
                                  "3.5 mm jack" if headphones else "controller speaker")

    def run(self):
        import array
        fd = -1
        capture = threading.Thread(target=self._capture_loop, daemon=True, name=self.name + "-capture")
        try:
            fd = os.open(self.hidraw_path, os.O_RDWR | os.O_CLOEXEC | os.O_NONBLOCK)
            encoder = OpusEncoder()
            capture.start()
            cutoff = GAME_HAPTICS_LOWPASS_HZ if self.game_audio else HAPTICS_LOWPASS_HZ
            hstate = {"y": [0.0, 0.0], "alpha": 1.0 - math.exp(-2 * math.pi * cutoff / HAPTICS_RATE)}
            silent_haptics = bytes(HAPTICS_BLOCK)

            def split(raw):
                """4-channel frames -> (speaker stereo, haptics stereo); stereo -> same for both."""
                pcm = array.array("f", raw)
                if self.channels == 2:
                    return pcm, array.array("f", pcm)
                speaker = array.array("f", bytes(OPUS_FRAME_SAMPLES * 8))
                haptics = array.array("f", bytes(OPUS_FRAME_SAMPLES * 8))
                speaker[0::2], speaker[1::2] = pcm[0::4], pcm[1::4]
                haptics[0::2], haptics[1::2] = pcm[2::4], pcm[3::4]
                return speaker, haptics

            limiter = {"gain": 1.0}

            def limit(pcm):
                """Peak limiter: instant attack, slow release; in place."""
                peak = max(max(pcm), -min(pcm))
                prev = limiter["gain"]
                target = min(1.0, LIMIT_PEAK / peak) if peak > 0 else 1.0
                gain = target if target < prev else min(target, prev + LIMIT_RELEASE)
                limiter["gain"] = gain
                if gain < 1.0:
                    n = len(pcm)
                    if gain > prev:      # releasing: ramp to avoid a step
                        for i in range(n):
                            pcm[i] *= prev + (gain - prev) * i / n
                    else:
                        for i in range(n):
                            pcm[i] *= gain

            def encode(raw):
                if raw is None:
                    return pad_opus(OPUS_SILENCE), silent_haptics
                opts = self.options
                speaker, haptics_pcm = split(raw)
                opus = OPUS_SILENCE
                # Silence is encoded too while the stream runs: an encoder that skipped frames
                # continues from stale state when sound comes back, heard as a squeak.
                if opts["speaker"]:
                    limit(speaker)
                    opus = encoder.encode_float(speaker)
                haptics = silent_haptics
                if opts["haptics"] and any(haptics_pcm):
                    haptics = self._haptics_from_pcm(haptics_pcm, hstate)
                return pad_opus(opus), haptics

            period = STREAM_PERIOD
            primed = False
            tick = 0
            seq = 0
            level = None
            smoothed = None
            last_adjust = 0
            levels = []
            counts = collections.Counter()
            start = time.monotonic()
            next_stats = start + STREAM_STATS_PERIOD
            last = dict(self.stats)
            max_late = 0.0
            last_audible = -STREAM_HOLD
            was_idle = True
            mic_sent = False
            now_ok = lambda until: time.monotonic() >= until  # noqa: E731
            last_tick = 0
            qstat = []
            self.state = "active"
            self.log.info("Audio stream to %s started", self.hidraw_path)
            while not self._stop_event.is_set():
                level = self._read_level(fd, level)
                mic = bool(self.options.get("mic"))
                if mic != mic_sent and now_ok(self._backoff_until):
                    # Audio reports carry the flag too; this covers silence (no audio reports).
                    self._write(fd, mic_status_report(seq, mic))
                    seq += 1
                    mic_sent = mic
                    self._mic_muted = False
                    self._mic_state_pending = True
                    self.log.info("Controller microphone %s", "on" if mic else "off")
                if now_ok(self._backoff_until):
                    seq = self._mic_controls(fd, seq, mic)
                if level is not None:
                    levels.append(level)
                    smoothed = level if smoothed is None else smoothed + LEVEL_SMOOTHING * (level - smoothed)
                with self._queue_lock:
                    if not primed and len(self._queue) >= STREAM_PREBUFFER:
                        primed = True
                    raw = self._queue.popleft() if primed and self._queue else None
                    raw2 = None
                    target = self.options["latency"]
                    may_adjust = smoothed is not None and tick - last_adjust >= LEVEL_ADJUST_INTERVAL
                    low = may_adjust and smoothed < target - LEVEL_LOW_MARGIN
                    high = may_adjust and smoothed > target + LEVEL_HIGH_MARGIN
                    # Catch up only while our own queue keeps a reserve.
                    if raw is not None and low and len(self._queue) >= 2:
                        raw2 = self._queue.popleft()
                    queued = len(self._queue)
                if raw is not None:
                    qstat.append(queued)
                now = time.monotonic()
                if (raw is not None and any(raw)) or (raw2 is not None and any(raw2)):
                    last_audible = now
                idle = now - last_audible > STREAM_HOLD or now < self._backoff_until
                if was_idle and not idle:
                    # A new stream after a pause: the encoder and the haptics filter start clean.
                    encoder.reset()
                    hstate["y"] = [0.0, 0.0]
                was_idle = idle
                if primed and raw is None and not idle:
                    self.stats["underruns"] += 1
                    primed = False
                late = now - (start + tick * period)
                max_late = max(max_late, late)
                if late > RESYNC_LATENESS:
                    start = now - tick * period     # after a stall: carry on, don't burst
                if now >= next_stats:
                    if counts["idle"] < tick - last_tick or max_late > RESYNC_LATENESS:
                        # Quiet while idle: one line per period only when audio was sent.
                        self.log.info(
                            "Audio stream: controller buffer %s ms, doubles %d, skipped %d, idle %d, "
                            "underruns +%d, dropped +%d, stalls +%d, queue %s, max lateness %.1f ms",
                            ("%d..%d avg %d" % (min(levels), max(levels), sum(levels) / len(levels)))
                            if levels else "?",
                            counts["double"], counts["skip"], counts["idle"],
                            self.stats["underruns"] - last["underruns"], self.stats["dropped"] - last["dropped"],
                            self.stats["stalls"] - last["stalls"],
                            ("%d..%d avg %.1f" % (min(qstat), max(qstat), sum(qstat) / len(qstat)))
                            if qstat else str(queued), max_late * 1000)
                    qstat = []
                    last_tick = tick
                    last = dict(self.stats)
                    levels = []
                    counts.clear()
                    max_late = 0.0
                    next_stats = now + STREAM_STATS_PERIOD
                if idle:
                    # Nothing to play: only game output (triggers, LEDs) goes to the controller.
                    if now >= self._backoff_until:
                        game = self._game_output.take()
                        if game:
                            from dualsense_bridge import common_to_bt
                            self._write(fd, common_to_bt(game, seq))
                            seq += 1
                    counts["idle"] += 1
                elif raw is not None and high:
                    counts["skip"] += 1           # controller has too much queued: drop this frame
                    last_adjust = tick
                    seq = self._send_game_output(fd, seq)
                elif raw2 is not None:
                    last_adjust = tick
                    (o1, h1), (o2, h2) = encode(raw), encode(raw2)
                    report = build_report(0x39, seq, [(SUB_STREAM, [stream_header(seq, self.options["latency"], self.options.get("mic"))]),
                                                      (self._opus_block(), [o1, o2]), (SUB_HAPTICS, [h1, h2])])
                    self._write(fd, report)
                    counts["double"] += 1
                    seq += 1
                    seq = self._send_game_output(fd, seq)
                else:
                    volume = max(0, min(0x7F, round(self.options["volume"] * 0x7F / 100)))
                    settings = self._settings(volume)
                    opus, haptics = encode(raw)
                    report = build_report(0x36, seq, [
                        (SUB_SETTINGS, [settings]), (SUB_STREAM, [stream_header(seq, self.options["latency"], self.options.get("mic"))]),
                        (self._opus_block(), [opus]), (SUB_HAPTICS, [haptics])])
                    self._write(fd, report)
                    seq += 1
                    seq = self._send_game_output(fd, seq)
                self.stats["sent"] += 1
                tick += 1
                _pace(start, tick, period)
            self.state = "stopped"
        except Exception as e:  # noqa: BLE001 - surface any failure in the UI
            self.state = "error"
            self.error = str(e)
            self.log.exception("Audio stream to %s failed", self.hidraw_path)
        finally:
            self.stop()
            if fd >= 0:
                try:
                    # Leave the controller silent, its microphone off.
                    os.write(fd, build_report(0x32, 0, [(SUB_SETTINGS, [settings_block(flags=AUDIO_ONLY_FLAGS)]),
                                                        (SUB_HAPTICS, [bytes(HAPTICS_BLOCK)])]))
                    if self.options.get("mic"):
                        os.write(fd, mic_status_report(1, False))
                except OSError:
                    pass
                os.close(fd)
            self.log.info("Audio stream to %s stopped", self.hidraw_path)


# --- tests ------------------------------------------------------------------------------

def _pace(start, index, period):
    delay = start + index * period - time.monotonic()
    if delay > 0:
        time.sleep(delay)


def run_haptics(fd, freq, seconds, amplitude, report_id, flags=DSX_FLAGS):
    """Sine wave on both actuators; report 0x32 (haptics only) or 0x36 (with silent Opus)."""
    period = HAPTICS_FRAMES / HAPTICS_RATE
    packets = int(seconds / period)
    settings = settings_block(flags=flags)
    phase = 0.0
    start = time.monotonic()
    for i in range(packets):
        samples = []
        for _ in range(HAPTICS_FRAMES):
            v = amplitude * math.sin(phase)
            samples.append((v, v))
            phase += 2 * math.pi * freq / HAPTICS_RATE
        subs = [(SUB_SETTINGS, [settings])]
        if report_id == 0x36:
            subs += [(SUB_STREAM, [stream_header(i)]), (SUB_OPUS, [pad_opus(OPUS_SILENCE)])]
        subs.append((SUB_HAPTICS, [haptics_block(samples)]))
        os.write(fd, build_report(report_id, i, subs))
        _pace(start, i + 1, period)
    # Leave the actuators silent.
    os.write(fd, build_report(0x32, packets, [(SUB_SETTINGS, [settings]),
                                              (SUB_HAPTICS, [bytes(HAPTICS_BLOCK)])]))


# audio_control byte (settings/common output byte 7), as described for the USB output report:
# bits 0-1 mic select, 2 echo cancel, 3 noise cancel, 4-5 output path
# (0 headset L/R, 1 headset L/L, 2 headset L + speaker R, 3 speaker only), 6-7 input path.
# (label, audio_control, stream header fill byte): the 0x11 stream header sent by DSX is
# fe 30 30 30 30 30 nn; 0x30 has the same "output path 3" bits as audio_control.
ROUTING_VARIANTS = (
    ("header 0x00 (path 0: headset L/R?)", 0x09, 0x00),
    ("header 0x30 (DSX)", 0x09, 0x30),
    ("header 0x10 (path 1?)", 0x09, 0x10),
    ("header 0x20 (path 2?)", 0x09, 0x20),
    ("header 0x00 + audio_control 0x00", 0x00, 0x00),
)


def run_routing(fd, seconds=2.0, gap=1.5, freq=440.0):
    """
    Listening test for the speaker/headset routing: one beep per audio_control value in
    ROUTING_VARIANTS, rising in pitch. Pauses the plugin's bridges meanwhile.
    """
    from dualsense_bridge import PAUSE_FILE
    with open(PAUSE_FILE, "w") as f:
        f.write(str(os.getpid()))
    try:
        time.sleep(3)   # the plugin stops its stream for this controller
        get_feature(fd, 0x05, 41)
        enc = OpusEncoder()
        period = OPUS_FRAME_SAMPLES / OPUS_RATE
        seq = 0
        for n, (label, audio_control, fill) in enumerate(ROUTING_VARIANTS, 1):
            settings = bytearray(settings_block(flags=AUDIO_ONLY_FLAGS))
            settings[7] = audio_control
            tone = freq * (1 + 0.25 * (n - 1))
            print("beep %d: %s, %.0f Hz" % (n, label, tone), flush=True)
            phase = 0.0
            start = time.monotonic()
            for i in range(int(seconds / period)):
                pcm = []
                for _ in range(OPUS_FRAME_SAMPLES):
                    v = int(0.5 * 32767 * math.sin(phase))
                    pcm += (v, v)
                    phase += 2 * math.pi * tone / OPUS_RATE
                header = bytes([0xFE, fill, fill, fill, fill, fill, seq & 0xFF])
                os.write(fd, build_report(0x36, seq, [
                    (SUB_SETTINGS, [bytes(settings)]), (SUB_STREAM, [header]),
                    (SUB_OPUS, [pad_opus(enc.encode(pcm))]), (SUB_HAPTICS, [bytes(HAPTICS_BLOCK)])]))
                seq += 1
                _pace(start, i + 1, period)
            time.sleep(gap)
    finally:
        os.remove(PAUSE_FILE)


def run_tone(fd, freq, seconds, amplitude, speaker_volume, headset_volume, prebuffer=0, double=False,
             flags=DSX_FLAGS):
    """
    Sine tone through the controller speaker/headset jack.
    double=False: report 0x36, one 10 ms Opus frame per report.
    double=True:  report 0x39, two frames (20 ms) per report, as seen once in captures.
    prebuffer: number of reports sent at once before real-time pacing starts.
    """
    enc = OpusEncoder()
    frames_per_report = 2 if double else 1
    period = frames_per_report * OPUS_FRAME_SAMPLES / OPUS_RATE
    packets = int(seconds / period)
    settings = settings_block(speaker_volume=speaker_volume, headset_volume=headset_volume, flags=flags)
    phase = 0.0
    sizes = set()
    write_times = []
    lateness = []

    def next_frame():
        nonlocal phase
        pcm = []
        for _ in range(OPUS_FRAME_SAMPLES):
            v = int(amplitude * 32767 * math.sin(phase))
            pcm += (v, v)
            phase += 2 * math.pi * freq / OPUS_RATE
        frame = enc.encode(pcm)
        sizes.add(len(frame))
        return pad_opus(frame)

    start = None
    for i in range(packets):
        frames = [next_frame() for _ in range(frames_per_report)]
        silent_haptics = [bytes(HAPTICS_BLOCK)] * frames_per_report
        if double:
            subs = [(SUB_STREAM, [stream_header(i)]), (SUB_OPUS, frames), (SUB_HAPTICS, silent_haptics)]
            report = build_report(0x39, i, subs)
        else:
            subs = [(SUB_SETTINGS, [settings]), (SUB_STREAM, [stream_header(i)]),
                    (SUB_OPUS, frames), (SUB_HAPTICS, silent_haptics)]
            report = build_report(0x36, i, subs)
        if i == prebuffer:
            start = time.monotonic() - prebuffer * period
        if start is not None:
            lateness.append(time.monotonic() - (start + i * period))
        t0 = time.monotonic()
        os.write(fd, report)
        write_times.append(time.monotonic() - t0)
        if start is not None:
            _pace(start, i + 1, period)
    os.write(fd, build_report(0x32, packets, [(SUB_SETTINGS, [settings])]))
    ms = lambda v: "%.1f" % (v * 1000)
    print("opus frame sizes: %s, TOC 0x%02x, reports: %d x 0x%02x" % (
        sorted(sizes), frames[0][0], packets, 0x39 if double else 0x36))
    print("write ms: avg %s max %s; lateness ms: avg %s max %s" % (
        ms(sum(write_times) / len(write_times)), ms(max(write_times)),
        ms(sum(lateness) / max(1, len(lateness))), ms(max(lateness or [0]))))


def find_virtual_hidraw():
    for sys_path in glob.glob("/sys/class/hidraw/hidraw*"):
        try:
            with open(os.path.join(sys_path, "device", "uevent")) as f:
                if "HID_PHYS=usb-decky-dualsense-wired" in f.read():
                    return "/dev/" + os.path.basename(sys_path)
        except OSError:
            pass
    return None


USB_BUTTON_OFFSETS = (8, 9, 10)     # USB 0x01: dpad/face, shoulders/sticks, PS/touchpad/mute


def run_buttons(bt_fd, seconds):
    """
    Log every change of the button bytes as sent by the Bluetooth controller and as
    seen on the virtual USB controller (the node Steam reads), side by side.
    """
    import select
    import dualsense_gadget as gadget
    host = gadget.host_side_devices()
    vnode = "/dev/" + host[0]["hidraw"][0] if host and host[0]["hidraw"] else find_virtual_hidraw()
    if not vnode:
        sys.exit("virtual controller not found")
    v_fd = os.open(vnode, os.O_RDONLY | os.O_NONBLOCK)
    print("BT vs %s for %.0f s; buttons = bytes %s of the USB report" % (vnode, seconds, USB_BUTTON_OFFSETS), flush=True)
    last = {"BT  ": None, "virt": None}
    counts = {"BT  ": 0, "virt": 0}
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        for fd in select.select([bt_fd, v_fd], [], [], 0.2)[0]:
            try:
                d = os.read(fd, 128)
            except BlockingIOError:
                continue
            if fd == bt_fd and d[0] == 0x31 and len(d) >= 65:
                name, usb = "BT  ", bytes([0x01]) + d[2:65]
            elif fd == v_fd and d[0] == 0x01 and len(d) >= 64:
                name, usb = "virt", d
            else:
                continue
            counts[name] += 1
            buttons = bytes(usb[i] for i in USB_BUTTON_OFFSETS)
            if buttons != last[name]:
                last[name] = buttons
                print("%7.3f %s buttons %s  sticks %s  triggers %s" % (
                    time.monotonic() - start, name, buttons.hex(" "), usb[1:5].hex(" "), usb[5:7].hex(" ")), flush=True)
    print("reports:", counts)
    os.close(v_fd)


def run_inputcheck(bt_fd, seconds):
    """
    Compare what the bridge delivers with the Bluetooth original: every BT 0x31 input
    report must reach the virtual USB controller as 0x01 + bytes 2..64, unchanged.
    Reports are matched by the 32-bit sensor timestamp.
    """
    import select
    vnode = find_virtual_hidraw()
    if not vnode:
        sys.exit("virtual controller not found (is the bridge running?)")
    v_fd = os.open(vnode, os.O_RDWR | os.O_NONBLOCK)
    bt, virt = {}, {}
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ready, _, _ = select.select([bt_fd, v_fd], [], [], 0.2)
        for fd in ready:
            try:
                d = os.read(fd, 128)
            except BlockingIOError:
                continue
            if fd == bt_fd and d[0] == 0x31 and len(d) >= 65:
                bt[d[2 + 27:2 + 31]] = d[2:65]
            elif fd == v_fd and d[0] == 0x01 and len(d) >= 64:
                virt[d[1 + 27:1 + 31]] = d[1:64]
    common = set(bt) & set(virt)
    diff = [k for k in common if bt[k] != virt[k]]
    print("%s: BT reports %d, virtual %d, matched %d, different %d, missing in virtual %d" % (
        vnode, len(bt), len(virt), len(common), len(diff), len(set(bt) - set(virt))))
    for k in diff[:3]:
        print(" BT  ", bt[k].hex(" "))
        print(" virt", virt[k].hex(" "))
    cal_bt = get_feature(bt_fd, 0x05, 41)
    cal_v = get_feature(v_fd, 0x05, 41)
    print("calibration 0x05 BT     :", cal_bt.hex(" "))
    print("calibration 0x05 virtual:", cal_v.hex(" "))
    print("calibration identical:", cal_bt == cal_v)
    os.close(v_fd)


def run_gadget(bt_fd, seconds, user="deck", padding=3, out_endpoint=False, bt_path=None, audio_options=None):
    """
    Experiment: expose the Bluetooth controller through GadgetBridge (the plugin's USB gadget
    backend) for a while, with the plugin paused, streaming game audio to the controller.
    """
    import logging
    import signal
    from dualsense_bridge import PAUSE_FILE, find_dualsenses
    from dualsense_gadget_bridge import GadgetBridge, can_use_gadget

    def _terminate(signum, frame):
        raise SystemExit(128 + signum)

    # Without this a SIGTERM skips the cleanup below and leaves the real controller hidden.
    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGHUP, _terminate)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
    log = logging.getLogger("gadget")
    bridge = streamer = None
    try:
        # Let the plugin drop its own bridge so games only see this one.
        with open(PAUSE_FILE, "w") as f:
            f.write(str(os.getpid()))
        time.sleep(3)
        controller = next((c for c in find_dualsenses() if can_use_gadget(c)
                           and (not bt_path or "/dev/" + c["hidraw"] == bt_path)), None)
        if not controller:
            sys.exit("No DualSense connected over Bluetooth")
        bridge = GadgetBridge(controller, log, user=user, padding=padding, out_endpoint=out_endpoint)
        bridge.start()
        end = time.monotonic() + seconds
        next_report = time.monotonic() + 5
        while time.monotonic() < end and bridge.is_alive() and not os.path.exists(STOP_FILE):
            if bridge.sink_ready and not streamer and bt_path:
                # Whatever games play into the controller's audio device goes to the controller.
                streamer = AudioStreamer(bt_path, logging.getLogger("game-audio"), user=user,
                                         options=dict(audio_options or {}, source="controller"))
                streamer.start()
            if time.monotonic() >= next_report:
                next_report = time.monotonic() + 5
                log.info("%s %s", bridge.status(), bridge.stats)
            time.sleep(0.2)
    finally:
        if streamer:
            streamer.stop()
            streamer.join(timeout=3)
        if bridge:
            bridge.stop()
            bridge.join(timeout=10)
        try:
            os.remove(PAUSE_FILE)
        except OSError:
            pass
        print("gadget removed")


STOP_FILE = "/run/decky-dualsense-gadget.stop"


def stop_gadget_experiment(user="deck"):
    """
    End a running `gadget` experiment: ask it to stop, and if it does not (older code
    without the stop file), kill it and clean up the gadget, sink and pause flag.
    """
    import signal
    import dualsense_gadget as gadget
    from dualsense_bridge import PAUSE_FILE
    open(STOP_FILE, "w").close()
    me = os.getpid()
    pids = []
    for pid_dir in glob.glob("/proc/[0-9]*"):
        pid = int(os.path.basename(pid_dir))
        if pid == me:
            continue
        try:
            with open(pid_dir + "/cmdline", "rb") as f:
                cmd = f.read().split(b"\0")
        except OSError:
            continue
        if any(c.endswith(b"dualsense_audio.py") for c in cmd) and b"gadget" in cmd:
            pids.append(pid)
    time.sleep(6)
    for pid in pids:
        if os.path.exists("/proc/%d" % pid):
            os.kill(pid, signal.SIGTERM)
            print("killed", pid)
    out = _pactl(user, "list", "short", "modules")
    for line in out.splitlines():
        if "sink_name=dualsense_gadget" in line:
            _pactl(user, "unload-module", line.split()[0])
            print("unloaded sink module", line.split()[0])
    for name in gadget.our_gadgets():
        gadget.destroy(name)
    for path in (PAUSE_FILE, STOP_FILE):
        try:
            os.remove(path)
        except OSError:
            pass
    print("experiment stopped, plugin bridge resumes")


def _pactl(user, *args):
    """Run pactl in the user's audio session (this tool runs as root)."""
    import pwd
    pw = pwd.getpwnam(user)
    env = {"PATH": "/usr/bin:/bin", "XDG_RUNTIME_DIR": "/run/user/%d" % pw.pw_uid}
    cmd = ["/usr/bin/runuser", "-u", user, "--", "/usr/bin/env"] + ["%s=%s" % kv for kv in env.items()] + \
          ["pactl"] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, env=env).stdout


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--device", help="hidraw node (default: first DualSense over Bluetooth)")
    p.add_argument("--audio-flags", action="store_true",
                   help="settings block applies only audio fields (do not touch LEDs/triggers)")
    sub = p.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("haptics")
    h.add_argument("--freq", type=float, default=80.0)
    h.add_argument("--seconds", type=float, default=3.0)
    h.add_argument("--amp", type=float, default=0.8)
    h.add_argument("--report", type=lambda v: int(v, 0), default=0x32, choices=[0x32, 0x36])
    t = sub.add_parser("tone")
    t.add_argument("--freq", type=float, default=440.0)
    t.add_argument("--seconds", type=float, default=3.0)
    t.add_argument("--amp", type=float, default=0.3)
    t.add_argument("--speaker", type=lambda v: int(v, 0), default=0x7F)
    t.add_argument("--headset", type=lambda v: int(v, 0), default=0x7F)
    t.add_argument("--prebuffer", type=int, default=0)
    t.add_argument("--double", action="store_true")
    sub.add_parser("info")
    st = sub.add_parser("stream", help="stream Deck audio to the controller for a while")
    st.add_argument("--seconds", type=float, default=15.0)
    st.add_argument("--volume", type=int, default=80)
    st.add_argument("--gain", type=int, default=100)
    st.add_argument("--no-speaker", action="store_true")
    st.add_argument("--no-haptics", action="store_true")
    st.add_argument("--user", default=os.environ.get("SUDO_USER", "deck"))
    st.add_argument("--source", choices=("system", "controller"), default="system",
                    help="system: what the Deck plays; controller: games' controller audio (%s)" % GAME_AUDIO_SINK)
    gd = sub.add_parser("gadget", help="prototype: virtual composite USB DualSense (HID + audio)")
    gd.add_argument("--seconds", type=float, default=20.0)
    gd.add_argument("--padding", type=int, default=3, help="interfaces before the HID one")
    gd.add_argument("--out-endpoint", action="store_true", help="keep the interrupt OUT endpoint")
    gd.add_argument("--volume", type=int, default=80, help="controller speaker volume for game audio")
    gd.add_argument("--gain", type=int, default=100, help="haptics strength for game audio, %%")
    sub.add_parser("gadget-stop", help="stop a running gadget experiment and clean up")
    lv = sub.add_parser("levels", help="print the controller audio buffer byte from input reports")
    lv.add_argument("--seconds", type=float, default=2.0)
    ic = sub.add_parser("inputcheck")
    ic.add_argument("--seconds", type=float, default=3.0)
    sub.add_parser("routing", help="listening test: one beep per speaker/headset routing value")
    bn = sub.add_parser("buttons", help="log button changes on BT and on the virtual controller")
    bn.add_argument("--seconds", type=float, default=30.0)
    args = p.parse_args()

    if args.cmd == "gadget-stop":
        stop_gadget_experiment(os.environ.get("SUDO_USER", "deck"))
        return
    device = args.device or find_bt_hidraw()
    if not device:
        sys.exit("No DualSense connected over Bluetooth")
    if args.cmd == "stream":
        import logging
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        streamer = AudioStreamer(device, logging.getLogger("stream"), user=args.user, options={
            "speaker": not args.no_speaker, "haptics": not args.no_haptics,
            "volume": args.volume, "haptics_gain": args.gain, "source": args.source})
        streamer.start()
        time.sleep(args.seconds)
        streamer.stop()
        streamer.join(timeout=3)
        print("stream stats:", streamer.status())
        return
    fd = os.open(device, os.O_RDWR)
    try:
        # Make sure the controller uses the full 0x31 input report mode.
        get_feature(fd, 0x05, 41)
        flags = AUDIO_ONLY_FLAGS if args.audio_flags else DSX_FLAGS
        if args.cmd == "haptics":
            run_haptics(fd, args.freq, args.seconds, args.amp, args.report, flags)
        elif args.cmd == "tone":
            run_tone(fd, args.freq, args.seconds, args.amp, args.speaker, args.headset,
                     args.prebuffer, args.double, flags)
        elif args.cmd == "gadget":
            run_gadget(fd, args.seconds, os.environ.get("SUDO_USER", "deck"),
                       padding=args.padding, out_endpoint=args.out_endpoint, bt_path=device,
                       audio_options={"volume": args.volume, "haptics_gain": args.gain})
        elif args.cmd == "levels":
            import select
            seq = []
            end = time.monotonic() + args.seconds
            while time.monotonic() < end:
                if select.select([fd], [], [], 0.2)[0]:
                    r = os.read(fd, 128)
                    if len(r) == 78 and r[0] == 0x31:
                        seq.append((round((time.monotonic() - end + args.seconds) * 1000), r[INPUT_AUDIO_LEVEL_OFFSET],
                                    r[INPUT_AUDIO_LEVEL_OFFSET - 1], r[INPUT_AUDIO_LEVEL_OFFSET + 1]))
            print("reports:", len(seq), "zeros:", sum(1 for s in seq if s[1] == 0))
            print("first 60 (ms, level, prev byte, next byte):", seq[:60])
            zeros = [i for i, s in enumerate(seq) if s[1] == 0]
            for i in zeros[:6]:
                print("around zero:", [(s[0], s[1]) for s in seq[max(0, i - 6):i + 7]])
        elif args.cmd == "inputcheck":
            run_inputcheck(fd, args.seconds)
        elif args.cmd == "buttons":
            run_buttons(fd, args.seconds)
        elif args.cmd == "routing":
            run_routing(fd)
        elif args.cmd == "info":
            for group, cmd in ((0x01, 0x11), (0x01, 0x13), (0x09, 0x02)):
                print("%02x %02x ->" % (group, cmd), command(fd, group, cmd)[:24].hex(" "))
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
