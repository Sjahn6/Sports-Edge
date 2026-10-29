"""The Odds API (v4): current spreads and totals from US sportsbooks.

Cost on the free plan = markets x regions per call. Spreads + totals for the
US region is 2 credits a call, so the 500 monthly credits cover about 250 pulls.
"""
import re
from datetime import datetime, timezone

import requests

BASE = "https://api.the-odds-api.com/v4/sports/{sport}/odds"


class OddsError(Exception):
    pass


def fetch(league, api_key, regions="us", markets="spreads,totals,h2h", bookmakers=""):
    if not api_key:
        raise OddsError("No Odds API key set. Add it to config.ini under [odds] api_key.")
    params = {"apiKey": api_key, "regions": regions, "markets": markets,
              "oddsFormat": "american", "dateFormat": "iso"}
    if bookmakers:
        params.pop("regions")
        params["bookmakers"] = bookmakers
    r = requests.get(BASE.format(sport=league.odds_key), params=params, timeout=25)
    usage = {"remaining": r.headers.get("x-requests-remaining"),
             "used": r.headers.get("x-requests-used"),
             "last_cost": r.headers.get("x-requests-last")}
    if r.status_code == 401:
        raise OddsError("The Odds API rejected the key (401). Check api_key in config.ini.")
    if r.status_code == 429:
        raise OddsError("Out of Odds API credits for this month (429).")
    r.raise_for_status()
    return r.json(), usage


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def parse(events: list, league_key: str, fetched_utc=None) -> list:
    fetched_utc = fetched_utc or datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for ev in events or []:
        home, away = ev.get("home_team", ""), ev.get("away_team", "")
        for bk in ev.get("bookmakers", []) or []:
            for mk in bk.get("markets", []) or []:
                mkey = mk.get("key")
                if mkey not in ("spreads", "totals", "h2h"):
                    continue
                for o in mk.get("outcomes", []) or []:
                    name = o.get("name", "")
                    if mkey in ("spreads", "h2h"):
                        side = "home" if norm(name) == norm(home) else "away" if norm(name) == norm(away) else None
                    else:
                        side = name.lower() if name.lower() in ("over", "under") else None
                    if side is None or o.get("price") is None:
                        continue
                    rows.append({
                        "league": league_key, "event_id": ev.get("id"), "game_id": None,
                        "commence_utc": ev.get("commence_time"), "home": home, "away": away,
                        "book": bk.get("key"), "book_title": bk.get("title", bk.get("key")),
                        "market": mkey, "side": side,
                        "point": o.get("point"), "price": int(o["price"]),
                        "fetched_utc": fetched_utc,
                    })
    return rows


def _parse_iso(s):
    from .util import parse_iso
    return parse_iso(s)


def match_games(con, rows, league_key):
    """Attach the ESPN game id to each odds row by team names and start time."""
    cache = {}
    for r in rows:
        key = (r["home"], r["away"], r["commence_utc"])
        if key not in cache:
            cache[key] = None
            start = _parse_iso(r["commence_utc"])
            day = start.date().isoformat()
            cands = con.execute(
                "SELECT id, home, away, date_utc FROM games WHERE league=? "
                "AND date_utc >= date(?, '-2 day') AND date_utc <= date(?, '+3 day')",
                (league_key, day, day),
            ).fetchall()
            for c in cands:
                if norm(c["home"]) == norm(r["home"]) and norm(c["away"]) == norm(r["away"]):
                    if abs((_parse_iso(c["date_utc"]) - start).total_seconds()) < 36 * 3600:
                        cache[key] = c["id"]
                        break
        r["game_id"] = cache[key]
    return rows
