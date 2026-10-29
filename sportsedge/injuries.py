"""Injury reports and how they move a game's expected margin and total.

Each injured player gets a point value:
  QB      starter only. Base value scaled by how good he's been (yards per
          attempt vs league average). A backup QB on the report counts 0.
  RB/WR/TE  share of the team's scrimmage yards x skill_value.
  defense   share of the team's tackles + sacks x defense_value.
  OL        flat ol_value, only for injuries dated in the last 10 days.

That value is multiplied by the chance he misses (status) and by how much he
has actually been playing lately. A player who missed the last few games is
already baked into the team rating, so he barely counts.

Offense losses lower the team's own points. Defense losses raise the
opponent's points. Margin moves by both; the total moves by the difference.
"""
from collections import defaultdict
from datetime import timedelta

from .util import now_utc, parse_iso

MISS_CHANCE = {
    "out": 1.0, "injured reserve": 1.0, "ir": 1.0, "suspension": 1.0, "pup": 1.0,
    "physically unable to perform": 1.0, "non-football injury": 1.0,
    "doubtful": 0.85, "questionable": 0.25, "day-to-day": 0.15, "probable": 0.05,
}
OFFENSE = {"QB", "RB", "FB", "WR", "TE"}
OLINE = {"OL", "OT", "OG", "G", "T", "C", "LT", "RT", "LG", "RG"}
DEFENSE = {"DL", "DE", "DT", "NT", "LB", "ILB", "OLB", "MLB", "CB", "S", "SS", "FS", "DB", "EDGE"}

DEFAULTS = {"enabled": "true", "qb_value": "3.5", "skill_value": "3.5",
            "defense_value": "2.0", "ol_value": "0.3", "max_per_team": "8.0",
            "nba_value": "22", "nba_max_per_team": "12",
            "nhl_skater_value": "2.0", "nhl_goalie_value": "0.25", "nhl_max_per_team": "0.6"}

# NBA: share of team production (points + 1.2 rebounds + 1.5 assists) above a
#      bench player's share, x nba_value. A 25% share star = about 4 pts.
# NHL: skaters by share of team points (goals + assists) above depth level, x
#      nhl_skater_value, in goals. The starting goalie (most recent ice time)
#      out costs nhl_goalie_value goals against.
NBA_FLOOR, NHL_FLOOR = 0.06, 0.03


def miss_chance(status: str) -> float:
    s = (status or "").strip().lower()
    if s in MISS_CHANCE:
        return MISS_CHANCE[s]
    for k, v in MISS_CHANCE.items():
        if k in s:
            return v
    return 0.0


def _rows(con, sql, args=()):
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def usage(con, league_key, season):
    if league_key in ("nba", "nhl"):
        return usage_other(con, league_key, season)
    return usage_nfl(con, league_key, season)


def usage_other(con, league_key, season):
    games = _rows(con, "SELECT id, home, away, date_utc FROM games WHERE league=? AND season=? AND completed=1 "
                       "AND players_loaded=1 ORDER BY date_utc DESC", (league_key, season))
    if not games:
        return {}, 0.0
    team_games = defaultdict(list)
    for g in games:
        team_games[g["home"]].append(g["id"])
        team_games[g["away"]].append(g["id"])
    ph = ",".join("?" * len(games))
    stats = _rows(con, f"SELECT game_id, player_id, player, team, position, category, stat, value_num "
                       f"FROM player_stats WHERE game_id IN ({ph}) AND value_num IS NOT NULL", [g["id"] for g in games])
    W = {"nba": {"points": 1.0, "rebounds": 1.2, "assists": 1.5},
         "nhl": {"goals": 1.0, "assists": 1.0}}[league_key]
    teams = defaultdict(lambda: {"players": {}, "prod": 0.0})
    for r in stats:
        t = teams[r["team"]]
        p = t["players"].setdefault(r["player_id"], {"id": r["player_id"], "name": r["player"], "pos": r["position"],
                                                     "games": set(), "prod": 0.0, "toi": 0.0, "goalie": False,
                                                     "recent_toi": 0.0, "yards": 0.0, "def": 0.0, "pass_att": 0.0})
        p["games"].add(r["game_id"])
        w = W.get(r["stat"])
        if w and not (league_key == "nhl" and r["category"].startswith("goal")):
            p["prod"] += w * r["value_num"]
            t["prod"] += w * r["value_num"]
        if league_key == "nhl" and r["category"].startswith("goal"):
            p["goalie"] = True
            if r["stat"] in ("timeOnIce", "minutes"):
                p["toi"] += r["value_num"]
                if r["game_id"] in team_games[r["team"]][:5]:
                    p["recent_toi"] += r["value_num"]
            if r["stat"] in ("saves", "shotsAgainst"):   # fallback when ice time is missing
                if r["game_id"] in team_games[r["team"]][:5]:
                    p["recent_toi"] += r["value_num"] * 0.01
    out = {}
    for name, t in teams.items():
        gs = [p for p in t["players"].values() if p["goalie"]]
        starter = max(gs, key=lambda p: (p["recent_toi"], p["toi"]))["id"] if gs else None
        out[name] = {"games": team_games.get(name, []), "players": t["players"], "prod": t["prod"],
                     "goalie_starter": starter, "qb_starter": None}
    return out, 0.0


