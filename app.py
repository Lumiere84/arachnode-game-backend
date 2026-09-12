"""
ARACHNODE: Capture & Quarantine -- game backend

A small Flask + sqlite service providing:
  - a global leaderboard (submit / top scores)
  - a "prize code" bank for the in-game Discernment Key reward
    (GK0000-GK9999 -- a GAME prize pool, kept entirely separate from
    ARACHNODE's real client-facing Discernment Key Bank / demo login
    codes, so playing the game never consumes real client credentials)

Run locally:
    pip install -r requirements.txt
    python app.py
    # API at http://localhost:8080/

Deploy: same pattern as the ARACHNODE demo -- Docker image, Render web
service, env vars for config. See README.md.
"""

import datetime
import os
import re
import sqlite3
import threading

from flask import Flask, jsonify, request

DATABASE_PATH = os.environ.get("GAME_DATABASE_PATH", "/data/game.db")
PORT = int(os.environ.get("PORT", "8080"))

PRIZE_CODE_COUNT = 10000
VALID_RESULTS = {"WIN", "LOSS"}
NAME_RE = re.compile(r"^[A-Za-z0-9 _\-]{1,24}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS leaderboard (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_name TEXT NOT NULL,
    score INTEGER NOT NULL,
    round_reached INTEGER NOT NULL,
    result TEXT NOT NULL,
    keys_earned INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prize_codes (
    code TEXT PRIMARY KEY,
    claimed_at TEXT
);
"""

_db_lock = threading.Lock()


def get_db():
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db_lock:
        db = get_db()
        db.executescript(SCHEMA)
        db.commit()
        db.close()


def ensure_prize_codes_seeded():
    """Idempotent: inserts GK0000..GK9999 if they don't already exist.
    Never touches a code already in the table, so claimed codes keep
    their history across restarts and redeploys."""
    with _db_lock:
        db = get_db()
        db.executemany(
            "INSERT OR IGNORE INTO prize_codes (code) VALUES (?)",
            [(f"GK{i:04d}",) for i in range(PRIZE_CODE_COUNT)],
        )
        db.commit()
        db.close()


def _now() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def create_app() -> Flask:
    app = Flask(__name__)

    @app.after_request
    def add_cors_headers(resp):
        # Public read/write API for a game leaderboard -- no cookies or
        # credentials are involved, so a permissive CORS policy is fine.
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp

    @app.route("/<path:_any>", methods=["OPTIONS"])
    def cors_preflight(_any):
        return ("", 204)

    init_db()
    ensure_prize_codes_seeded()

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.post("/leaderboard/submit")
    def submit_score():
        payload = request.get_json(silent=True) or {}

        player_name = str(payload.get("player_name", "")).strip() or "ANON"
        if not NAME_RE.match(player_name):
            player_name = re.sub(r"[^A-Za-z0-9 _\-]", "", player_name)[:24] or "ANON"

        try:
            score = int(payload.get("score", 0))
            round_reached = int(payload.get("round_reached", 0))
            keys_earned = int(payload.get("keys_earned", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "score, round_reached, and keys_earned must be integers"}), 400

        result = str(payload.get("result", "")).upper()
        if result not in VALID_RESULTS:
            return jsonify({"error": f"result must be one of {sorted(VALID_RESULTS)}"}), 400

        score = max(0, min(score, 1_000_000))
        round_reached = max(0, min(round_reached, 1000))
        keys_earned = max(0, min(keys_earned, 1000))

        with _db_lock:
            db = get_db()
            cur = db.execute(
                "INSERT INTO leaderboard (player_name, score, round_reached, result, keys_earned, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (player_name, score, round_reached, result, keys_earned, _now()),
            )
            db.commit()
            new_id = cur.lastrowid
            rank_row = db.execute(
                "SELECT COUNT(*) AS higher FROM leaderboard WHERE score > ?", (score,)
            ).fetchone()
            db.close()

        rank = rank_row["higher"] + 1
        return jsonify({"id": new_id, "rank": rank}), 201

    @app.get("/leaderboard/top")
    def top_scores():
        try:
            limit = int(request.args.get("limit", 10))
        except ValueError:
            limit = 10
        limit = max(1, min(limit, 100))

        db = get_db()
        rows = db.execute(
            "SELECT player_name, score, round_reached, result, keys_earned, created_at "
            "FROM leaderboard ORDER BY score DESC, created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        db.close()
        return jsonify([dict(r) for r in rows])

    @app.post("/prize/claim")
    def claim_prize():
        """Issues the next unused game-prize code. This pool (GK-prefixed)
        is separate from ARACHNODE's real client Discernment Key Bank
        (DK-prefixed) on the main product -- claiming a game prize never
        touches or consumes a real client login code."""
        with _db_lock:
            db = get_db()
            row = db.execute(
                "SELECT code FROM prize_codes WHERE claimed_at IS NULL ORDER BY code LIMIT 1"
            ).fetchone()
            if row is None:
                db.close()
                return jsonify({"error": "prize_pool_exhausted"}), 410
            code = row["code"]
            db.execute("UPDATE prize_codes SET claimed_at = ? WHERE code = ?", (_now(), code))
            db.commit()
            db.close()
        return jsonify({"code": code})

    @app.get("/prize/status")
    def prize_status():
        db = get_db()
        row = db.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN claimed_at IS NULL THEN 1 ELSE 0 END) AS unclaimed "
            "FROM prize_codes"
        ).fetchone()
        db.close()
        return jsonify({"total": row["total"], "unclaimed": row["unclaimed"] or 0})

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)

