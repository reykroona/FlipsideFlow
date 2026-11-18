"""PCM continuity, recovery and failure handling without requiring an audio device."""
import json
import struct
import time
import wave
from types import SimpleNamespace

import pytest

from recording import FRAME_BYTES, HEADER_BYTES, Recorder, RecordingError, wav_header


class FakeStream:
    def __init__(self, **kwargs):
        self.options = kwargs
        self.callback = kwargs["callback"]
        self.active = False
        self.closed = False

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def close(self):
        self.active = False
        self.closed = True

    def emit(self, data, overflow=False):
        assert self.active
        try:
            self.callback(data, len(data) // FRAME_BYTES, None, SimpleNamespace(input_overflow=overflow))
        except FakeAudio.CallbackAbort:
            self.active = False


class FakeAudio:
    class CallbackAbort(Exception):
        pass

    def query_devices(self):
        return [dict(name="Scarlett USB", hostapi=0, max_input_channels=4, default_samplerate=48000),
                dict(name="Speakers", hostapi=0, max_input_channels=0, default_samplerate=48000)]

    def query_hostapis(self):
        return [{"name": "Core Audio"}]

    def CoreAudioSettings(self, **kwargs):
        return kwargs

    def check_input_settings(self, **kwargs):
        self.checked = kwargs

    def RawInputStream(self, **kwargs):
        self.stream = FakeStream(**kwargs)
        return self.stream


@pytest.fixture
def recorder(tmp_path):
    rec = Recorder(tmp_path / "recordings", audio=FakeAudio())
    yield rec
    rec.shutdown()


def monitor(rec, rate=48000):
    return rec.monitor(0, "Scarlett USB", 1, 2, rate)


def emit(rec, data, **kwargs):
    rec._stream.emit(data, **kwargs)
    rec._queue.join()


def wav_data(path):
    with wave.open(str(path), "rb") as w:
        return w.getparams(), w.readframes(w.getnframes())


def test_pause_resume_is_sample_exact_and_monitoring_is_not_saved(recorder):
    rec = recorder
    monitor(rec)
    emit(rec, b"\x00\x00\x40\x00\x00\x20" * 50)
    assert rec.status()["levels"] == [-6.0, -12.0]
    assert rec.status()["seconds"] == 0
    rec.start("Artist / Album")
    first = b"\x01\x02\x03\x04\x05\x06" * 2048
    second = b"\x06\x05\x04\x03\x02\x01" * 1024
    emit(rec, first)
    paused = rec.pause(side_flip=True)
    assert paused["side_markers"] == [2048]
    emit(rec, bytes(6000))
    assert rec.status()["seconds"] == paused["seconds"]
    rec.pause(side_flip=True)  # idempotent, no duplicate side marker
    rec.resume()
    emit(rec, second)
    result = rec.stop()
    path, manifest = rec.open_saved(result["id"])
    params, data = wav_data(path)
    assert params.nchannels == 2 and params.sampwidth == 3 and params.framerate == 48000
    assert params.nframes == 3072 and data == first + second
    assert manifest["side_markers"] == [2048] and manifest["title"] == "Artist - Album"
    assert result["seconds"] == 3072 / 48000
    mtime = path.stat().st_mtime_ns
    rec.stop()
    rec.open_saved(result["id"])
    assert path.stat().st_mtime_ns == mtime, "retries must not invalidate an in-place source"


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
def test_rate_channel_mapping_and_clipping(recorder, rate):
    rec = recorder
    rec.monitor(0, "Scarlett USB", 3, 4, rate)
    settings = rec.audio.checked
    assert settings["extra_settings"]["channel_map"] == [2, 3]
    assert settings["extra_settings"]["fail_if_conversion_required"]
    assert settings["dtype"] == "int24"
    assert rec._stream.options["dither_off"] is True
    rec.start()
    emit(rec, b"\xff\xff\x7f\x00\x00\x80" * 128)
    assert rec.status()["clipping"] == [True, True]
    result = rec.stop()
    path, _ = rec.open_saved(result["id"])
    assert wav_data(path)[0].framerate == rate


def test_invalid_devices_channels_and_transitions(recorder):
    rec = recorder
    assert len(rec.devices()) == 1
    with pytest.raises(RecordingError):
        rec.start()
    for args in [(0, "Other device", 1, 2, 48000), (0, "Scarlett USB", 1, 1, 48000),
                 (0, "Scarlett USB", 1, 5, 48000), (0, "Scarlett USB", 1, 2, 12345)]:
        with pytest.raises(RecordingError):
            rec.monitor(*args)
    monitor(rec)
    rec.start()
    with pytest.raises(RecordingError):
        monitor(rec)
    with pytest.raises(RecordingError):
        rec.open_saved(rec.status()["id"])


def test_recover_stale_header_and_partial_frame_after_restart(recorder):
    rec = recorder
    monitor(rec)
    rec.start()
    original = b"\x01\x02\x03\x04\x05\x06" * 1024
    emit(rec, original)
    result = rec.stop()
    path, manifest = rec.open_saved(result["id"])
    # Simulate a hard exit before the next header/manifest checkpoint.
    with path.open("r+b") as f:
        f.write(wav_header(0, 48000))
        f.seek(0, 2)
        f.write(b"\x01\x02")
    manifest["state"] = "recording"
    (path.parent / "manifest.json").write_text(json.dumps(manifest))
    rec.shutdown()
    restored = Recorder(rec.root, audio=FakeAudio())
    try:
        assert restored.saved()[0]["state"] == "recording"
        recovered, _ = restored.open_saved(result["id"])
        params, data = wav_data(recovered)
        assert params.nframes == 1024 and data == original
        assert restored.saved()[0]["state"] == "finished"
    finally:
        restored.shutdown()


@pytest.mark.parametrize("failure", ["overflow", "disconnect", "disk"])
def test_failure_preserves_audio_without_status_polling(recorder, failure, monkeypatch):
    rec = recorder
    monitor(rec)
    rec.start()
    original = b"\x01\x02\x03\x04\x05\x06" * 256
    emit(rec, original)
    if failure == "overflow":
        emit(rec, original, overflow=True)
    elif failure == "disconnect":
        rec._stream.active = False
    else:
        rec._error = rec._callback_error = "Disk is full"
    deadline = time.monotonic() + 2
    while rec._state != "error" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert rec._state == "error", "watchdog must act even without a connected UI"
    assert rec._file is None
    rec.stop()
    path, _ = rec.open_saved(rec._manifest["id"])
    assert wav_data(path)[1] == original


def test_recording_folder_excludes_other_processes(recorder):
    monitor(recorder)
    other = Recorder(recorder.root, audio=FakeAudio())
    try:
        with pytest.raises(RecordingError, match="Another FlipsideFlow"):
            monitor(other)
    finally:
        other.shutdown()


def test_large_wav_promotes_to_rf64_without_moving_samples():
    size = 0x100000000 // FRAME_BYTES * FRAME_BYTES
    header = wav_header(size, 96000)
    assert len(header) == HEADER_BYTES
    assert header[:4] == b"RF64" and header[12:16] == b"ds64"
    assert struct.unpack_from("<QQQ", header, 20) == (size + 72, size, size // 6)
    assert header[-8:-4] == b"data"


def test_permission_failure_does_not_open_input_or_record(recorder):
    def denied():
        raise RecordingError("Microphone access is denied")
    recorder._permission = denied
    with pytest.raises(RecordingError, match="denied"):
        monitor(recorder)
    assert recorder._stream is None
    assert not recorder.saved()


def test_disk_write_and_close_failure_still_releases_recording(recorder):
    rec = recorder
    monitor(rec)
    rec.start()
    original = bytes(600)
    emit(rec, original)
    rec._checkpoint()
    real_file = rec._file
    class FullDisk:
        def write(self, data):
            raise OSError("No space left on device")
        def seek(self, *args):
            return real_file.seek(*args)
        def close(self):
            real_file.close()
            raise OSError("No space left on device")
    rec._file = FullDisk()
    emit(rec, bytes(600))
    result = rec.stop()
    assert result["state"] == "error" and rec._file is None
    path, _ = rec.open_saved(result["id"])
    assert wav_data(path)[1] == original


def test_disconnected_device_can_reject_stop_and_close(recorder):
    rec = recorder
    monitor(rec)
    rec.start()
    emit(rec, bytes(600))
    stream = rec._stream
    stream.active = False
    def gone():
        raise RuntimeError("Device unavailable")
    stream.stop = stream.close = gone
    result = rec.stop()
    assert result["state"] == "error" and rec._stream is None and rec._worker is None
    path, _ = rec.open_saved(result["id"])
    assert wav_data(path)[0].nframes == 100


def test_low_disk_space_rejects_start_without_losing_monitor(recorder, monkeypatch):
    monitor(recorder)
    monkeypatch.setattr("recording.shutil.disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(RecordingError, match="free disk"):
        recorder.start()
    assert recorder.status()["state"] == "monitoring"


def _block(level, frames=4800):
    """One 0.1 s stereo block at 48 kHz of a constant sample (level 0..1)."""
    value = int(level * 8388607)
    sample = struct.pack("<i", value)[:3]
    return (sample + sample) * frames


def test_side_end_detector_waits_for_a_long_quiet_after_the_minimum_side():
    from recording import SideEndDetector
    d = SideEndDetector()
    # Quiet early in a side is music: no side end before the minimum side length.
    assert not any(d.side_ended(-70, 0.5, 30, 240) for _ in range(40))
    d.reset()
    assert not any(d.side_ended(-70, 0.5, 300, 240) for _ in range(19))
    assert d.side_ended(-70, 0.5, 300, 240)
    d.reset()
    assert not d.needle_dropped(-10, 0.2)  # the drop's thump alone
    assert not d.needle_dropped(-60, 0.2)
    assert not any(d.needle_dropped(-20, 0.1) for _ in range(5))
    assert d.needle_dropped(-20, 0.1)


def test_auto_flip_pauses_at_runout_and_resumes_at_needle_drop_with_preroll(recorder, monkeypatch):
    import recording
    monkeypatch.setattr(recording.SideEndDetector, "DEFAULT_MIN_SIDE_S", 1.0)
    monkeypatch.setattr(recording.SideEndDetector, "QUIET_S", 0.5)
    rec = recorder
    monitor(rec)
    rec.start("A — B", auto_flip=True)
    music, quiet = _block(0.25), _block(0.0)
    for _ in range(15):
        emit(rec, music)
    for _ in range(5):  # 0.5 s of run-out groove
        emit(rec, quiet)
    status = rec.status()
    assert status["state"] == "paused" and status["auto_waiting"] and status["side_markers"]
    saved = status["seconds"]
    lead_in = _block(0.001)
    for _ in range(3):
        emit(rec, lead_in)      # crackle before the music: kept as pre-roll, not saved yet
    assert rec.status()["seconds"] == saved
    for _ in range(6):
        emit(rec, music)        # 0.6 s of music: the needle is down
    status = rec.status()
    assert status["state"] == "recording" and status["side"] == "B"
    # The pre-roll and the drop are saved sample-exactly: nothing between pause and now is lost.
    assert status["seconds"] == pytest.approx(saved + 0.9, abs=1e-6)
    emit(rec, music)
    result = rec.stop()
    path, manifest = rec.open_saved(result["id"])
    _, data = wav_data(path)
    assert len(data) // FRAME_BYTES == round((saved + 1.0) * 48000)
    assert manifest["side_markers"] == [round(saved * 48000)]


def test_plain_pause_never_resumes_by_itself(recorder):
    rec = recorder
    monitor(rec)
    rec.start("x", auto_flip=True)
    emit(rec, _block(0.25))
    rec.pause()
    for _ in range(10):
        emit(rec, _block(0.25))
    assert rec.status()["state"] == "paused"


def test_flip_at_the_end_of_the_last_side_is_not_kept(recorder):
    rec = recorder
    monitor(rec)
    rec.start("x")
    emit(rec, _block(0.25))
    rec.pause(side_flip=True)
    rec.resume()
    emit(rec, _block(0.25))
    rec.pause(side_flip=True)   # end of side B, then Finish
    result = rec.stop()
    _, manifest = rec.open_saved(result["id"])
    assert manifest["side_markers"] == [4800]
