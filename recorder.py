"""
recorder.py — BirdWatcher core pipeline

Flow:
  Microphone → RMS silence gate → circular audio buffer
  → 9-second clip trigger → save temp WAV
  → BirdNET-Analyzer → confidence filter
  → save snippet + write SQLite record

Runs until Ctrl+C.
"""

import os
import time
import queue
import struct
import logging
import sqlite3
import hashlib
import threading
import collections
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

# ── Configuration ─────────────────────────────────────────────────────────────

SAMPLE_RATE  = 48_000   # Hz — BirdNET expects 48 kHz
CHANNELS     = 1
CLIP_SECONDS = 9        # length of each analysis window
STEP_SECONDS = 3        # slide window every N seconds (overlap)

# Tunables (via env) to help calibrate different mics/rooms:
SILENCE_THRESHOLD = float(os.environ.get("SILENCE_THRESHOLD", "0.008"))
MIN_CONFIDENCE    = float(os.environ.get("MIN_CONFIDENCE", "0.70"))
SNIPPETS_DIR      = Path("snippets")
DB_PATH           = Path("birdwatcher.db")

# Your location — used by BirdNET species range model
LATITUDE          = 47.6101  # Sammamish, WA
LONGITUDE         = -122.0326

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("birdwatcher")

# ── Database ──────────────────────────────────────────────────────────────────

INPUT_DEVICE_INDEX: int | None = None

def configure_audio_device():
    """
    Ensure we have a usable input device.
    Optionally force a device with AUDIO_INPUT_DEVICE=<index or substring>.
    """
    global INPUT_DEVICE_INDEX
    forced = os.environ.get("AUDIO_INPUT_DEVICE", "").strip()

    # Log what sounddevice thinks is default.
    try:
        log.info("Audio default device (in,out): %s", sd.default.device)
    except Exception:
        pass

    devices = sd.query_devices()

    def is_input_dev(d) -> bool:
        try:
            return int(d.get("max_input_channels", 0)) > 0
        except Exception:
            return False

    chosen = None
    if forced:
        # Accept either an index or a case-insensitive substring match.
        if forced.isdigit():
            idx = int(forced)
            if 0 <= idx < len(devices) and is_input_dev(devices[idx]):
                chosen = idx
        else:
            f = forced.lower()
            for i, d in enumerate(devices):
                if is_input_dev(d) and f in str(d.get("name", "")).lower():
                    chosen = i
                    break

        if chosen is None:
            log.warning("AUDIO_INPUT_DEVICE=%r did not match a usable input device; using default.", forced)

    if chosen is None:
        # Use sounddevice default input if possible.
        # sd.default.device is a sounddevice._InputOutputPair (not a list/tuple),
        # but supports indexing like one — so index it directly rather than
        # isinstance-checking for list/tuple, which never matches.
        default_device = sd.default.device
        if isinstance(default_device, int):
            chosen = default_device
        else:
            try:
                chosen = default_device[0]
            except (TypeError, IndexError):
                chosen = None

    # Final sanity check + log selected device details
    try:
        if chosen is None:
            raise RuntimeError("No input device selected")
        info = sd.query_devices(int(chosen))
        log.info("Using input device #%s: %s (%s in)", int(chosen), info.get("name"), info.get("max_input_channels"))
        if int(info.get("max_input_channels", 0)) <= 0:
            raise RuntimeError("Selected input device has 0 input channels")
        INPUT_DEVICE_INDEX = int(chosen)
    except Exception as exc:
        # Fall back: pick first device with input channels.
        for i, d in enumerate(devices):
            if is_input_dev(d):
                INPUT_DEVICE_INDEX = i
                log.warning("Falling back to first input device #%s (%s) due to: %s", i, d.get("name"), exc)
                break


def init_db():
    SNIPPETS_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS detections (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at   TEXT NOT NULL,
            common_name   TEXT NOT NULL,
            scientific_name TEXT,
            confidence    REAL NOT NULL,
            file_path     TEXT NOT NULL,
            latitude      REAL,
            longitude     REAL,
            duration_sec  REAL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    # Seed from env-configured defaults only if not already set, so a value
    # adjusted live (or by a previous run) survives across restarts.
    con.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('silence_threshold', ?)",
        (str(SILENCE_THRESHOLD),),
    )
    con.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('min_confidence', ?)",
        (str(MIN_CONFIDENCE),),
    )
    con.commit()
    con.close()
    log.info("Database ready: %s", DB_PATH)


# ── Live settings ─────────────────────────────────────────────────────────────
# SILENCE_THRESHOLD / MIN_CONFIDENCE are polled from the DB periodically so
# they can be adjusted from the dashboard without restarting the recorder.

