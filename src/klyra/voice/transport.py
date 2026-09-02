"""Audio transport abstraction + PyAudio implementation.

The abstract ``AudioTransport`` decouples the voice orchestrator from any
specific audio backend.  The CLI ships with ``PyAudioTransport`` (direct
mic/speaker via PortAudio); a future web UI can plug in a WebSocket or
LiveKit transport without touching orchestrator or tool logic.
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import struct
import sys
import warnings
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

# Gemini Live API audio specs
INPUT_SAMPLE_RATE = 16_000   # Hz — what Gemini expects
OUTPUT_SAMPLE_RATE = 24_000  # Hz — what Gemini sends back
CHANNELS = 1                 # mono
SAMPLE_WIDTH = 2             # 16-bit (2 bytes per sample)
INPUT_CHUNK_FRAMES = 1024    # ~64 ms at 16 kHz
OUTPUT_CHUNK_FRAMES = 1536   # ~64 ms at 24 kHz


class AudioDeviceError(RuntimeError):
    """Raised when no suitable audio device is available."""


class AudioTransport(ABC):
    """Bidirectional audio I/O interface.

    Implementations must be safe to call from an asyncio event loop — blocking
    I/O should be wrapped in an executor.
    """

    @abstractmethod
    async def start(self) -> None:
        """Open audio streams / acquire devices."""

    @abstractmethod
    async def read_chunk(self) -> bytes:
        """Return the next chunk of mic audio (16 kHz, 16-bit PCM, mono).

        Must block (in a thread-safe way) until a chunk is available.
        """

    @abstractmethod
    async def write_chunk(self, data: bytes) -> None:
        """Play a chunk of speaker audio (24 kHz, 16-bit PCM, mono)."""

    @abstractmethod
    async def stop(self) -> None:
        """Release audio streams / devices."""


def _detect_wsl() -> bool:
    """Return True if running inside Windows Subsystem for Linux."""
    if platform.system() != "Linux":
        return False
    try:
        with open("/proc/version", "r") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


_WSL_HELP = """\
WSL detected but no working audio server found.

To enable audio in WSL2, choose one of:

  Option A — WSLg (Windows 11, easiest):
    Your WSL distro should already have audio via PipeWire/PulseAudio
    if WSLg is enabled.  Run `pactl info` to check.  If not working,
    update WSL:  wsl --update

  Option B — Manual PulseAudio forwarding:
    1. sudo apt install pulseaudio-utils
    2. Add to ~/.bashrc:
       export PULSE_SERVER=tcp:$(grep nameserver /etc/resolv.conf | awk '{print $2}')
    3. On Windows, install and run PulseAudio for Windows
       (allow TCP connections on port 4713)
    4. Restart your WSL terminal
