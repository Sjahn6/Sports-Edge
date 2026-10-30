"""ESPN public site API: schedules, scores, team box scores and player box scores.

These endpoints are free and need no key, but they are unofficial, so every
parser here is defensive: a missing field skips that piece instead of crashing.
"""
import re
import time

import requests

from .util import nickname, parse_iso

BASE = "https://site.api.espn.com/apis/site/v2/sports/{path}"
HEADERS = {"User-Agent": "SportsEdge/0.1 (personal stats tracker)"}
_session = requests.Session()
_session.headers.update(HEADERS)


def _get(url, params=None, tries=3):
    for attempt in range(tries):
        try:
            r = _session.get(url, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
        except requests.RequestException:
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"ESPN request failed: {url}")


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        v = v.get("value", v.get("displayValue"))
        return _num(v)
    s = str(v).strip().replace(",", "")
    if s.endswith("%"):
        s = s[:-1]
    m = re.match(r"^(\d+):(\d{2})$", s)   # time on ice / minutes as mm:ss
    if m:
        return int(m.group(1)) + int(m.group(2)) / 60.0
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------- schedule

def parse_scoreboard(data: dict, league: str) -> list:
    games = []
    for ev in data.get("events", []) or []:
        g = parse_event(ev, league)
        if g:
            games.append(g)
    return games


def parse_event(ev: dict, league: str):
    comps = ev.get("competitions") or []
    if not comps:
        return None
    comp = comps[0]
    home = away = None
    for c in comp.get("competitors", []) or []:
        if c.get("homeAway") == "home":
            home = c
        elif c.get("homeAway") == "away":
            away = c
    if not home or not away:
        return None
    status = (comp.get("status") or ev.get("status") or {}).get("type") or {}
    completed = bool(status.get("completed"))
    season = ev.get("season") or {}
    week = (ev.get("week") or comp.get("week") or {}).get("number")

    def team(c):
        t = c.get("team") or {}
        full = t.get("displayName") or t.get("name") or ""
        short = t.get("shortDisplayName") or ""
        if not short or short == full or len(short) <= 4:
            short = nickname(full)
        col = (t.get("color") or "").strip("#")
        return str(t.get("id", "")), full, t.get("abbreviation") or "", short, (col if re.match(r"^[0-9a-fA-F]{6}$", col) else None)

    hid, hname, habbr, hshort, hcol = team(home)
    aid, aname, aabbr, ashort, acol = team(away)
    when = comp.get("date") or ev.get("date")
    if not when:
        return None
    return {
        "id": str(ev.get("id")),
        "league": league,
        "season": season.get("year"),
        "season_type": season.get("type"),
        "week": week,
        "date_utc": parse_iso(when).isoformat(),
        "home_id": hid, "home": hname, "home_abbr": habbr, "home_short": hshort, "home_color": hcol,
        "away_id": aid, "away": aname, "away_abbr": aabbr, "away_short": ashort, "away_color": acol,
        "home_score": _num(home.get("score")) if completed else None,
        "away_score": _num(away.get("score")) if completed else None,
        "completed": 1 if completed else 0,
        "neutral": 1 if comp.get("neutralSite") else 0,
    }


def fetch_week(league, season: int, week: int, season_type: int = 2, with_odds=False):
    url = BASE.format(path=league.espn_path) + "/scoreboard"
    data = _get(url, {"dates": season, "seasontype": season_type, "week": week})
    games = parse_scoreboard(data, league.key)
    if with_odds:
        return games, parse_scoreboard_odds(data, league.key)
    return games


def fetch_dates(league, dates: str, with_odds=False):
    """dates: 'YYYYMMDD' or a range 'YYYYMMDD-YYYYMMDD'."""
    url = BASE.format(path=league.espn_path) + "/scoreboard"
    data = _get(url, {"dates": dates, "limit": 1000})
    games = parse_scoreboard(data, league.key)
    if with_odds:
        return games, parse_scoreboard_odds(data, league.key)
    return games


# ---------------------------------------------------------------- odds

def _price(v):
    """'-110', -110.0, 'EVEN', '+150' -> int, or None."""
    if v is None:
        return None
    if isinstance(v, dict):
        v = v.get("odds", v.get("value", v.get("american")))
    s = str(v).strip().upper()
    if s in ("EVEN", "EV", "PK"):
        return 100
    try:
        x = int(round(float(s.replace("+", ""))))
    except ValueError:
        return None
    return x if abs(x) >= 100 else None


