# SMP leaderboard watch

Checks the Money SMP leaderboard (https://www.smp.money/leaderboard/current) every 30 seconds
and logs every change in a player's money.

## Files

| File | What it is |
| --- | --- |
| `data/changes.json` | Every change found, oldest first, one per line. `type` is `money`, `stats` (kills/deaths/tier/team), `joined`, `left` or `event`. |
| `data/latest.json` | Latest copy of the leaderboard (ranked board, team totals and the exact API payload). |
| `data/status.json` | Heartbeat: last check time, check counts, last error. |
| `watcher.py` | The script. |
| `.github/workflows/watch.yml` | Runs the script on GitHub Actions. |

A money record looks like this:

```json
{"at": "2026-11-05T06:00:00Z", "atIST": "2026-11-05 11:30:00", "player": "ConixD", "team": "Purple Aces", "type": "money", "from": 60, "to": 260, "delta": 200, "rankFrom": 36, "rankTo": 1, "flags": ["big_move", "rank_jump"]}
```

Flags: `big_move` (50 or more in one check), `rank_jump` (5 or more places), `while_offline` (money moved while the player was offline).

The commit history is the update feed. Each data commit is titled like `11:30 IST: ConixD +200 ($260), Flowtives -10 ($219)`.

## How it runs

- smp.money blocks plain scripts (Vercel bot check), so the script opens the page in headless Chrome once,
  then calls the site's own API from inside the page every 30 s.
- A GitHub job can run for 6 hours at most. Each run watches for about 5h40m, saves, then queues the next run.
  An hourly schedule is the backup starter. Only one run is active at a time.
- Playtime and online/offline flips are not logged (they change all the time).

## Controls

- Start now: Actions > SMP leaderboard watch > Run workflow.
- Stop: Actions > SMP leaderboard watch > "..." > Disable workflow, then cancel the running job.