"""


class PyAudioTransport(AudioTransport):
    """CLI audio transport using PyAudio (PortAudio wrapper).

    Mic input: 16 kHz, 16-bit, mono (Gemini Live input format).
    Speaker output: 24 kHz, 16-bit, mono (Gemini Live output format).

    Blocking PyAudio reads/writes are run in the default executor so they
    don't block the asyncio event loop.
    """

    def __init__(self) -> None:
        self._pa = None
        self._mic_stream = None
        self._spk_stream = None
        self._loop: asyncio.AbstractEventLoop | None = None
        
        self._mic_actual_rate = INPUT_SAMPLE_RATE
        self._spk_actual_rate = OUTPUT_SAMPLE_RATE
        self._mic_state = None
        self._spk_state = None

    async def start(self) -> None:
        try:
            import pyaudio  # noqa: F811
        except ImportError as exc:
            raise AudioDeviceError(
                "PyAudio is not installed.  Run:\n"
                "  sudo apt install portaudio19-dev   # system library\n"
                "  pip install -e '.[voice]'          # Python package\n"
            ) from exc

        self._loop = asyncio.get_running_loop()
        self._pa = pyaudio.PyAudio()

        # --- Check for audio devices ---
        info = self._pa.get_host_api_info_by_index(0)
        num_devices = info.get("deviceCount", 0)

        has_input = False
        has_output = False
        for i in range(num_devices):
            dev = self._pa.get_device_info_by_host_api_device_index(0, i)
            if dev.get("maxInputChannels", 0) > 0:
                has_input = True
            if dev.get("maxOutputChannels", 0) > 0:
                has_output = True

        if not has_input or not has_output:
            self._pa.terminate()
            msg = "No audio "
            if not has_input and not has_output:
                msg += "input or output devices"
            elif not has_input:
                msg += "input device (microphone)"
            else:
                msg += "output device (speaker)"
            msg += " found."

            if _detect_wsl():
                msg += "\n\n" + _WSL_HELP

            raise AudioDeviceError(msg)

        # --- Open streams ---
        import pyaudio as _pa_mod

        # Try default rates first, then fallback to 48000
        fallback_rate = 48000
        
        try:
            self._mic_stream = self._pa.open(
                format=_pa_mod.paInt16,
                channels=CHANNELS,
                rate=INPUT_SAMPLE_RATE,
                input=True,
                frames_per_buffer=INPUT_CHUNK_FRAMES,
            )
            self._mic_actual_rate = INPUT_SAMPLE_RATE
        except Exception as e:
            logger.warning("Failed to open mic at %dHz, trying %dHz. Error: %s", INPUT_SAMPLE_RATE, fallback_rate, e)
            self._mic_actual_rate = fallback_rate
            frames = int(INPUT_CHUNK_FRAMES * fallback_rate / INPUT_SAMPLE_RATE)
            self._mic_stream = self._pa.open(
                format=_pa_mod.paInt16,
                channels=CHANNELS,
                rate=fallback_rate,
                input=True,
                frames_per_buffer=frames,
            )

        try:
            self._spk_stream = self._pa.open(
                format=_pa_mod.paInt16,
                channels=CHANNELS,
                rate=OUTPUT_SAMPLE_RATE,
                output=True,
                frames_per_buffer=OUTPUT_CHUNK_FRAMES,
            )
            self._spk_actual_rate = OUTPUT_SAMPLE_RATE
        except Exception as e:
            logger.warning("Failed to open spk at %dHz, trying %dHz. Error: %s", OUTPUT_SAMPLE_RATE, fallback_rate, e)
            self._spk_actual_rate = fallback_rate
            frames = int(OUTPUT_CHUNK_FRAMES * fallback_rate / OUTPUT_SAMPLE_RATE)
            self._spk_stream = self._pa.open(
                format=_pa_mod.paInt16,
                channels=CHANNELS,
                rate=fallback_rate,
                output=True,
                frames_per_buffer=frames,
            )

        logger.info(
            "Audio transport started  mic=%dHz (actual %dHz)  spk=%dHz (actual %dHz)",
            INPUT_SAMPLE_RATE, self._mic_actual_rate,
            OUTPUT_SAMPLE_RATE, self._spk_actual_rate,
        )

    async def read_chunk(self) -> bytes:
        """Read one chunk from the microphone (runs in executor)."""
        frames_to_read = INPUT_CHUNK_FRAMES
        if self._mic_actual_rate != INPUT_SAMPLE_RATE:
            frames_to_read = int(INPUT_CHUNK_FRAMES * self._mic_actual_rate / INPUT_SAMPLE_RATE)
            
        data = await self._loop.run_in_executor(
            None,
            self._mic_stream.read,
            frames_to_read,
            False,  # exception_on_overflow
        )
        
        if self._mic_actual_rate != INPUT_SAMPLE_RATE:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                import audioop
            data, self._mic_state = audioop.ratecv(data, 2, 1, self._mic_actual_rate, INPUT_SAMPLE_RATE, self._mic_state)
            
        return data

    async def write_chunk(self, data: bytes) -> None:
        """Write one chunk to the speaker (runs in executor)."""
        if self._spk_actual_rate != OUTPUT_SAMPLE_RATE:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                import audioop
            data, self._spk_state = audioop.ratecv(data, 2, 1, OUTPUT_SAMPLE_RATE, self._spk_actual_rate, self._spk_state)
            
        await self._loop.run_in_executor(None, self._spk_stream.write, data)

    async def stop(self) -> None:
        """Tear down PyAudio streams and terminate."""
        for stream in (self._mic_stream, self._spk_stream):
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass  # best-effort cleanup

        if self._pa is not None:
            self._pa.terminate()

        self._mic_stream = None
        self._spk_stream = None
        self._pa = None
        logger.info("Audio transport stopped")
