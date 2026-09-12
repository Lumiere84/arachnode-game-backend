# ARACHNODE: Capture & Quarantine — Game Backend

A small Flask + SQLite API that gives the game a global leaderboard and
a prize-code bank for the in-game "fastest defender" Discernment Key
reward. This is separate infrastructure from the main ARACHNODE product
backend -- nothing here touches the real client Discernment Key Bank
(DK0000-DK9999). This game's prize pool uses its own GK0000-GK9999
codes so playing never spends a real client login code.

## Run it locally

```
pip install -r requirements.txt
python app.py
```

The API comes up at http://localhost:8080/

Or with Docker:

```
docker compose up --build
```

## Endpoints

- GET /health -- liveness check
- POST /leaderboard/submit -- submit a finished run: player_name, score, round_reached, result, keys_earned
- GET /leaderboard/top?limit=10 -- top scores, highest first
- POST /prize/claim -- claim the next unused GK#### game-prize code
- GET /prize/status -- how many game-prize codes are left

result must be "WIN" or "LOSS". All fields are validated and clamped
server-side (name length, integer ranges) before being stored.

## Deploying it (same pattern as the ARACHNODE demo)

1. This repo is ready to connect directly on Render.
2. On Render: New -> Web Service, connect this repo, set the runtime to
   Docker (Render sometimes auto-detects wrong, override it if so).
3. No environment variables are required to get started --
   GAME_DATABASE_PATH already defaults to /data/game.db inside the
   container.
4. Add a persistent disk mounted at /data (Render: the service's Disks
   tab) so the leaderboard and prize codes survive redeploys -- without
   it, a new deploy starts the database over from empty.
5. Once it's live, you'll have a URL like
   https://your-service.onrender.com. That's the leaderboard_url value
   the game's config.cfg needs.

## Security notes before a real public launch

This is a working MVP, not a hardened public API. Before shipping it to
real players, you'll want to add rate limiting on /leaderboard/submit
and /prize/claim, a basic anti-cheat check on submitted scores, and
tighter CORS if you build a companion website leaderboard viewer.