def value_other(league_key, p, tu, cfg, inj):
    """(value, why, side) for an NBA or NHL player."""
    pos = (inj.get("position") or (p or {}).get("pos") or "").upper()
    if not p:
        return 0.0, "no recent role", "off"
    if league_key == "nhl" and (p.get("goalie") or pos == "G"):
        if tu.get("goalie_starter") == p["id"]:
            return cfg["nhl_goalie_value"], "starting goalie", "def"
        return 0.0, "backup goalie", "def"
    share = p["prod"] / tu["prod"] if tu.get("prod") else 0.0
    if league_key == "nba":
        v = cfg["nba_value"] * max(0.0, share - NBA_FLOOR)
        return v, f"{share * 100:.0f}% of team production", "both"
    v = cfg["nhl_skater_value"] * max(0.0, share - NHL_FLOOR)
    return v, f"{share * 100:.0f}% of team points", "off"


def usage_nfl(con, league_key, season):
    """Per team usage this season from saved box scores.

    Returns {team: {"games": [game ids newest first],
                    "players": {pid: {...}}, "qb_starter": pid, "league_ypa": x}}
    """
    games = _rows(con, "SELECT id, home, away, date_utc FROM games WHERE league=? AND season=? AND completed=1 "
                       "AND players_loaded=1 ORDER BY date_utc DESC", (league_key, season))
    if not games:
        return {}, 7.0
    team_games = defaultdict(list)
    for g in games:
        team_games[g["home"]].append(g["id"])
        team_games[g["away"]].append(g["id"])
    ph = ",".join("?" * len(games))
    stats = _rows(con, f"SELECT game_id, player_id, player, team, position, category, stat, value_num "
                       f"FROM player_stats WHERE game_id IN ({ph}) AND value_num IS NOT NULL", [g["id"] for g in games])

    teams = defaultdict(lambda: {"players": {}, "yards": 0.0, "def": 0.0})
    tot_yds = tot_att = 0.0
    for r in stats:
        t = teams[r["team"]]
        p = t["players"].setdefault(r["player_id"], {"id": r["player_id"], "name": r["player"], "pos": r["position"],
                                                     "games": set(), "yards": 0.0, "def": 0.0,
                                                     "pass_att": 0.0, "pass_yds": 0.0, "recent_att": 0.0})
        p["games"].add(r["game_id"])
        st, v = r["stat"], r["value_num"]
        if r["category"] in ("rushing", "receiving") and st in ("rushingYards", "receivingYards"):
            p["yards"] += v
            t["yards"] += v
        elif r["category"] == "passing" and st == "passingAttempts":
            p["pass_att"] += v
            tot_att += v
            if r["game_id"] in team_games[r["team"]][:3]:
                p["recent_att"] += v
        elif r["category"] == "passing" and st == "passingYards":
            p["pass_yds"] += v
            tot_yds += v
        elif r["category"] == "defensive" and st in ("totalTackles", "sacks"):
            w = 3.0 if st == "sacks" else 1.0
            p["def"] += v * w
            t["def"] += v * w
    league_ypa = tot_yds / tot_att if tot_att else 7.0

    out = {}
    for name, t in teams.items():
        qbs = [p for p in t["players"].values() if p["pass_att"] > 0]
        starter = max(qbs, key=lambda p: (p["recent_att"], p["pass_att"]))["id"] if qbs else None
        out[name] = {"games": team_games.get(name, []), "players": t["players"],
                     "yards": t["yards"], "def": t["def"], "qb_starter": starter}
    return out, league_ypa