def _line(v):
    """'-3.5', 'o47.5', 'u47.5', 'PK', -3.5 -> float, or None."""
    if v is None:
        return None
    if isinstance(v, dict):
        v = v.get("line", v.get("value"))
    s = str(v).strip().lower().lstrip("ou")
    if s in ("pk", "pick", "even"):
        return 0.0
    try:
        return float(s.replace("+", ""))
    except ValueError:
        return None


def _cur(d, side):
    """New style ESPN odds: {"home": {"open": {...}, "close": {...}}}; 'close' is the current number."""
    x = (d or {}).get(side) or {}
    return x.get("close") or x.get("current") or x.get("open") or {}


def parse_event_odds(ev: dict, g: dict) -> list:
    """Sportsbook lines ESPN includes in the scoreboard, as odds rows (no key, no credits)."""
    comp = (ev.get("competitions") or [{}])[0]
    out = []
    for o in comp.get("odds", []) or []:
        prov = o.get("provider") or {}
        title = prov.get("displayName") or prov.get("name") or "ESPN odds"
        book = "espn:" + re.sub(r"[^a-z0-9]+", "", title.lower())
        ho, ao = o.get("homeTeamOdds") or {}, o.get("awayTeamOdds") or {}
        ps, ml, tot = o.get("pointSpread") or {}, o.get("moneyline") or {}, o.get("total") or {}

        # spread: newest format first, then older fields, then the "DET -3.5" text
        h_pt = _line(_cur(ps, "home").get("line"))
        a_pt = _line(_cur(ps, "away").get("line"))
        h_pr = _price(_cur(ps, "home").get("odds")) or _price(ho.get("spreadOdds"))
        a_pr = _price(_cur(ps, "away").get("odds")) or _price(ao.get("spreadOdds"))
        if h_pt is None and o.get("spread") is not None:
            v = _line(o.get("spread"))
            if v is not None and abs(v) < 60:
                if ho.get("favorite") is True:
                    h_pt = -abs(v)
                elif ao.get("favorite") is True:
                    h_pt = abs(v)
                elif v == 0:
                    h_pt = 0.0
        if h_pt is None:
            # "DET -3.5" text. In hockey this text is the moneyline ("TOR -170"), so
            # only trust spread sized numbers.
            det = str(o.get("details") or "")
            m = re.match(r"^\s*([A-Za-z.]+)\s+([-+]?\d+(?:\.\d+)?)\s*$", det)
            if m and abs(float(m.group(2))) < 60:
                pts = float(m.group(2))
                if m.group(1).upper() == (g["home_abbr"] or "").upper():
                    h_pt = pts
                elif m.group(1).upper() == (g["away_abbr"] or "").upper():
                    h_pt = -pts
            elif det.strip().upper() in ("EVEN", "PK", "PICK"):
                h_pt = 0.0
        if h_pt is not None and a_pt is None:
            a_pt = -h_pt
        if h_pt is not None:
            out.append(("spreads", "home", h_pt, h_pr or -110, book, title))
            out.append(("spreads", "away", a_pt, a_pr or -110, book, title))

        # total
        t_line = _line(_cur(tot, "over").get("line")) or _line(o.get("overUnder"))
        o_pr = _price(_cur(tot, "over").get("odds")) or _price(o.get("overOdds"))
        u_pr = _price(_cur(tot, "under").get("odds")) or _price(o.get("underOdds"))
        if t_line:
            out.append(("totals", "over", t_line, o_pr or -110, book, title))
            out.append(("totals", "under", t_line, u_pr or -110, book, title))

        # moneyline
        h_ml = _price(_cur(ml, "home").get("odds")) or _price(ho.get("moneyLine"))
        a_ml = _price(_cur(ml, "away").get("odds")) or _price(ao.get("moneyLine"))
        if h_ml and a_ml:
            out.append(("h2h", "home", None, h_ml, book, title))
            out.append(("h2h", "away", None, a_ml, book, title))

    return [{"league": g["league"], "event_id": "espn-" + g["id"], "game_id": g["id"],
             "commence_utc": g["date_utc"], "home": g["home"], "away": g["away"],
             "book": b, "book_title": t, "market": mk, "side": sd, "point": pt, "price": pr,
             "fetched_utc": None} for mk, sd, pt, pr, b, t in out]


