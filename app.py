"""
ARACHNODE: Capture & Quarantine -- game backend

A small Flask + sqlite service providing:
  - a global leaderboard (submit / top scores)
  - a "prize code" bank for the in-game Discernment Key reward
    (GK0000-GK9999 -- a GAME prize pool, kept entirely separate from
    ARACHNODE's real client-facing Discernment Key Bank / demo login
    codes, so playing the game never consumes real client credentials)
  - Discernment Academy: lesson-based Allow/Block/Escalate training
    scenarios, mirroring real ARACHNODE Agent Security Gateway policy
    calls, with per-lesson mastery keys and a Master Discernment Key
    once every seeded lesson has been answered correctly

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
VALID_ACTIONS = {"ALLOW", "BLOCK", "ESCALATE"}
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

CREATE TABLE IF NOT EXISTS academy_lessons (
    lesson_id TEXT PRIMARY KEY,
    sort_order INTEGER NOT NULL,
    title TEXT NOT NULL,
    scenario_text TEXT NOT NULL,
    policy_text TEXT NOT NULL,
    correct_action TEXT NOT NULL,
    explanation TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS academy_progress (
    player_name TEXT NOT NULL,
    lesson_id TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    correct INTEGER NOT NULL DEFAULT 0,
    last_action TEXT,
    completed_at TEXT,
    PRIMARY KEY (player_name, lesson_id)
);

CREATE TABLE IF NOT EXISTS academy_keys (
    player_name TEXT NOT NULL,
    lesson_id TEXT NOT NULL,
    earned_at TEXT NOT NULL,
    PRIMARY KEY (player_name, lesson_id)
);
"""
# Seed content: real ARACHNODE-style intercepted agent tool calls. Each
# teaches one Allow / Block / Escalate judgment. Add more rows here as
# the lesson set grows -- sort_order controls display order, lesson_id
# must stay stable once players have progress recorded against it.
ACADEMY_LESSONS = [
    {
        "lesson_id": "L01-read-status",
        "sort_order": 1,
        "title": "Public Status Page Read",
        "scenario_text": (
            "An AI agent calls GET /status on the company's public status "
            "page to check current uptime before starting a task."
        ),
        "policy_text": (
            "Policy: read-only calls against public, non-sensitive "
            "endpoints are low risk and do not require review."
        ),
        "correct_action": "ALLOW",
        "explanation": (
            "Read-only, public data, no side effects -- this is exactly "
            "the kind of call ARACHNODE's policy engine clears "
            "automatically so agents aren't slowed down by safe requests."
        ),
    },
    {
        "lesson_id": "L02-drop-table",
        "sort_order": 2,
        "title": "Drop Production Table",
        "scenario_text": (
            "An AI agent calls DROP TABLE customers on the production "
            "database as part of a 'cleanup' task."
        ),
        "policy_text": (
            "Policy: destructive, irreversible operations against "
            "production data stores are never auto-approved."
        ),
        "correct_action": "BLOCK",
        "explanation": (
            "Irreversible data loss on production, with no upside that "
            "justifies the risk. ARACHNODE blocks this outright rather "
            "than even routing it to a human -- there's no legitimate "
            "'cleanup' that needs this."
        ),
    },
    {
        "lesson_id": "L03-restart-staging",
        "sort_order": 3,
        "title": "Restart Staging Server",
        "scenario_text": (
            "An AI agent calls restart_service('staging-web-01') to "
            "recover from a crashed process."
        ),
        "policy_text": (
            "Policy: service restarts in non-production environments are "
            "low risk and reversible."
        ),
        "correct_action": "ALLOW",
        "explanation": (
            "Staging, not production; a restart is a normal recovery "
            "action with no lasting impact. Auto-approving keeps agents "
            "productive on low-stakes infrastructure."
        ),
    },
    {
        "lesson_id": "L04-adjust-billing",
        "sort_order": 4,
        "title": "Adjust Customer Billing",
        "scenario_text": (
            "An AI agent calls adjust_balance(customer_id, -500.00) to "
            "issue what it determined was an appropriate refund."
        ),
        "policy_text": (
            "Policy: direct financial adjustments to customer accounts "
            "require human sign-off, even when the agent's reasoning "
            "looks sound."
        ),
        "correct_action": "ESCALATE",
        "explanation": (
            "Real money, real customer impact, and the agent's judgment "
            "hasn't been verified by a person. This is the textbook case "
            "for the Master Discernment Key human-approval step -- not an "
            "auto-block, since the refund may well be legitimate, but not "
            "an auto-allow either."
        ),
    },
    {
        "lesson_id": "L05-export-pii",
        "sort_order": 5,
        "title": "Export Customer PII Externally",
        "scenario_text": (
            "An AI agent calls export_customers(format='csv', "
            "destination='external-partner@example.com') to share a "
            "full customer data export."
        ),
        "policy_text": (
            "Policy: bulk export of personally identifiable information "
            "to an external destination is treated as a potential "
            "exfiltration event."
        ),
        "correct_action": "BLOCK",
        "explanation": (
            "Bulk PII leaving the company to an external address is "
            "exactly the pattern ARACHNODE watches for, whether the "
            "agent's intent was benign or not. Block first; a legitimate "
            "data-sharing need goes through a separate, audited process "
            "-- not an agent's unilateral tool call."
        ),
    },
]
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