def player_value(p, team_use, league_ypa, cfg, inj, now):
    """Point value of a fully absent player, before status and recency scaling."""
    pos = (inj.get("position") or (p or {}).get("pos") or "").upper()
    if p and p["pass_att"] > 0 or pos == "QB":
        if not p or team_use.get("qb_starter") != p["id"]:
            return 0.0, "backup QB"
        ypa = p["pass_yds"] / p["pass_att"] if p["pass_att"] else league_ypa
        scale = min(1.6, max(0.5, 1.0 + (ypa - league_ypa) / 2.5))
        return cfg["qb_value"] * scale, f"starting QB, {ypa:.1f} yds/att"
    if pos in OLINE:
        when = inj.get("date")
        fresh = bool(when) and (now - parse_iso(when)) <= timedelta(days=10)
        return (cfg["ol_value"] if fresh else 0.0), "lineman" if fresh else "lineman, older injury"
    if p and team_use.get("yards") and p["yards"] > 0 and (pos in OFFENSE or not pos):
        share = p["yards"] / team_use["yards"]
        return cfg["skill_value"] * share, f"{share * 100:.0f}% of team scrimmage yards"
    if p and team_use.get("def") and p["def"] > 0:
        share = p["def"] / team_use["def"]
        return cfg["defense_value"] * share * 3, f"{share * 100:.0f}% of team tackles and sacks"
    return 0.0, "no recent role"


def recent_presence(p, team_use, n=3):
    """Share of the team's last n games he played in. 1.0 means he's been playing."""
    if not p:
        return 0.0
    last = team_use.get("games", [])[:n]
    if not last:
        return 0.0
    return sum(1 for g in last if g in p["games"]) / len(last)


def team_adjustment(con, league_key, season, team, cfg, use=None, now=None):
    """Returns {"off": pts lost on offense, "def": pts lost on defense, "players": [...]}"""
    now = now or now_utc()
    use, league_ypa = use if use else usage(con, league_key, season)
    tu = use.get(team, {"players": {}, "games": []})
    by_name = {p["name"].lower(): p for p in tu["players"].values()}
    inj = _rows(con, "SELECT * FROM injuries WHERE league=? AND team=?", (league_key, team))
    off = dfn = 0.0
    listed = []
    for i in inj:
        chance = miss_chance(i["status"])
        p = tu["players"].get(i["player_id"]) or by_name.get((i["player"] or "").lower())
        pos = (i["position"] or (p or {}).get("pos") or "").upper()
        if league_key in ("nba", "nhl"):
            value, why, side = value_other(league_key, p, tu, cfg, i)
            presence = recent_presence(p, tu, n=5 if league_key == "nba" else 4)
        else:
            value, why = player_value(p, tu, league_ypa, cfg, i, now)
            presence = 1.0 if pos in OLINE else recent_presence(p, tu)
            side = "def" if (pos in DEFENSE or (p and p["def"] > 0 and p["yards"] == 0 and p["pass_att"] == 0)) else "off"
        pts = value * chance * presence
        if side == "both":   # NBA stars hurt both ends: about 60% offense, 40% defense
            off += pts * 0.6
            dfn += pts * 0.4
            side = "off"
        elif side == "off":
            off += pts
        else:
            dfn += pts
        listed.append({"player": i["player"], "pos": pos, "status": i["status"], "detail": i["detail"],
                       "comment": i["comment"], "chance": chance, "value": value, "presence": presence,
                       "pts": pts, "why": why, "side": side,
                       "starter_qb": bool(p) and tu.get("qb_starter") == p["id"],
                       "starter_g": bool(p) and tu.get("goalie_starter") == p["id"]})
    cap = {"nba": cfg["nba_max_per_team"], "nhl": cfg["nhl_max_per_team"]}.get(league_key, cfg["max_per_team"])
    total = off + dfn
    if total > cap:
        off, dfn = off * cap / total, dfn * cap / total
    listed.sort(key=lambda x: (-x["pts"], -x["chance"]))
    return {"off": off, "def": dfn, "players": listed}


def game_adjustment(con, league_key, season, g, cfg, use=None):
    """Margin and total shifts for one game from both injury reports."""
    h = team_adjustment(con, league_key, season, g["home"], cfg, use)
    a = team_adjustment(con, league_key, season, g["away"], cfg, use)
    margin = -(h["off"] + h["def"]) + (a["off"] + a["def"])
    total = -(h["off"] + a["off"]) + (h["def"] + a["def"])
    return {"margin": margin, "total": total, "home": h, "away": a}


def config(cp):
    if not cp.has_section("injuries"):
        cp.add_section("injuries")
    for k, v in DEFAULTS.items():
        if not cp.has_option("injuries", k):
            cp.set("injuries", k, v)
    return {"enabled": cp.getboolean("injuries", "enabled"),
            **{k: cp.getfloat("injuries", k) for k in DEFAULTS if k != "enabled"}}
