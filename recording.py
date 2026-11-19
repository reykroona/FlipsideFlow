"""Local desktop PCM capture. Audio never passes through the browser.

PortAudio's callback only copies into a bounded queue. A single writer handles
meters and disk I/O. Control operations stop the stream and drain that queue
before changing state, so pause/resume boundaries cannot race queued samples.
Originals and their manifests live outside the upload/cache directory.
"""
from __future__ import annotations

import json
import math
import os
import queue
import re
import shutil
import struct
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np
from silence_learning import SilenceLearning

RATES = (44100, 48000, 96000)
FRAME_BYTES = 6  # stereo, packed signed 24-bit PCM
HEADER_BYTES = 80  # reserve a ds64-sized JUNK chunk for in-place RF64 promotion
MIN_FREE_BYTES = 64 * 1024 * 1024
ACTIVE_STATES = {"recording", "paused"}
PREROLL_S = 5.0  # kept while waiting for a needle drop, so a quiet intro is never cut
LAST_INPUT_FILE = ".last-input.json"  # the input that last opened, offered again at the next launch


class RecordingError(RuntimeError):
    pass


class SideEndDetector:
    """Spots the end of a side and the next needle drop from per-block input levels.

    A side has ended after ``QUIET_S`` of near-silence (the run-out groove or a lifted needle
    sit far below any music), but never before ``min_side_s`` of the side: quiet passages
    early in a side are music.  A needle drop is ``LOUD_S`` of sustained level; the drop's
    own thump is shorter than that.  Pure: no audio, threads or clocks.
    """
    QUIET_DB = -48.0
    QUIET_S = 10.0
    LOUD_DB = -38.0
    LOUD_S = 0.6
    DEFAULT_MIN_SIDE_S = 240.0

    def __init__(self):
        self.quiet = 0.0
        self.loud = 0.0
        self.burst = 0.0
        self.clicks = 0.0

    def reset(self):
        self.quiet = self.loud = self.burst = self.clicks = 0.0

    def side_ended(self, rms_db: float, block_s: float, side_s: float, min_side_s: float) -> bool:
        if rms_db < self.QUIET_DB:
            self.quiet += block_s
            self.clicks += self.burst if self.burst <= .1 else 0
            self.burst = 0.0
            if self.clicks > max(.1, self.quiet * .1):
                self.quiet = self.clicks = 0.0
        else:
            self.burst += block_s
            if self.burst > .1:
                self.quiet = self.clicks = 0.0
        return self.burst == 0 and self.quiet >= self.QUIET_S and side_s >= min_side_s

    def needle_dropped(self, rms_db: float, block_s: float) -> bool:
        self.loud = self.loud + block_s if rms_db > self.LOUD_DB else 0.0
        return self.loud >= self.LOUD_S


def wav_header(size: int, rate: int) -> bytes:
    """Standard PCM WAV below 4 GiB, RF64 above it; identical data offset."""
    riff_size = size + HEADER_BYTES - 8
    large = riff_size > 0xFFFFFFFF
    prefix = struct.pack("<4sI4s", b"RF64" if large else b"RIFF",
                         0xFFFFFFFF if large else riff_size, b"WAVE")
    reserve = (struct.pack("<4sIQQQI", b"ds64", 28, riff_size, size, size // FRAME_BYTES, 0)
               if large else struct.pack("<4sI", b"JUNK", 28) + bytes(28))
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 2, rate, rate * FRAME_BYTES, FRAME_BYTES, 24)
    return prefix + reserve + fmt + struct.pack("<4sI", b"data", 0xFFFFFFFF if large else size)