def ensure_academy_lessons_seeded():
    """Idempotent: inserts/updates the seed lesson set by lesson_id.
    Safe to redeploy after editing ACADEMY_LESSONS above -- existing
    player progress (keyed by lesson_id) is untouched."""
    with _db_lock:
        db = get_db()
        db.executemany(
            "INSERT INTO academy_lessons "
            "(lesson_id, sort_order, title, scenario_text, policy_text, correct_action, explanation) "
            "VALUES (:lesson_id, :sort_order, :title, :scenario_text, :policy_text, :correct_action, :explanation) "
            "ON CONFLICT(lesson_id) DO UPDATE SET "
            "sort_order=excluded.sort_order, title=excluded.title, "
            "scenario_text=excluded.scenario_text, policy_text=excluded.policy_text, "
            "correct_action=excluded.correct_action, explanation=excluded.explanation",
            ACADEMY_LESSONS,
        )
        db.commit()
        db.close()


def _now() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def _clean_player_name(raw) -> str:
    player_name = str(raw or "").strip() or "ANON"
    if not NAME_RE.match(player_name):
        player_name = re.sub(r"[^A-Za-z0-9 _\-]", "", player_name)[:24] or "ANON"
    return player_name

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
    ensure_academy_lessons_seeded()

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.post("/leaderboard/submit")
    def submit_score():
        payload = request.get_json(silent=True) or {}

        player_name = _clean_player_name(payload.get("player_name"))

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

    # ---- Discernment Academy -------------------------------------------

    @app.get("/academy/lessons")
    def academy_lessons():
        """Lesson list for the front end. correct_action and explanation
        are withheld here on purpose -- they're only revealed in the
        response to POST /academy/progress, after the player answers."""
        db = get_db()
        rows = db.execute(
            "SELECT lesson_id, sort_order, title, scenario_text, policy_text "
            "FROM academy_lessons ORDER BY sort_order ASC"
        ).fetchall()
        db.close()
        return jsonify([dict(r) for r in rows])

    @app.post("/academy/progress")
    def academy_progress():
        payload = request.get_json(silent=True) or {}

        player_name = _clean_player_name(payload.get("player_name"))
        lesson_id = str(payload.get("lesson_id", "")).strip()
        chosen_action = str(payload.get("chosen_action", "")).upper().strip()

        if chosen_action not in VALID_ACTIONS:
            return jsonify({"error": f"chosen_action must be one of {sorted(VALID_ACTIONS)}"}), 400

        db = get_db()
        lesson = db.execute(
            "SELECT lesson_id, correct_action, explanation FROM academy_lessons WHERE lesson_id = ?",
            (lesson_id,),
        ).fetchone()
        if lesson is None:
            db.close()
            return jsonify({"error": "unknown lesson_id"}), 404

        is_correct = chosen_action == lesson["correct_action"]
        key_earned_now = False

        with _db_lock:
            db.execute(
                "INSERT INTO academy_progress (player_name, lesson_id, attempts, correct, last_action, completed_at) "
                "VALUES (?, ?, 1, ?, ?, ?) "
                "ON CONFLICT(player_name, lesson_id) DO UPDATE SET "
                "attempts = attempts + 1, "
                "correct = MAX(correct, excluded.correct), "
                "last_action = excluded.last_action, "
                "completed_at = CASE WHEN excluded.correct = 1 THEN excluded.completed_at ELSE completed_at END",
                (player_name, lesson_id, int(is_correct), chosen_action, _now() if is_correct else None),
            )

            if is_correct:
                existing_key = db.execute(
                    "SELECT 1 FROM academy_keys WHERE player_name = ? AND lesson_id = ?",
                    (player_name, lesson_id),
                ).fetchone()
                if existing_key is None:
                    db.execute(
                        "INSERT INTO academy_keys (player_name, lesson_id, earned_at) VALUES (?, ?, ?)",
                        (player_name, lesson_id, _now()),
                    )
                    key_earned_now = True

            db.commit()
            db.close()

        mastery = _mastery_summary(player_name)

        return jsonify(
            {
                "correct": is_correct,
                "correct_action": lesson["correct_action"],
                "explanation": lesson["explanation"],
                "key_earned_this_attempt": key_earned_now,
                "mastery": mastery,
            }
        )

    @app.get("/academy/mastery")
    def academy_mastery():
        player_name = _clean_player_name(request.args.get("player_name"))
        return jsonify(_mastery_summary(player_name))

    def _mastery_summary(player_name: str) -> dict:
        db = get_db()
        total_lessons = db.execute("SELECT COUNT(*) AS n FROM academy_lessons").fetchone()["n"]
        keys_earned = db.execute(
            "SELECT COUNT(*) AS n FROM academy_keys WHERE player_name = ?", (player_name,)
        ).fetchone()["n"]
        db.close()
        return {
            "player_name": player_name,
            "total_lessons": total_lessons,
            "keys_earned": keys_earned,
            "master_discernment_key_earned": total_lessons > 0 and keys_earned >= total_lessons,
        }

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
