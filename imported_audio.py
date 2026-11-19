"""Read-only, bounded excerpt extraction for WAV/AIFF imports."""
import io
import subprocess
import wave

import numpy as np

from audio_processor import run_ffmpeg
from recognition import CLIP_SECONDS, RecognitionError


def choose_file_excerpt(path, duration):
    if duration < CLIP_SECONDS:
        raise RecognitionError('Choose a file with at least 10 seconds of music.', retryable=False)
    rate = 22050
    try:
        result = run_ffmpeg(['-nostdin', '-v', 'error', '-i', str(path), '-t', '120',
                             '-map', '0:a:0', '-vn', '-ac', '2', '-ar', str(rate),
                             '-f', 's16le', 'pipe:1'], text=False, capture_output=True,
                            check=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        raise RecognitionError('Could not read an audio excerpt. Check the source file and try again.') from None
    samples = np.frombuffer(result.stdout[:len(result.stdout) // 4 * 4], dtype='<i2').reshape(-1, 2)
    count = CLIP_SECONDS * rate
    best = None
    for start in range(0, len(samples) - count + 1, 5 * rate):
        block = samples[start:start + count].astype(np.float64) / 32768
        rms = np.sqrt(np.mean(block ** 2, axis=0))
        channel = int(np.argmax(rms))  # never cancel out-of-phase stereo by averaging
        mono = block[:, channel]
        active = np.mean(np.sqrt(np.mean(mono.reshape(CLIP_SECONDS, rate) ** 2, axis=1)) > 10 ** (-50 / 20))
        clipping = np.mean(np.abs(mono) >= 0.999)
        if active < 0.8 or clipping > 0.005:
            continue
        score = active + min(float(rms[channel]), 0.25) - clipping * 10
        if best is None or score > best[0]:
            best = score, samples[start:start + count, channel].astype('<i2').tobytes(), start / rate
    if best is None:
        raise RecognitionError('No clear music found in the first two minutes. Try another file or search Discogs manually.')
    out = io.BytesIO()
    with wave.open(out, 'wb') as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(rate)
        clip.writeframes(best[1])
    return out.getvalue(), best[2]
