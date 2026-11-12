"""
The controller microphone over Bluetooth as a PipeWire source.

With the microphone flag set in the audio stream header (see dualsense_audio.STREAM_FLAG_MIC),
the controller interleaves extra 0x31 input reports with byte 1 bit 1 set: a 71-byte Opus frame
(48 kHz mono, 10 ms) at offset 3 instead of controller state. The layout was learned from the
open DS5Dongle project (github.com/awalol/DS5Dongle); this is an independent implementation.

The frames are decoded and written into a pipe source with the same device properties as the
controller's sink, so Wine pairs it with the controller like a wired one's microphone.
"""

import os
import pwd
import time

from dualsense_audio import OpusDecoder, _pactl

MIC_OFFSET = 3
MIC_FRAME_BYTES = 71
MIC_RATE = 48000


class MicSource:
    def __init__(self, name, properties, user, logger):
        self.name = name
        self.properties = properties
        self.user = user
        self.log = logger
        uid = pwd.getpwnam(user).pw_uid
        self.fifo = "/run/user/%d/%s.fifo" % (uid, name)
        self._module = None
        self._fd = -1
        self._decoder = None
        self.frames = 0
        self.errors = 0

    def start(self):
        try:
            os.unlink(self.fifo)
        except FileNotFoundError:
            pass
        props = dict(self.properties, **{"device.description": self.properties["device.description"]})
        prop_str = " ".join('%s="%s"' % kv for kv in props.items())
        module = _pactl(self.user, "load-module", "module-pipe-source", "source_name=" + self.name,
                        "file=" + self.fifo, "format=s16le", "rate=%d" % MIC_RATE, "channels=1",
                        "source_properties='%s'" % prop_str).strip()
        if not module.isdigit():
            self.log.warning("Creating the controller microphone source failed: %s", module or "no output")
            return False
        self._module = module
        # PipeWire creates the fifo and keeps it open for reading.
        for _ in range(40):
            try:
                self._fd = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
                break
            except OSError:
                time.sleep(0.05)
        if self._fd < 0:
            self.log.warning("Controller microphone: cannot open %s", self.fifo)
            self.stop()
            return False
        self._decoder = OpusDecoder(MIC_RATE, 1)
        self.log.info("Controller microphone: source %s", self.name)
        return True

    def feed(self, report):
        """A microphone input report from the controller."""
        if self._fd < 0 or len(report) < MIC_OFFSET + MIC_FRAME_BYTES:
            return
        try:
            pcm = self._decoder.decode(bytes(report[MIC_OFFSET:MIC_OFFSET + MIC_FRAME_BYTES]))
            os.write(self._fd, pcm)
            self.frames += 1
        except OSError:
            # EAGAIN: nobody is recording and the pipe is full; a bad frame: skip it.
            self.errors += 1

    def stop(self):
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1
        if self._module:
            _pactl(self.user, "unload-module", self._module)
            self._module = None
        try:
            os.unlink(self.fifo)
        except OSError:
            pass
