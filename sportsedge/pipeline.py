"""Update steps: pull schedules and scores, box scores, and current lines."""
import configparser
import time
from datetime import timedelta
from pathlib import Path

from . import db as D
from . import espn, odds
from .leagues import LEAGUES, season_for, season_window
from .util import now_utc, parse_iso

DEFAULTS = {
    "general": {"leagues": "nfl,nba,nhl", "timezone": "America/Detroit", "database": "sportsedge.db",
                "report": "report.html"},
    "odds": {"source": "espn", "api_key": "", "regions": "us", "bookmakers": "", "markets": "spreads,totals,h2h"},
    "targets": {"price": "-200", "band_low": "-260", "band_high": "-160"},
    "model": {"market_blend": "0.3", "min_ev": "0.02", "kelly_fraction": "0.25", "seasons_back": "1"},
}


def load_config(path):
    cp = configparser.ConfigParser()
    cp.read_dict(DEFAULTS)
    if Path(path).exists():
        cp.read(path)
    return cp


def model_cfg(cp):
    from . import injuries
    return {"market_blend": cp.getfloat("model", "market_blend"),
            "min_ev": cp.getfloat("model", "min_ev"),
            "kelly_fraction": cp.getfloat("model", "kelly_fraction"),
            "target_price": cp.getint("targets", "price"),
            "band_low": cp.getint("targets", "band_low"),
            "band_high": cp.getint("targets", "band_high"),
            "injuries": injuries.config(cp)}


def log(msg):
    print(msg, flush=True)


def _months(start, end):
    """Calendar month chunks covering [start, end]."""
    cur = start
    while cur <= end:
        nxt = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        yield cur, min(nxt - timedelta(days=1), end)
        cur = nxt


def _schedule_daily(con, league, season, seasons_back, full, espn_odds, now):
    """NBA/NHL: pull a month at a time (falls back to day by day if ESPN won't do ranges)."""
    odds_rows = []
    for s in range(season - seasons_back, season + 1):
        done_key = f"{league.key}:{s}:complete"
        if s < season and D.get_meta(con, done_key) == "1" and not full:
            continue
        start, end = season_window(league, s)
        if s == season:
            end = min(end, now + timedelta(days=8))
        n_new = 0
        for a, b in _months(start, end):
            ck = f"{league.key}:{s}:month:{a:%Y%m}"
            if D.get_meta(con, ck) == "1" and not full:
                continue
            try:
                games, od = espn.fetch_dates(league, f"{a:%Y%m%d}-{b:%Y%m%d}", with_odds=True)
            except Exception:
                games, od = [], []
            if not games and a.month not in (7, 8, 9):
                # ESPN would not do a range: go day by day
                d = a
                while d <= b:
                    try:
                        g2, o2 = espn.fetch_dates(league, f"{d:%Y%m%d}", with_odds=True)
                        games += g2
                        od += o2
                    except Exception as e:
                        log(f"  ! {league.name} {d:%Y-%m-%d}: {e}")
                    d += timedelta(days=1)
                    time.sleep(0.1)
            for g in games:
                D.upsert_game(con, g)
                n_new += 1
            if s == season:
                lo, hi = now - timedelta(hours=6), now + timedelta(days=8)
                odds_rows += [r for r in od if lo <= parse_iso(r["commence_utc"]) <= hi]
            # a finished month never changes again
            if b < now - timedelta(days=2) and all(g["completed"] for g in games):
                D.set_meta(con, ck, "1")
            time.sleep(0.15)
        con.commit()
        log(f"  {league.name} {s - 1}-{str(s)[-2:]}: schedule and scores refreshed ({n_new} games touched)")
        if s < season:
            D.set_meta(con, done_key, "1")
            con.commit()
    return odds_rows


