# 🐦 BirdWatcher — Local Birdsong Monitor

Real-time birdsong detection and snippet saving, running 100% locally on your Mac.
Uses BirdNET-Analyzer (Cornell Lab) for identification and a web dashboard for browsing results.

## Quick Start

```bash
# 1. Run the installer (first time only)
chmod +x install.sh && ./install.sh

# 2. Start everything
./start.sh

# 3. Open the dashboard
open http://localhost:5000
```

## Project Structure

```
birdwatcher/
├── install.sh          # One-time setup (Python venv, deps, ffmpeg)
├── start.sh            # Start recorder + dashboard together
├── recorder.py         # Mic capture → VAD → clip → BirdNET → save
├── dashboard.py        # Flask web dashboard (API + UI)
├── templates/
│   └── index.html      # Dashboard frontend
├── snippets/           # Saved .wav clips (auto-created)
└── birdwatcher.db      # SQLite log (auto-created)
```

## Configuration (top of recorder.py)

| Setting | Default | Description |
|---|---|---|
| `MIN_CONFIDENCE` | 0.70 | Discard detections below this score |
| `CLIP_SECONDS` | 9 | Length of each analysed clip |
| `SILENCE_THRESHOLD` | 0.01 | RMS level below which = silence |
| `LATITUDE / LONGITUDE` | Sammamish, WA | Used by BirdNET species range filter |

## Stopping

Press `Ctrl+C` in the terminal running `start.sh`.

## Requirements

- macOS (Apple Silicon M1/M2/M3)
- Python 3.11
- uv (installed by `install.sh` unless `SKIP_UV_INSTALL=1`)
- ffmpeg (installed by install.sh via Homebrew)
- ~500 MB disk for BirdNET model weights (downloaded on first run)
