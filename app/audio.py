"""Pull duration, sample rate, bitrate, loudness and a rough SNR out of a file.

Everything here shells out to ffmpeg/ffprobe rather than using a Python audio
library, for one reason: the browser's MediaRecorder produces WebM/Opus, and
most pure-Python readers (wave, soundfile) cannot open that. ffmpeg opens
anything, so the same code path handles a recorded blob and an uploaded MP3.
"""

import json
import subprocess

import numpy as np

# Sample rate we decode to for the loudness/SNR maths. The reported
# sample_rate_hz is the file's real rate from ffprobe, not this one.
ANALYSIS_RATE = 16000

# Length of each analysis window. 50 ms is long enough to average out a single
# glottal pulse and short enough that a pause between words lands in its own frame.
FRAME_SECONDS = 0.05


def probe(path):
    """Return ffprobe's format and first audio stream as dicts."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate,bit_rate,channels,codec_name",
            "-show_entries", "format=duration,bit_rate,format_name",
            "-of", "json", path,
        ],
        capture_output=True, text=True, check=True,
    )
    parsed = json.loads(result.stdout)
    stream = (parsed.get("streams") or [{}])[0]
    return parsed.get("format", {}), stream


def decode_samples(path):
    """Decode to mono float32 at ANALYSIS_RATE. Returns a numpy array in -1..1."""
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", path,
            "-ac", "1", "-ar", str(ANALYSIS_RATE),
            "-f", "f32le", "-",
        ],
        capture_output=True, check=True,
    )
    return np.frombuffer(result.stdout, dtype=np.float32)


def rms_db(samples):
    """RMS level in dBFS. Silence returns None rather than -inf."""
    if samples.size == 0:
        return None
    rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float64)))))
    if rms <= 0:
        return None
    return 20.0 * np.log10(rms)


def estimate_snr_db(samples):
    """Rough SNR: loud frames treated as signal, quiet frames as noise floor.

    This is an estimate, not a measurement. We chop the audio into 50 ms frames
    and take the RMS of each. The assumption is that a real recording contains
    both speech and gaps, so the loudest frames are mostly signal and the
    quietest are mostly room noise. The 90th and 10th percentiles are used
    instead of max/min so one door slam or one dropout cannot define the result.

    It is wrong for audio with no pauses, and wrong for pure silence. Both of
    those return None rather than a confident-looking bad number.
    """
    frame_length = int(ANALYSIS_RATE * FRAME_SECONDS)
    if samples.size < frame_length * 4:
        return None

    frame_count = samples.size // frame_length
    frames = samples[: frame_count * frame_length].reshape(frame_count, frame_length)
    frame_rms = np.sqrt(np.mean(np.square(frames.astype(np.float64)), axis=1))

    signal = float(np.percentile(frame_rms, 90))
    noise = float(np.percentile(frame_rms, 10))
    if signal <= 0 or noise <= 0:
        return None
    return 20.0 * np.log10(signal / noise)


def analyse(path):
    """Return the five metrics for one audio file. Any field may be None."""
    metrics = {
        "duration_sec": None,
        "sample_rate_hz": None,
        "bitrate_kbps": None,
        "loudness_db": None,
        "snr_db": None,
    }

    format_info, stream_info = probe(path)

    if format_info.get("duration"):
        metrics["duration_sec"] = round(float(format_info["duration"]), 3)
    if stream_info.get("sample_rate"):
        metrics["sample_rate_hz"] = int(stream_info["sample_rate"])

    # Prefer the audio stream's own bitrate; WebM/Opus often reports it only at
    # the container level, so fall back to that.
    bit_rate = stream_info.get("bit_rate") or format_info.get("bit_rate")
    if bit_rate:
        metrics["bitrate_kbps"] = round(int(bit_rate) / 1000, 1)

    # float() throughout: numpy scalars leak out of the maths below and the
    # database driver has no adapter for numpy types.
    samples = decode_samples(path)
    loudness = rms_db(samples)
    if loudness is not None:
        metrics["loudness_db"] = round(float(loudness), 2)
    snr = estimate_snr_db(samples)
    if snr is not None:
        metrics["snr_db"] = round(float(snr), 2)

    # A container with no declared bitrate but a known size and duration still
    # has an effective bitrate; compute it so the column is rarely empty.
    if metrics["bitrate_kbps"] is None and metrics["duration_sec"]:
        import os
        size_bits = os.path.getsize(path) * 8
        metrics["bitrate_kbps"] = round(size_bits / metrics["duration_sec"] / 1000, 1)

    return metrics