def parse_scoreboard_odds(data: dict, league_key: str) -> list:
    rows = []
    for ev in data.get("events", []) or []:
        g = parse_event(ev, league_key)
        if g and not g["completed"]:
            rows += parse_event_odds(ev, g)
    return rows


# ---------------------------------------------------------------- box score

_PAIR = re.compile(r"^\s*(-?[\d.]+)\s*[/-]\s*(-?[\d.]+)\s*$")


def _split_stat(key: str, value):
    """ESPN packs some stats like 'completions/passingAttempts' = '22/31'."""
    text = None if value is None else str(value)
    if key and text:
        for sep in ("/", "-"):
            if sep in key:
                parts = key.split(sep)
                m = _PAIR.match(text)
                if m and len(parts) == 2:
                    return [(parts[0], float(m.group(1)), text), (parts[1], float(m.group(2)), text)]
    return [(key, _num(value), text)]


def parse_summary(data: dict):
    """Returns (team_rows, player_rows) from an ESPN game summary."""
    box = data.get("boxscore") or {}
    team_rows, player_rows = [], []

    for t in box.get("teams", []) or []:
        tname = (t.get("team") or {}).get("displayName", "")
        for s in t.get("statistics", []) or []:
            name = s.get("name") or s.get("label")
            if not name:
                continue
            disp = s.get("displayValue")
            m = _PAIR.match(str(disp or ""))
            if m and "-" in str(disp) and name.lower().endswith(("eff", "efficiency")):
                team_rows.append({"team": tname, "stat": name + "_made", "value_num": float(m.group(1)), "value_text": disp})
                team_rows.append({"team": tname, "stat": name + "_att", "value_num": float(m.group(2)), "value_text": disp})
            else:
                team_rows.append({"team": tname, "stat": name, "value_num": _num(s.get("value", disp)), "value_text": disp})

    for t in box.get("players", []) or []:
        tname = (t.get("team") or {}).get("displayName", "")
        for cat in t.get("statistics", []) or []:
            cname = (cat.get("name") or cat.get("type") or cat.get("text") or "players").lower()
            keys = cat.get("keys") or cat.get("names") or cat.get("labels") or []
            for a in cat.get("athletes", []) or []:
                ath = a.get("athlete") or {}
                pid = str(ath.get("id", ""))
                if not pid:
                    continue
                pos = (ath.get("position") or {}).get("abbreviation", "")
                for k, v in zip(keys, a.get("stats", []) or []):
                    for stat, vnum, vtext in _split_stat(k, v):
                        player_rows.append({
                            "player_id": pid, "player": ath.get("displayName", ""),
                            "team": tname, "position": pos, "category": cname,
                            "stat": stat, "value_num": vnum, "value_text": vtext,
                        })
    return team_rows, player_rows


# ---------------------------------------------------------------- injuries

def parse_injuries(data: dict, league_key: str, fetched_utc: str) -> list:
    rows = []
    for t in data.get("injuries", []) or []:
        team = t.get("displayName") or (t.get("team") or {}).get("displayName") or ""
        for i in t.get("injuries", []) or []:
            ath = i.get("athlete") or {}
            status = i.get("status") or (i.get("type") or {}).get("description") or ""
            det = i.get("details") or {}
            detail = " ".join(x for x in (det.get("side") if det.get("side") not in (None, "Not Specified") else "",
                                          det.get("type") or "", det.get("detail") or "") if x).strip()
            when = i.get("date")
            try:
                when = parse_iso(when).isoformat() if when else None
            except ValueError:
                when = None
            rows.append({
                "league": league_key, "team": team or (ath.get("team") or {}).get("displayName", ""),
                "player_id": str(ath.get("id", "")), "player": ath.get("displayName", ""),
                "position": (ath.get("position") or {}).get("abbreviation", ""),
                "status": status, "detail": detail,
                "comment": i.get("shortComment") or "", "date": when, "fetched_utc": fetched_utc,
            })
    return [r for r in rows if r["team"] and r["player"]]


def fetch_injuries(league, fetched_utc: str) -> list:
    url = BASE.format(path=league.espn_path) + "/injuries"
    return parse_injuries(_get(url), league.key, fetched_utc)


def fetch_summary(league, event_id: str):
    url = BASE.format(path=league.espn_path) + "/summary"
    return parse_summary(_get(url, {"event": event_id}))
