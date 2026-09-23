"""
dashboard.py — BirdWatcher web dashboard

Serves:
  GET /                     → HTML dashboard
  GET /api/detections       → JSON list of detections (paginated, filterable)
  GET /api/stats            → summary counts
  GET /api/snippet/<id>     → stream the WAV file for playback
  DELETE /api/detection/<id>→ delete a detection + its file
"""

import os
import json
import sqlite3
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta

from flask import Flask, jsonify, request, send_file, render_template, abort
from flask_cors import CORS

DB_PATH      = Path("birdwatcher.db")
SNIPPETS_DIR = Path("snippets")

app = Flask(__name__, template_folder="templates")
CORS(app)

# ── Species info (Wikipedia summary — no API key required) ────────────────────

_species_info_cache: dict[str, dict] = {}


def _wikipedia_summary(title: str) -> dict | None:
    """Fetch a page summary + thumbnail from Wikipedia's REST API, or None if unavailable."""
    url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(
        title.strip().replace(" ", "_")
    )
    req = urllib.request.Request(
        url, headers={"User-Agent": "BirdWatcher/0.1 (local birdwatching dashboard)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None

    if data.get("type") == "disambiguation":
        return None

    thumbnail = (data.get("thumbnail") or {}).get("source")
    original  = (data.get("originalimage") or {}).get("source")
    page_url  = ((data.get("content_urls") or {}).get("desktop") or {}).get("page")

    return {
        "found": True,
        "title": data.get("title"),
        "extract": data.get("extract"),
        "thumbnail": thumbnail or original,
        "wiki_url": page_url,
    }

# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def ensure_db():
    """Create DB if it doesn't exist yet (dashboard started before recorder)."""
    if not DB_PATH.exists():
        con = sqlite3.connect(DB_PATH)
        con.execute("""
            CREATE TABLE IF NOT EXISTS detections (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                detected_at     TEXT NOT NULL,
                common_name     TEXT NOT NULL,
                scientific_name TEXT,
                confidence      REAL NOT NULL,
                file_path       TEXT NOT NULL,
                latitude        REAL,
                longitude       REAL,
                duration_sec    REAL
            )
        """)
        con.commit()
        con.close()

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/detections")
def api_detections():
    limit  = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))
    search = request.args.get("q", "").strip()
    since  = request.args.get("since", "")  # ISO datetime string

    con = get_db()
    params = []
    where_clauses = []

    if search:
        where_clauses.append("common_name LIKE ?")
        params.append(f"%{search}%")
    if since:
        where_clauses.append("detected_at >= ?")
        params.append(since)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    rows = con.execute(
        f"""SELECT id, detected_at, common_name, scientific_name,
                   confidence, file_path, latitude, longitude, duration_sec
            FROM detections
            {where_sql}
            ORDER BY detected_at DESC
            LIMIT ? OFFSET ?""",
        [*params, limit, offset],
    ).fetchall()

    total = con.execute(
        f"SELECT COUNT(*) FROM detections {where_sql}", params
    ).fetchone()[0]
    con.close()

    detections = []
    for row in rows:
        d = dict(row)
        d["has_audio"] = Path(d["file_path"]).exists()
        detections.append(d)

    return jsonify({"detections": detections, "total": total, "offset": offset})


@app.route("/api/stats")
def api_stats():
    con = get_db()

    total = con.execute("SELECT COUNT(*) FROM detections").fetchone()[0]
    species_count = con.execute(
        "SELECT COUNT(DISTINCT common_name) FROM detections"
    ).fetchone()[0]

    today = datetime.now().date().isoformat()
    today_count = con.execute(
        "SELECT COUNT(*) FROM detections WHERE detected_at >= ?", (today,)
    ).fetchone()[0]

    top_species = con.execute(
        """SELECT common_name, COUNT(*) as cnt, MAX(confidence) as best_conf
           FROM detections
           GROUP BY common_name
           ORDER BY cnt DESC
           LIMIT 10"""
    ).fetchall()

    recent_24h = con.execute(
        """SELECT common_name, detected_at, confidence, id
           FROM detections
           WHERE detected_at >= ?
           ORDER BY detected_at DESC
           LIMIT 20""",
        ((datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds"),),
    ).fetchall()

    con.close()

    return jsonify({
        "total_detections": total,
        "unique_species": species_count,
        "today_detections": today_count,
        "top_species": [dict(r) for r in top_species],
        "recent_24h": [dict(r) for r in recent_24h],
    })


@app.route("/api/snippet/<int:detection_id>")
def api_snippet(detection_id):
    con = get_db()
    row = con.execute(
        "SELECT file_path FROM detections WHERE id = ?", (detection_id,)
    ).fetchone()
    con.close()

    if not row:
        abort(404)

    path = Path(row["file_path"])
    if not path.exists():
        abort(404)

    return send_file(str(path.resolve()), mimetype="audio/wav", as_attachment=False)


@app.route("/api/species/<name>")
def api_species_info(name):
    sci_name = request.args.get("sci", "").strip()
    cache_key = name.lower()

    if cache_key in _species_info_cache:
        return jsonify(_species_info_cache[cache_key])

    result = _wikipedia_summary(name)
    if result is None and sci_name:
        result = _wikipedia_summary(sci_name)

    if result is None:
        result = {"found": False, "title": name, "extract": None, "thumbnail": None, "wiki_url": None}

    _species_info_cache[cache_key] = result
    return jsonify(result)


@app.route("/api/detection/<int:detection_id>", methods=["DELETE"])
def api_delete_detection(detection_id):
    con = get_db()
    row = con.execute(
        "SELECT file_path FROM detections WHERE id = ?", (detection_id,)
    ).fetchone()

    if not row:
        con.close()
        abort(404)

    # Remove file if it exists
    path = Path(row["file_path"])
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass

    con.execute("DELETE FROM detections WHERE id = ?", (detection_id,))
    con.commit()
    con.close()

    return jsonify({"deleted": detection_id})


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ensure_db()
    print("🐦  BirdWatcher Dashboard → http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