def update_games(con, league, seasons_back=1, fetch_players=True, full=False, espn_odds=True):
    now = now_utc()
    stamp = now.isoformat(timespec="seconds")
    odds_rows = []
    season = season_for(league.key, now)
    if league.schedule == "daily":
        odds_rows = _schedule_daily(con, league, season, seasons_back, full, espn_odds, now)

    for s in (range(season - seasons_back, season + 1) if league.schedule == "weekly" else []):
        done_key = f"{league.key}:{s}:complete"
        if s < season and D.get_meta(con, done_key) == "1" and not full:
            continue
        weeks = [(2, w) for w in range(1, 19)] + [(3, w) for w in range(1, 6)]
        n_new = 0
        for stype, wk in weeks:
            existing = con.execute(
                "SELECT COUNT(*) n, SUM(completed) c FROM games WHERE league=? AND season=? AND season_type=? AND week=?",
                (league.key, s, stype, wk)).fetchone()
            if existing["n"] and existing["n"] == existing["c"] and not full:
                continue
            if s == season and stype == 3 and now.month in (9, 10, 11):
                continue  # no playoff schedule yet
            try:
                games, wk_odds = espn.fetch_week(league, s, wk, stype, with_odds=True)
            except Exception as e:  # keep going; one bad week should not stop the run
                log(f"  ! {league.name} {s} week {wk}: {e}")
                continue
            for g in games:
                D.upsert_game(con, g)
                n_new += 1
            if s == season and espn_odds:
                odds_rows += wk_odds
            time.sleep(0.15)
        con.commit()
        log(f"  {league.name} {s}: schedule and scores refreshed ({n_new} games touched)")
        if s < season:
            D.set_meta(con, done_key, "1")
            con.commit()

    if espn_odds:
        for r in odds_rows:
            r["fetched_utc"] = stamp
        D.save_odds(con, odds_rows)
        if odds_rows:
            D.set_meta(con, f"{league.key}:odds_last_pull", stamp)
            D.set_meta(con, f"{league.key}:odds_source", ", ".join(sorted({r['book_title'] for r in odds_rows})))
        con.commit()
        log(f"  {league.name} lines from ESPN (free): {len({r['game_id'] for r in odds_rows})} games, "
            f"book: {', '.join(sorted({r['book_title'] for r in odds_rows})) or 'none listed yet'}")

    if fetch_players:
        # player box scores: both seasons for the NFL (few games), this season only for NBA/NHL
        min_season = season - (seasons_back if league.schedule == "weekly" else 0)
        todo = con.execute(
            "SELECT id, season, week, away_abbr, home_abbr FROM games WHERE league=? AND completed=1 AND players_loaded=0 "
            "AND season >= ? ORDER BY date_utc DESC", (league.key, min_season)).fetchall()
        if todo:
            log(f"  pulling {len(todo)} box scores (first run takes a few minutes)")
        for i, g in enumerate(todo, 1):
            try:
                team_rows, player_rows = espn.fetch_summary(league, g["id"])
                D.save_box(con, g["id"], league.key, team_rows, player_rows)
            except Exception as e:
                log(f"  ! box score {g['away_abbr']} @ {g['home_abbr']} ({g['id']}): {e}")
            if i % 25 == 0:
                con.commit()
                log(f"    {i}/{len(todo)}")
            time.sleep(0.2)
        con.commit()


def update_injuries(con, league):
    rows = espn.fetch_injuries(league, now_utc().isoformat(timespec="seconds"))
    if not rows:
        log(f"  ! {league.name} injuries: report came back empty, keeping the last one")
        return
    D.replace_injuries(con, league.key, rows)
    D.set_meta(con, f"{league.key}:injuries_pulled", now_utc().isoformat(timespec="seconds"))
    con.commit()
    out = sum(1 for r in rows if r["status"].lower() in ("out", "injured reserve", "doubtful"))
    log(f"  {league.name} injuries: {len(rows)} players listed ({out} out or doubtful) across "
        f"{len({r['team'] for r in rows})} teams")


def update_odds(con, league, cp):
    key = cp.get("odds", "api_key").strip()
    events, usage = odds.fetch(league, key, cp.get("odds", "regions"), cp.get("odds", "markets"),
                               cp.get("odds", "bookmakers").strip())
    rows = odds.parse(events, league.key)
    rows = odds.match_games(con, rows, league.key)
    unmatched = {(r["away"], r["home"]) for r in rows if not r["game_id"]}
    rows = [r for r in rows if r["game_id"]]
    D.save_odds(con, rows)
    D.set_meta(con, f"{league.key}:odds_credits_remaining", usage.get("remaining"))
    D.set_meta(con, f"{league.key}:odds_last_pull", now_utc().isoformat(timespec="seconds"))
    con.commit()
    log(f"  {league.name} lines: {len(rows)} prices from {len({r['book'] for r in rows})} books "
        f"for {len({r['game_id'] for r in rows})} games. Credits left this month: {usage.get('remaining')}")
    for a, h in sorted(unmatched):
        log(f"  ! could not match line for {a} @ {h} to a scheduled game")


def enabled_leagues(cp, only=None):
    keys = [only] if only else [k.strip() for k in cp.get("general", "leagues").split(",") if k.strip()]
    out = []
    for k in keys:
        lg = LEAGUES.get(k)
        if not lg:
            raise SystemExit(f"Unknown league '{k}'. Options: {', '.join(LEAGUES)}")
        if not lg.enabled:
            log(f"  {lg.name} is switched off. Skipping.")
            continue
        out.append(lg)
    return out