SETTINGS_POLL_SECONDS = 3


def refresh_settings():
    global SILENCE_THRESHOLD, MIN_CONFIDENCE
    con = sqlite3.connect(DB_PATH)
    rows = dict(con.execute("SELECT key, value FROM settings").fetchall())
    con.close()

    if "silence_threshold" in rows:
        try:
            new_val = float(rows["silence_threshold"])
        except ValueError:
            new_val = None
        if new_val is not None and new_val != SILENCE_THRESHOLD:
            log.info("Silence threshold updated: %.4f → %.4f", SILENCE_THRESHOLD, new_val)
            SILENCE_THRESHOLD = new_val

    if "min_confidence" in rows:
        try:
            new_val = float(rows["min_confidence"])
        except ValueError:
            new_val = None
        if new_val is not None and new_val != MIN_CONFIDENCE:
            log.info("Min confidence updated: %.2f → %.2f", MIN_CONFIDENCE, new_val)
            MIN_CONFIDENCE = new_val


def settings_watcher():
    while True:
        time.sleep(SETTINGS_POLL_SECONDS)
        try:
            refresh_settings()
        except Exception as exc:
            log.warning("Settings refresh failed: %s", exc)


def save_detection(common_name, scientific_name, confidence, file_path, duration):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """INSERT INTO detections
           (detected_at, common_name, scientific_name, confidence,
            file_path, latitude, longitude, duration_sec)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            datetime.now().isoformat(timespec="seconds"),
            common_name,
            scientific_name,
            round(confidence, 4),
            str(file_path),
            LATITUDE,
            LONGITUDE,
            round(duration, 2),
        ),
    )
    con.commit()
    con.close()

# ── BirdNET analysis ──────────────────────────────────────────────────────────

def _summarize_detections(detections: list[dict], limit: int = 5) -> str:
    """Human-readable summary for terminal logs."""
    if not detections:
        return "0 detections"
    items = sorted(detections, key=lambda d: d.get("confidence", 0), reverse=True)[:limit]
    parts: list[str] = []
    for d in items:
        name = d.get("common_name") or d.get("scientific_name") or "Unknown"
        conf = float(d.get("confidence", 0.0))
        parts.append(f"{name} ({conf:.2f})")
    extra = "" if len(detections) <= limit else f" +{len(detections) - limit} more"
    return f"{len(detections)} detections: " + ", ".join(parts) + extra


def analyze_clip(wav_path: Path) -> list[dict]:
    """
    Run BirdNET-Analyzer on a WAV file.
    Returns list of dicts: {common_name, scientific_name, confidence, start_time, end_time}
    """
    try:
        from birdnetlib import Recording
        from birdnetlib.analyzer import Analyzer

        # Analyzer is cached as a module-level singleton after first load
        if not hasattr(analyze_clip, "_analyzer"):
            log.info("Loading BirdNET model (first run — ~10 s)…")
            analyze_clip._analyzer = Analyzer()
            log.info("BirdNET model loaded.")

        try:
            size_kb = wav_path.stat().st_size / 1024
            log.info("Analyzing clip with BirdNET (%0.0f KB): %s", size_kb, wav_path.name)
        except Exception:
            log.info("Analyzing clip with BirdNET: %s", wav_path.name)

        recording = Recording(
            analyze_clip._analyzer,
            str(wav_path),
            lat=LATITUDE,
            lon=LONGITUDE,
            date=datetime.now(),
            min_conf=MIN_CONFIDENCE,
        )
        recording.analyze()
        detections = recording.detections or []
        log.info("BirdNET result → %s", _summarize_detections(detections))
        return detections

    except Exception as exc:
        log.error("BirdNET error: %s", exc)
        return []

# ── Clip saving ───────────────────────────────────────────────────────────────

def save_snippet(audio: np.ndarray, detections: list[dict]):
    """Persist the audio clip and write DB records for each detection."""
    if not detections:
        return

    # Use a hash of audio content to avoid duplicate filenames
    digest = hashlib.md5(audio.tobytes()).hexdigest()[:8]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # One file per clip, named after the highest-confidence detection
    top = max(detections, key=lambda d: d["confidence"])
    slug = top["common_name"].lower().replace(" ", "-")
    filename = f"{ts}_{slug}_{digest}.wav"
    out_path = SNIPPETS_DIR / filename

    sf.write(str(out_path), audio, SAMPLE_RATE, subtype="PCM_16")
    duration = len(audio) / SAMPLE_RATE

    for det in detections:
        if det["confidence"] >= MIN_CONFIDENCE:
            save_detection(
                common_name    = det.get("common_name", "Unknown"),
                scientific_name= det.get("scientific_name", ""),
                confidence     = det["confidence"],
                file_path      = out_path,
                duration       = duration,
            )
            log.info(
                "🐦  %-28s  conf=%.2f  → %s",
                det["common_name"],
                det["confidence"],
                filename,
            )

# ── Analysis worker thread ────────────────────────────────────────────────────

analysis_queue: queue.Queue = queue.Queue(maxsize=10)

def analysis_worker():
    """Consume (audio_array, tmp_wav_path) from queue, run BirdNET, save."""
    while True:
        item = analysis_queue.get()
        if item is None:
            break
        audio, tmp_path = item
        log.info("Analysis worker picked up clip: %s", tmp_path.name)
        try:
            detections = analyze_clip(tmp_path)
            if detections:
                save_snippet(audio, detections)
            else:
                log.info("No birds detected in clip.")
        finally:
            # Clean up the temp file
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            analysis_queue.task_done()

# ── Main recorder loop ────────────────────────────────────────────────────────

def rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(audio ** 2)))


def record_loop():
    """
    Continuously read from the microphone.
    Accumulate audio in a rolling buffer.
    Every STEP_SECONDS, if RMS > threshold, dispatch a CLIP_SECONDS window
    to the analysis queue.
    """
    clip_samples = CLIP_SECONDS * SAMPLE_RATE
    step_samples = STEP_SECONDS * SAMPLE_RATE

    # Ring buffer: keep last CLIP_SECONDS of audio at all times
    ring = collections.deque(maxlen=clip_samples)
    samples_since_dispatch = 0
    tmp_counter = 0

    def audio_callback(indata, frames, time_info, status):
        nonlocal samples_since_dispatch, tmp_counter

        if status:
            log.warning("Audio status: %s", status)

        chunk = indata[:, 0].copy()  # mono
        ring.extend(chunk)
        samples_since_dispatch += len(chunk)

        # Every STEP_SECONDS, check if we have enough audio and it's not silent
        if samples_since_dispatch >= step_samples:
            samples_since_dispatch = 0

            if len(ring) < clip_samples:
                return  # not enough buffered yet

            clip = np.array(ring, dtype=np.float32)
            clip_rms = rms(clip)
            clip_peak = float(np.max(np.abs(clip))) if len(clip) else 0.0

            if clip_rms < SILENCE_THRESHOLD:
                log.info(
                    "Silence (rms=%.4f peak=%.4f < %.4f) — skipping",
                    clip_rms,
                    clip_peak,
                    SILENCE_THRESHOLD,
                )
                return

            log.info("Sound detected (rms=%.4f) — queuing %ds clip", clip_rms, CLIP_SECONDS)

            # Write clip to a temp WAV for BirdNET
            tmp_counter += 1
            tmp_path = SNIPPETS_DIR / f"_tmp_{tmp_counter}.wav"
            try:
                sf.write(str(tmp_path), clip, SAMPLE_RATE, subtype="PCM_16")
            except Exception as exc:
                log.error("Failed to write temp clip: %s", exc)
                return

            try:
                analysis_queue.put_nowait((clip, tmp_path))
                log.info("Queued clip for analysis: %s (queue=%d)", tmp_path.name, analysis_queue.qsize())
            except queue.Full:
                log.warning("Analysis queue full — dropping clip")
                tmp_path.unlink(missing_ok=True)

    log.info("Starting microphone capture (device: default, %d Hz)…", SAMPLE_RATE)
    log.info("Silence threshold RMS=%.3f  |  Min confidence=%.0f%%", SILENCE_THRESHOLD, MIN_CONFIDENCE * 100)
    log.info("Listening… (Ctrl+C to stop)")

    with sd.InputStream(
        device=INPUT_DEVICE_INDEX,
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="float32",
        blocksize=int(SAMPLE_RATE * 0.1),  # 100 ms blocks
        callback=audio_callback,
    ):
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            log.info("Stopping recorder…")

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    init_db()
    configure_audio_device()

    # Start background analysis thread
    worker = threading.Thread(target=analysis_worker, daemon=True)
    worker.start()

    # Start background settings-poll thread (picks up live dashboard edits)
    settings_thread = threading.Thread(target=settings_watcher, daemon=True)
    settings_thread.start()

    try:
        record_loop()
    finally:
        # Drain queue cleanly
        analysis_queue.put(None)
        try:
            worker.join(timeout=30)
        except KeyboardInterrupt:
            # Allow Ctrl+C to exit cleanly even if analysis is still running.
            pass
        log.info("BirdWatcher stopped.")


if __name__ == "__main__":
    main()
