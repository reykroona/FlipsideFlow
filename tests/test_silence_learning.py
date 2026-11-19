import numpy as np
import pytest
from silence_learning import SilenceLearning, suggest
from recording import SideEndDetector
from track_boundaries import gaps_from_envelope


def test_calibration_ignores_sparse_loud_pops_and_uses_louder_channel():
    learning = SilenceLearning()
    learning.start()
    rate = 48000
    quiet = np.full((480, 2), 8388608 * 10 ** (-58 / 20))
    quiet[:, 1] = 8388608 * 10 ** (-50 / 20)
    for i in range(1000):
        learning.feed(np.full((480, 2), 8388608 * .8) if i % 100 == 50 else quiet, rate)
    result = learning.status()
    assert result['state'] == 'ready'
    assert result['noise_floor'] == pytest.approx(-50, abs=.1)
    assert result['threshold'] == pytest.approx(-44, abs=.1)
    assert len(learning.levels) == 1000
    learning.feed(np.zeros((48000, 2)), rate)
    assert len(learning.levels) == 1000


@pytest.mark.parametrize('levels', [[-120] * 1000, [-20] * 1000, [-55] * 800 + [-10] * 200])
def test_dead_music_or_repeating_loud_click_sample_is_refused(levels):
    assert not suggest(levels)['usable']


def test_cancel_and_uneven_block_sizes_do_not_reuse_old_samples():
    learning = SilenceLearning()
    learning.start()
    learning.feed(np.full((475, 2), 1000), 48000)
    assert learning.status()['seconds'] == 0
    learning.feed(np.full((485, 2), 1000), 48000)
    assert len(learning.levels) == 2
    learning.cancel()
    learning.feed(np.full((480, 2), 1000), 48000)
    assert learning.status() == {'state': 'idle', 'seconds': 0}


def test_short_pop_does_not_break_gap_but_music_burst_does():
    db = np.r_[np.full(500, -15), np.full(95, -55), np.full(5, -5), np.full(100, -55), np.full(500, -15)]
    gaps = gaps_from_envelope(db, .01, -44, 1.5)
    assert len(gaps) == 1
    assert (gaps[0].start, gaps[0].end) == (5, 7)
    db[580:620] = -15
    assert gaps_from_envelope(db, .01, -44, 1.5) == []


def test_repeating_music_pulses_are_not_one_silence():
    db = np.tile(np.r_[np.full(10, -55), np.full(10, -15)], 100)
    assert gaps_from_envelope(db, .01, -44, 1.5) == []


def test_side_end_tolerates_short_pop_but_resets_on_sustained_music():
    detector = SideEndDetector()
    for _ in range(500): assert not detector.side_ended(-55, .01, 600, 240)
    for _ in range(5): assert not detector.side_ended(-5, .01, 600, 240)
    result = False
    for _ in range(501): result = detector.side_ended(-55, .01, 600, 240)
    assert result
    detector.reset()
    for _ in range(500): detector.side_ended(-55, .01, 600, 240)
    for _ in range(20): detector.side_ended(-15, .01, 600, 240)
    for _ in range(500): assert not detector.side_ended(-55, .01, 600, 240)


def test_real_ffmpeg_silence_pass_bridges_pop(tmp_path):
    import shutil, wave
    from audio_processor import AudioProcessor
    if not shutil.which('ffmpeg'): pytest.skip('ffmpeg unavailable')
    rate = 8000
    samples = np.sin(np.arange(rate * 9) / rate * 2 * np.pi * 440) * .3
    samples[rate * 3:rate * 6] = .001
    samples[rate * 4:rate * 4 + 160] = .7
    path = tmp_path / 'pop.wav'
    with wave.open(str(path), 'wb') as out:
        out.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        out.writeframes((samples * 32767).astype('<i2').tobytes())
    gaps, duration = AudioProcessor()._scan_gaps(path, -40, 2)
    assert duration == pytest.approx(9, abs=.01)
    assert len(gaps) == 1
    assert gaps[0].start == pytest.approx(3, abs=.02)
    assert gaps[0].end == pytest.approx(6, abs=.02)
