#!/usr/bin/env bash
# BirdWatcher installer for macOS Apple Silicon
set -e

PYTHON_VERSION="3.11"

echo ""
echo "🐦  BirdWatcher Installer"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Allow skipping Homebrew installs (useful in restricted environments)
SKIP_BREW="${SKIP_BREW:-0}"

# Allow skipping uv installation (if you already have it)
SKIP_UV_INSTALL="${SKIP_UV_INSTALL:-0}"

# Prefer a project-local venv for tooling that expects it
# (uv will also use this when UV_PROJECT_ENVIRONMENT is set)
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$(pwd)/.venv}"

# Allow skipping Homebrew installs (useful in restricted environments)
# ── 1. Homebrew ───────────────────────────────────────────────────────────────
if [ "$SKIP_BREW" = "1" ]; then
  echo "↷  Skipping Homebrew installs (SKIP_BREW=1)"
else
  if ! command -v brew &>/dev/null; then
    echo "➤  Installing Homebrew…"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Add brew to PATH for Apple Silicon
    eval "$(/opt/homebrew/bin/brew shellenv)"
  else
    echo "✓  Homebrew found"
  fi
fi

# ── 2. uv (Python deps manager) ────────────────────────────────────────────────
if [ "$SKIP_UV_INSTALL" = "1" ]; then
  echo "↷  Skipping uv install (SKIP_UV_INSTALL=1)"
else
  if command -v uv &>/dev/null; then
    echo "✓  uv found"
  else
    echo "➤  Installing uv…"
    # Prefer official installer (works without Homebrew)
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Typical install location for the script above
    export PATH="$HOME/.cargo/bin:$PATH"
  fi
fi

# ── 2. ffmpeg ─────────────────────────────────────────────────────────────────
if [ "$SKIP_BREW" != "1" ]; then
  if ! command -v ffmpeg &>/dev/null; then
    echo "➤  Installing ffmpeg…"
    if ! brew install ffmpeg; then
      echo "⚠️  Could not install ffmpeg via Homebrew (continuing)."
    fi
  else
    echo "✓  ffmpeg found"
  fi
fi

# ── 3. PortAudio (needed by PyAudio / sounddevice) ────────────────────────────
if [ "$SKIP_BREW" != "1" ] && command -v brew &>/dev/null; then
  if ! brew list portaudio &>/dev/null 2>&1; then
    echo "➤  Installing portaudio…"
    if ! brew install portaudio; then
      echo "⚠️  Could not install portaudio via Homebrew (continuing)."
      echo "   If audio capture fails, try: sudo chown -R \"$(whoami)\" /opt/homebrew/Cellar"
    fi
  else
    echo "✓  portaudio found"
  fi
fi

# ── 4. Python + deps (uv) ──────────────────────────────────────────────────────
if ! command -v uv &>/dev/null; then
  echo "❌  uv is not available on PATH."
  echo "   Re-run without SKIP_UV_INSTALL=1, or install uv and try again."
  exit 1
fi

echo "➤  Syncing Python environment (uv)…"
uv sync --python "${PYTHON_VERSION}"

echo ""
echo "✅  Installation complete!"
echo ""
echo "   Run './start.sh' to begin monitoring."
echo ""