def request_microphone_permission() -> None:
    """Called only after an explicit user action, never at app startup."""
    import AVFoundation as av
    status = av.AVCaptureDevice.authorizationStatusForMediaType_(av.AVMediaTypeAudio)
    if status == av.AVAuthorizationStatusNotDetermined:
        done = threading.Event()
        result = []
        def received(granted):
            result.append(bool(granted))
            done.set()
        av.AVCaptureDevice.requestAccessForMediaType_completionHandler_(av.AVMediaTypeAudio, received)
        if not done.wait(60):
            raise RecordingError("Microphone permission is still pending. Allow FlipsideFlow in the macOS prompt, then try again.")
        if result and result[0]:
            return
    elif status == av.AVAuthorizationStatusAuthorized:
        return
    raise RecordingError("Microphone access is denied. Enable FlipsideFlow in System Settings → Privacy & Security → Microphone, then reopen the app.")


class Recorder:
    def __init__(self, root: Path, *, audio=None, permission=None, protect_sleep=False):
        self.root = Path(root).resolve()
        self._audio = audio
        self._permission = permission or (lambda: None)
        self._protect_sleep = protect_sleep
        self._control = threading.RLock()
        self._stream = None
        self._worker = None
        self._queue = queue.Queue(maxsize=128)
        self._worker_stop = threading.Event()
        self._callback_error = None
        self._file = None
        self._manifest = None
        self._bytes = 0
        self._caffeinate = None
        self._folder_lock = None
        self._state = "idle"
        self._error = None
        self._meter_lock = threading.Lock()
        self._peak_hold = [0.0, 0.0]
        self._levels = [0.0, 0.0]
        self._clips = [False, False]
        self._meter_time = 0.0
        self._overflows = 0
        self._settings = None
        self._watchdog_stop = threading.Event()
        self._watchdog = None
        self._last_input = None
        self._last_input_loaded = False
        # Automatic side flips: pause at the run-out groove, resume at the next needle drop.
        self._auto_flip = False
        self._side_lengths = []      # expected seconds per side (Discogs), when known
        self._side_index = 0
        self._side_frames = 0        # frames saved since this side started
        self._detector = SideEndDetector()
        self.calibration = SilenceLearning()
        self._auto_waiting = False   # paused at a side flip, listening for the needle
        self._auto_live = False      # needle dropped: the writer saves before the state catches up
        self._auto_event = None      # "side_end" / "needle_drop", acted on by the control thread
        self._auto_message = ""
        self._preroll = deque()
        self._preroll_bytes = 0

    @property
    def audio(self):
        if self._audio is None:
            try:
                import sounddevice
                self._audio = sounddevice
            except (ImportError, OSError) as exc:
                raise RecordingError("CoreAudio recording is unavailable. Install the macOS recording dependencies or rebuild the desktop app.") from exc
        return self._audio

    def devices(self):
        try:
            devices = self.audio.query_devices()
            hosts = self.audio.query_hostapis()
            return [dict(id=i, name=d["name"], channels=int(d["max_input_channels"]),
                         default_rate=int(d["default_samplerate"]))
                    for i, d in enumerate(devices)
                    if d["max_input_channels"] >= 2 and hosts[d["hostapi"]]["name"] == "Core Audio"]
        except RecordingError:
            raise
        except Exception as exc:
            raise RecordingError(f"Could not list CoreAudio inputs: {exc}") from exc

    def _lock_folder(self):
        if self._folder_lock is not None:
            return
        import fcntl
        self.root.mkdir(parents=True, exist_ok=True)
        lock = (self.root / ".recorder.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock.close()
            raise RecordingError("Another FlipsideFlow window is using the recordings folder. Close it first.") from exc
        self._folder_lock = lock

    def monitor(self, device: int, device_name: str, left: int, right: int, rate: int):
        with self._control:
            if self._state in ACTIVE_STATES:
                raise RecordingError("Stop the current recording before changing inputs.")
            available = {d["id"]: d for d in self.devices()}
            info = available.get(device)
            if not info or info["name"] != device_name:
                raise RecordingError("The input device changed. Refresh inputs and select the Scarlett again.")
            if rate not in RATES or left == right or not all(1 <= c <= info["channels"] for c in (left, right)):
                raise RecordingError("Choose two different input channels and a supported sample rate.")
            self.shutdown_stream()
            self._permission()
            self._lock_folder()
            self._error = self._callback_error = None
            self._overflows = 0
            self._manifest = None
            self._bytes = 0
            with self._meter_lock:
                self._levels, self._peak_hold, self._clips = [0.0, 0.0], [0.0, 0.0], [False, False]
            settings = self.audio.CoreAudioSettings(channel_map=[left - 1, right - 1],
                                                    change_device_parameters=True,
                                                    fail_if_conversion_required=True)
            try:
                self.audio.check_input_settings(device=device, channels=2, dtype="int24", samplerate=rate,
                                                extra_settings=settings)
                self._stream = self.audio.RawInputStream(
                    device=device, channels=2, dtype="int24", samplerate=rate,
                    blocksize=2048, latency="high", dither_off=True,
                    extra_settings=settings, callback=self._callback)
                self.calibration.cancel()
                self._settings = dict(device=device, device_name=device_name, left=left, right=right, rate=rate)
                self._remember_input()
                self._worker_stop.clear()
                self._state = "monitoring"
                self._worker = threading.Thread(target=self._write_loop, name="vinylflow-recorder", daemon=True)
                self._worker.start()
                self._stream.start()
                if self._watchdog is None or not self._watchdog.is_alive():
                    self._watchdog_stop.clear()
                    self._watchdog = threading.Thread(target=self._watch, name="vinylflow-input-watch", daemon=True)
                    self._watchdog.start()
            except Exception as exc:
                self.shutdown_stream()
                self._state = "error"
                self._error = f"Could not open the audio input at {rate} Hz: {exc}. Check Microphone permission and Audio MIDI Setup."
                raise RecordingError(self._error) from exc
            return self.status()

    def _remember_input(self):
        """Keep the device name, channels and rate (device ids change between launches)."""
        choice = {k: self._settings[k] for k in ("device_name", "left", "right", "rate")}
        self._last_input, self._last_input_loaded = choice, True
        try:
            temp = self.root / (LAST_INPUT_FILE + ".tmp")
            temp.write_text(json.dumps(choice))
            temp.replace(self.root / LAST_INPUT_FILE)
        except OSError:
            pass  # a convenience only; recording does not depend on it

    def last_input(self):
        if not self._last_input_loaded:
            self._last_input_loaded = True
            try:
                data = json.loads((self.root / LAST_INPUT_FILE).read_text())
                if (isinstance(data.get("device_name"), str) and data.get("rate") in RATES
                        and all(isinstance(data.get(k), int) and 1 <= data[k] <= 128 for k in ("left", "right"))
                        and data["left"] != data["right"]):
                    self._last_input = {k: data[k] for k in ("device_name", "left", "right", "rate")}
            except (OSError, ValueError, AttributeError):
                pass
        return dict(self._last_input) if self._last_input else None

    def _callback(self, data, frames, time_info, status):
        recording = self._state == "recording"
        if status.input_overflow:
            self._overflows += 1
            if recording:
                self._callback_error = "Audio input overflow. Recording stopped to avoid silently saving missing audio."
                raise self.audio.CallbackAbort
        try:
            self._queue.put_nowait((bytes(data), recording))
        except queue.Full:
            if recording:
                self._overflows += 1
                self._callback_error = "The disk writer could not keep up. Captured audio has been preserved."
                raise self.audio.CallbackAbort

    def _write_loop(self):
        last_flush = time.monotonic()
        while not self._worker_stop.is_set() or not self._queue.empty():
            try:
                data, recording = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                packed = np.frombuffer(data, dtype=np.uint8).reshape(-1, 2, 3).astype(np.int32)
                values = packed[:, :, 0] | (packed[:, :, 1] << 8) | (packed[:, :, 2] << 16)
                values = (values ^ 0x800000) - 0x800000
                self.calibration.feed(values, self._settings["rate"])
                peaks = np.max(np.abs(values), axis=0) / 8388608.0
                with self._meter_lock:
                    self._levels = peaks.tolist()
                    self._peak_hold = [max(old, float(peak)) for old, peak in zip(self._peak_hold, peaks)]
                    self._meter_time = time.monotonic()
                    self._clips = [bool(old or peak >= 0.999) for old, peak in zip(self._clips, peaks)]
                live = recording or self._auto_live
                if live and self._file is not None and not self._error:
                    self._file.write(data)
                    self._bytes += len(data)
                    self._side_frames += len(data) // FRAME_BYTES
                    if time.monotonic() - last_flush >= 1:
                        self._checkpoint()
                        last_flush = time.monotonic()
                        if shutil.disk_usage(self.root).free < MIN_FREE_BYTES:
                            raise OSError("Low disk space")
                if self._auto_flip and self._settings and len(values):
                    self._auto_listen(data, values, live)
            except Exception as exc:
                # Continue draining: a control operation must never deadlock on queue.join().
                self._error = f"Recording interrupted: {exc}. Already captured audio is retained."
                self._callback_error = self._error
            finally:
                self._queue.task_done()

    def _auto_listen(self, data, values, live):
        """Writer thread: watch the level for a side end, or start saving at a needle drop."""
        rate = self._settings["rate"]
        block_s = len(values) / rate
        rms = np.sqrt(np.mean((values.astype(np.float64) / 8388608.0) ** 2, axis=0))
        rms_db = 20 * math.log10(max(float(np.max(rms)), 1e-9))
        if live:
            if self._event_pending():
                return
            if self._detector.side_ended(rms_db, block_s, self._side_frames / rate, self._min_side_s()):
                self._auto_event = "side_end"
        elif self._auto_waiting and self._file is not None and not self._error:
            self._preroll.append(data)
            self._preroll_bytes += len(data)
            limit = int(PREROLL_S * rate) * FRAME_BYTES
            while self._preroll_bytes - len(self._preroll[0]) >= limit:
                self._preroll_bytes -= len(self._preroll.popleft())
            if self._detector.needle_dropped(rms_db, block_s):
                # Sample-exact: the pre-roll (this block included) goes to disk now and every
                # later block follows, before the control thread flips the state.
                for chunk in self._preroll:
                    self._file.write(chunk)
                self._bytes += self._preroll_bytes
                self._side_frames = self._preroll_bytes // FRAME_BYTES
                self._preroll.clear()
                self._preroll_bytes = 0
                self._auto_waiting = False
                self._auto_live = True
                self._detector.reset()
                self._auto_event = "needle_drop"

    def _event_pending(self):
        return self._auto_event is not None

    def _min_side_s(self):
        if self._side_index < len(self._side_lengths) and self._side_lengths[self._side_index]:
            return 0.75 * self._side_lengths[self._side_index]
        return SideEndDetector.DEFAULT_MIN_SIDE_S

    def _auto_step(self):
        """Control thread (lock held): act on what the writer heard."""
        event, self._auto_event = self._auto_event, None
        if event == "side_end" and self._state == "recording":
            ended = self._side_letter(self._side_index)
            self.pause(side_flip=True)
            self._auto_message = f"Side {ended} ended. Flip the record and drop the needle; recording resumes by itself."
        elif event == "needle_drop" and self._state == "paused":
            self._manifest["state"] = "recording"
            self._save_manifest()
            self._state = "recording"
            self._auto_message = f"Needle drop heard. Recording side {self._side_letter(self._side_index)}."

    @staticmethod
    def _side_letter(index):
        return chr(ord("A") + index) if 0 <= index < 26 else str(index + 1)

    def _drain(self):
        if self._stream is not None:
            try:
                self._stream.stop()  # waits for callbacks before the queue is drained
            except Exception as exc:
                self._error = f"Audio input disconnected: {exc}"
                try:
                    self._stream.close()
                except Exception:
                    pass  # disconnected devices may also reject close
                self._stream = None
        self._queue.join()

    def _watch(self):
        # Capture errors must be handled even when the browser is disconnected.
        while not self._watchdog_stop.wait(0.25):
            if self._control.acquire(blocking=False):
                try:
                    self._check_health()
                    if self._state in ACTIVE_STATES:
                        self._auto_step()
                finally:
                    self._control.release()

    def _save_manifest(self):
        if self._manifest is None:
            return
        folder = self.root / self._manifest["id"]
        temp = folder / "manifest.tmp"
        with temp.open("w") as f:
            json.dump(self._manifest, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        temp.replace(folder / "manifest.json")

    def _checkpoint(self):
        if self._file is not None:
            self._file.seek(0)
            self._file.write(wav_header(self._bytes, self._settings["rate"]))
            self._file.seek(HEADER_BYTES + self._bytes)
            self._file.flush()
            os.fsync(self._file.fileno())

    def start(self, title="", auto_flip=False, side_lengths=None):
        with self._control:
            self._check_health()
            if self._state != "monitoring":
                raise RecordingError("Check the input levels before starting a recording.")
            if shutil.disk_usage(self.root).free < MIN_FREE_BYTES:
                raise RecordingError("Not enough free disk space to start recording.")
            self.calibration.cancel()
            self._drain()
            ident = str(uuid.uuid4())
            folder = self.root / ident
            folder.mkdir()
            title = re.sub(r'[\x00-\x1f/\\:]', "-", title).strip(" .")[:120]
            title = title or datetime.now().strftime("Recording %Y-%m-%d %H-%M-%S")
            self._manifest = dict(version=1, id=ident, title=title, rate=self._settings["rate"],
                                  channels=2, bit_depth=24, input=self._settings.copy(),
                                  side_markers=[], pause_markers=[], state="recording", created_at=datetime.now().isoformat())
            self._bytes = 0
            with self._meter_lock:
                self._levels, self._peak_hold, self._clips = [0.0, 0.0], [0.0, 0.0], [False, False]
            self._auto_flip = bool(auto_flip)
            self._side_lengths = [float(x) if isinstance(x, (int, float)) and 0 < x < 7200 else 0.0
                                  for x in (side_lengths or [])][:26]
            self._side_index = 0
            self._side_frames = 0
            self._detector.reset()
            self._auto_waiting = self._auto_live = False
            self._auto_event = None
            self._auto_message = ""
            self._preroll.clear()
            self._preroll_bytes = 0
            try:
                self._file = (folder / "audio.wav").open("w+b")
                self._checkpoint()
                self._save_manifest()
                self._state = "recording"
                if self._protect_sleep:
                    self._caffeinate = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])
                self._stream.start()
            except Exception as exc:
                self._error = f"Could not start recording: {exc}"
                self._finish("interrupted")
                raise RecordingError(self._error) from exc
            return self.status()

    def pause(self, side_flip=False):
        with self._control:
            self._check_health()
            if self._state == "paused":
                return self.status()  # retried command
            if self._state != "recording":
                raise RecordingError("There is no recording to pause.")
            self._drain()
            if self._error:
                self._finish("interrupted")
                raise RecordingError(self._error)
            self._auto_live = False
            self._auto_event = None
            self._detector.reset()
            if side_flip and self._bytes:
                self._manifest["side_markers"].append(self._bytes // FRAME_BYTES)
                self._side_index += 1
                self._side_frames = 0
            # Only a side flip listens for the needle; a plain pause waits for Resume.
            self._auto_waiting = self._auto_flip and side_flip
            if not side_flip:
                self._auto_message = ""
            self._preroll.clear()
            self._preroll_bytes = 0
            self._manifest["pause_markers"].append(self._bytes // FRAME_BYTES)
            self._state = "paused"
            self._manifest["state"] = "paused"
            self._checkpoint()
            self._save_manifest()
            self._stream.start()  # meters continue, but these frames are not saved
            return self.status()

    def resume(self):
        with self._control:
            self._check_health()
            if self._state == "recording":
                return self.status()
            if self._state != "paused":
                raise RecordingError("There is no paused recording to resume.")
            self._drain()
            if self._error:
                self._finish("interrupted")
                raise RecordingError(self._error)
            self._auto_waiting = self._auto_live = False
            self._auto_event = None
            self._preroll.clear()
            self._preroll_bytes = 0
            self._detector.reset()
            self._auto_message = ""
            self._manifest["state"] = "recording"
            self._save_manifest()
            self._state = "recording"
            self._stream.start()
            return self.status()

    def _finish(self, state="finished"):
        try:
            self._checkpoint()
        except OSError as exc:
            self._error = f"Could not finalize the WAV header: {exc}. Use Recover in Saved recordings."
            state = "interrupted"
        finally:
            if self._file is not None:
                try:
                    self._file.close()
                except OSError as exc:
                    self._error = f"Could not flush the recording: {exc}. Use Recover in Saved recordings."
                    state = "interrupted"
                finally:
                    self._file = None
            if self._caffeinate is not None:
                self._caffeinate.terminate()
                self._caffeinate.wait()
                self._caffeinate = None
        self._state = "error" if state == "interrupted" else "finished"
        if self._manifest is not None:
            self._manifest.update(state=state, frames=self._bytes // FRAME_BYTES, error=self._error)
            try:
                self._save_manifest()
            except OSError:
                self._error = self._error or "Could not update recording details. The audio remains in the recordings folder."

    def _check_health(self):
        if self._state not in {"recording", "paused", "monitoring"}:
            return
        try:
            inactive = self._stream is None or not self._stream.active
        except Exception:
            inactive = True
        if self._callback_error or inactive:
            self._error = self._callback_error or "The audio input stopped or disconnected. Captured audio has been preserved."
            self._drain()
            self._finish("interrupted")

    def stop(self):
        with self._control:
            self._check_health()
            if self._state in ACTIVE_STATES:
                self._drain()
                self._auto_waiting = self._auto_live = False
                self._auto_event = None
                # A flip with no audio after it (paused at the end of the last side) is no side change.
                total = self._bytes // FRAME_BYTES
                self._manifest["side_markers"] = [m for m in self._manifest["side_markers"] if m < total]
                self._finish("interrupted" if self._error else "finished")
            elif self._state == "monitoring":
                self._drain()
                self._state = "idle"
            self.shutdown_stream()
            return self.status()

    def shutdown_stream(self):
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass  # hot-unplug may invalidate the handle before we can stop it
            finally:
                try:
                    self._stream.close()
                except Exception:
                    pass
                finally:
                    self._stream = None
        self._worker_stop.set()
        if self._worker is not None:
            self._worker.join()
            self._worker = None

    def shutdown(self):
        with self._control:
            self.stop()
            self._watchdog_stop.set()
            if self._folder_lock is not None:
                self._folder_lock.close()
                self._folder_lock = None
        if self._watchdog is not None:
            self._watchdog.join()

    def status(self):
        with self._control:
            self._check_health()
            if self._state in ACTIVE_STATES:
                self._auto_step()
            with self._meter_lock:
                levels = list(self._levels) if time.monotonic() - self._meter_time < 1 else [0, 0]
                held = list(self._peak_hold)
                clips = list(self._clips)
            return dict(calibration=self.calibration.status(), state=self._state, id=self._manifest["id"] if self._manifest else None,
                        title=self._manifest["title"] if self._manifest else "",
                        seconds=self._bytes / FRAME_BYTES / self._settings["rate"] if self._settings else 0,
                        levels=[round(20 * math.log10(max(p, 0.000001)), 1) for p in levels],
                        peak_hold=[round(20 * math.log10(max(p, 0.000001)), 1) for p in held],
                        clipping=clips, overflows=self._overflows, error=self._error,
                        settings=self._settings, last_input=self.last_input(), directory=str(self.root),
                        free_bytes=shutil.disk_usage(self.root).free if self.root.exists() else None,
                        side_markers=list(self._manifest["side_markers"]) if self._manifest else [],
                        auto_flip=self._auto_flip, auto_waiting=self._auto_waiting and self._state == "paused",
                        auto_message=self._auto_message, side=self._side_letter(self._side_index),
                        side_seconds=self._side_frames / self._settings["rate"] if self._settings else 0)

    def reset_meters(self):
        """Clear held readings without stopping the stream or touching recorded samples.

        The writer and UI only share this tiny lock, never the control lock:
        control operations may drain the writer while holding _control.
        """
        with self._control:
            with self._meter_lock:
                self._peak_hold = [0.0, 0.0]
                self._clips = [False, False]
            return self.status()

    def learn_silence(self, action):
        with self._control:
            if action == 'cancel':
                self.calibration.cancel()
            elif self._state != 'monitoring':
                raise RecordingError('Start input monitoring before learning silence; finish the current recording first.')
            elif action == 'start':
                self.calibration.start()
            elif action != 'apply':
                raise RecordingError('Unknown calibration action.')
            return self.calibration.status()

    def capture_snapshot(self):
        """Small immutable description for background recognition; no audio/network work."""
        with self._control:
            if self._manifest is None:
                return None
            return dict(id=self._manifest["id"], state=self._state, rate=self._manifest["rate"],
                        frames=self._bytes // FRAME_BYTES,
                        path=self.root / self._manifest["id"] / "audio.wav",
                        pause_markers=list(self._manifest.get("pause_markers", [])))

    def saved(self):
        rows = []
        for path in sorted(self.root.glob("*/manifest.json"), reverse=True):
            try:
                item = json.loads(path.read_text())
                ident = str(uuid.UUID(path.parent.name))
                if ident != item["id"] or path.parent.is_symlink():
                    continue
                if item["rate"] not in RATES:
                    continue
                if self._manifest and ident == self._manifest["id"] and self._state in ACTIVE_STATES:
                    continue
                size = (path.parent / "audio.wav").stat().st_size
                rows.append(dict(id=ident, title=item["title"], created_at=item["created_at"],
                                 state=item["state"], error=item.get("error"),
                                 seconds=max(0, size - HEADER_BYTES) // FRAME_BYTES / item["rate"]))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)

    def open_saved(self, ident):
        """Repair a checkpointed/interrupted header, then return a durable source."""
        with self._control:
            self._lock_folder()
            try:
                ident = str(uuid.UUID(ident))
                folder = self.root / ident
                if folder.is_symlink() or (folder / "audio.wav").is_symlink():
                    raise ValueError("Linked recordings cannot be opened")
                item = json.loads((folder / "manifest.json").read_text())
                if item["id"] != ident or item["rate"] not in RATES:
                    raise ValueError("Invalid recording manifest")
                if self._state in ACTIVE_STATES and (item['state'] != 'finished' or ident == (self._manifest or {}).get('id')):
                    raise RecordingError('Cannot open or repair an active recording.')
                path = folder / "audio.wav"
                frames = max(0, path.stat().st_size - HEADER_BYTES) // FRAME_BYTES
                if not frames:
                    raise RecordingError("This recording contains no audio frames.")
                # Don't touch finished sources: changing mtime invalidates in-place sessions.
                if item["state"] != "finished":
                    with path.open("r+b") as f:
                        f.truncate(HEADER_BYTES + frames * FRAME_BYTES)
                        f.seek(0)
                        f.write(wav_header(frames * FRAME_BYTES, item["rate"]))
                        f.flush()
                        os.fsync(f.fileno())
                    item.update(state="finished", frames=frames)
                    temp = folder / "manifest.tmp"
                    temp.write_text(json.dumps(item, indent=2))
                    temp.replace(folder / "manifest.json")
                return path, item
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise RecordingError(f"Could not open this saved recording: {exc}") from exc
