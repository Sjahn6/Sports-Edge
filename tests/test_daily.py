"""Offline tests for NBA and NHL: simulated seasons shaped like ESPN's date based
scoreboard, box scores, odds and injury feeds. Run: python tests/test_daily.py [save.html]"""
import math
import random
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLORS = __import__("json").loads((Path(__file__).parent / "fixtures" / "team_colors.json").read_text())
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from sportsedge import board as B  # noqa: E402
from sportsedge import db as D  # noqa: E402
from sportsedge import espn  # noqa: E402
from sportsedge import gamedist as GD  # noqa: E402
from sportsedge import model as M  # noqa: E402
from sportsedge import pipeline as PL  # noqa: E402
from sportsedge import report as R  # noqa: E402
from sportsedge.leagues import LEAGUES, season_for  # noqa: E402
from sportsedge.util import now_utc, parse_iso  # noqa: E402

NBA_TEAMS = ["Atlanta Hawks", "Boston Celtics", "Brooklyn Nets", "Charlotte Hornets", "Chicago Bulls",
             "Cleveland Cavaliers", "Dallas Mavericks", "Denver Nuggets", "Detroit Pistons", "Golden State Warriors",
             "Houston Rockets", "Indiana Pacers", "LA Clippers", "Los Angeles Lakers", "Memphis Grizzlies",
             "Miami Heat", "Milwaukee Bucks", "Minnesota Timberwolves", "New Orleans Pelicans", "New York Knicks",
             "Oklahoma City Thunder", "Orlando Magic", "Philadelphia 76ers", "Phoenix Suns", "Portland Trail Blazers",
             "Sacramento Kings", "San Antonio Spurs", "Toronto Raptors", "Utah Jazz", "Washington Wizards"]
NHL_TEAMS = ["Anaheim Ducks", "Boston Bruins", "Buffalo Sabres", "Calgary Flames", "Carolina Hurricanes",
             "Chicago Blackhawks", "Colorado Avalanche", "Columbus Blue Jackets", "Dallas Stars", "Detroit Red Wings",
             "Edmonton Oilers", "Florida Panthers", "Los Angeles Kings", "Minnesota Wild", "Montreal Canadiens",
             "Nashville Predators", "New Jersey Devils", "New York Islanders", "New York Rangers", "Ottawa Senators",
             "Philadelphia Flyers", "Pittsburgh Penguins", "San Jose Sharks", "Seattle Kraken", "St. Louis Blues",
             "Tampa Bay Lightning", "Toronto Maple Leafs", "Utah Mammoth", "Vancouver Canucks", "Vegas Golden Knights",
             "Washington Capitals", "Winnipeg Jets"]


def abbr_map(teams):
    out = {}
    for t in teams:
        a = "".join(w[0] for w in t.split()).upper()[:2] + t.split()[-1][:2].upper()
        while a in out.values():
            a += "X"
        out[t] = a
    return out


def ml(p):
    p = min(max(p, 0.03), 0.97)
    return int(round(-100 * p / (1 - p))) if p >= 0.5 else int(round(100 * (1 - p) / p))


class World:
    def __init__(self, key, seed):
        self.key = key
        self.lg = LEAGUES[key]
        self.rng = random.Random(seed)
        self.teams = NBA_TEAMS if key == "nba" else NHL_TEAMS
        self.ab = abbr_map(self.teams)
        sd = 6.0 if key == "nba" else 0.35
        self.true = {t: self.rng.gauss(0, sd) for t in self.teams}
        self.now = now_utc()
        self.season = season_for(key, self.now)
        self.events, self.summaries = [], {}
        self.gid = 500000 if key == "nba" else 600000
        self.build()

    def score(self, h, a):
        r = self.rng
        if self.key == "nba":
            d = self.true[h] - self.true[a] + 2.0
            m = int(round(r.gauss(d, 12.0)))
            if m == 0:
                m = r.choice((-1, 1)) * r.randint(1, 6)   # overtime decides it
            tot = int(round(r.gauss(226, 15)))
            hs = (tot + m) // 2
            return hs, hs - m, False
        lh = math.exp(math.log(3.05) + self.true[h] / 2 + 0.03)
        la = math.exp(math.log(3.05) + self.true[a] / 2 - 0.03)
        hs, as_ = np.random.default_rng(r.randint(0, 10**9)).poisson([lh, la])
        ot = hs == as_
        if ot:
            if r.random() < 0.52:
                hs += 1
            else:
                as_ += 1
        return int(hs), int(as_), ot

    def build(self):
        s = self.season
        # last season: 70 days in Jan-Apr, this season: opening night 9 days ago to 7 days ahead
        last = [parse_iso(f"{s - 1}-01-10T00:00Z") + timedelta(days=i) for i in range(70)]
        this = [self.now - timedelta(days=9) + timedelta(days=i) for i in range(17)]
        for season, days in ((s - 1, last), (s, this)):
            for day in days:
                order = self.teams[:]
                self.rng.shuffle(order)
                n = 6 if self.key == "nba" else 7
                for i in range(n):
                    h, a = order[2 * i], order[2 * i + 1]
                    when = day.replace(hour=23, minute=30 + i % 3 * 10, second=0, microsecond=0)
                    self.add_game(season, when, h, a)
        # a guaranteed back to back for the first upcoming game's home team
        up = sorted((e for e in self.events if not e["_done"]), key=lambda e: e["date"])[0]
        h = up["_home"]
        others = [t for t in self.teams if t not in (h, up["_away"])]
        self.add_game(s, parse_iso(up["date"]) - timedelta(hours=24), h, others[0])

    def add_game(self, season, when, h, a):
        self.gid += 1
        gid = str(self.gid)
        done = when < self.now
        hs, as_, ot = self.score(h, a) if done else (None, None, False)
        date = when.strftime("%Y-%m-%dT%H:%MZ")

        def comp(t, side, sc):
            return {"homeAway": side, "score": str(sc if sc is not None else 0),
                    "team": {"id": str(self.teams.index(t) + 1), "displayName": t, "abbreviation": self.ab[t], "color": COLORS.get(t, "")}}
        ev = {"id": gid, "date": date, "season": {"year": season, "type": 2},
              "competitions": [{"date": date, "neutralSite": False,
                                "status": {"type": {"completed": done}},
                                "competitors": [comp(h, "home", hs), comp(a, "away", as_)]}],
              "_done": done, "_home": h, "_away": a}
        ev["competitions"][0]["odds"] = [self.espn_odds(h, a)]   # finished games keep the closing line
        self.events.append(ev)
        if done:
            self.summaries[gid] = self.summary(h, a, hs, as_)

    def espn_odds(self, h, a):
        if self.key == "nba":
            d = self.true[h] - self.true[a] + 2.0
            sp = -round(d * 2) / 2
            ph = M.win_prob(d, 12.0)
            return {"provider": {"name": "DraftKings"},
                    "pointSpread": {"home": {"close": {"line": f"{sp:+g}", "odds": "-110"}},
                                    "away": {"close": {"line": f"{-sp:+g}", "odds": "-110"}}},
                    "total": {"over": {"close": {"line": "o226.5", "odds": "-110"}},
                              "under": {"close": {"line": "u226.5", "odds": "-110"}}},
                    "moneyline": {"home": {"close": {"odds": f"{ml(ph + 0.02):+d}"}},
                                  "away": {"close": {"odds": f"{ml(1 - ph + 0.02):+d}"}}}}
        lh = math.exp(math.log(3.05) + self.true[h] / 2 + 0.03)
        la = math.exp(math.log(3.05) + self.true[a] / 2 - 0.03)
        g = GD.PoissonGame(lh, la)
        ph = g.p_home_win()
        fav_home = ph >= 0.5
        cov = g.margin_probs(1.5)[0] if fav_home else g.margin_probs(-1.5)[2]
        return {"provider": {"name": "DraftKings"},
                "details": f"{self.ab[h] if fav_home else self.ab[a]} {ml(ph + 0.02) if fav_home else ml(1 - ph + 0.02)}",
                "spread": 1.5, "overUnder": 6.0 if g.exp_total > 6.1 else 5.5,
                "overOdds": -115, "underOdds": -105,
                "homeTeamOdds": {"favorite": fav_home, "moneyLine": ml(ph + 0.02),
                                 "spreadOdds": ml(cov + 0.02) if fav_home else ml(1 - cov + 0.02)},
                "awayTeamOdds": {"favorite": not fav_home, "moneyLine": ml(1 - ph + 0.02),
                                 "spreadOdds": ml(1 - cov + 0.02) if fav_home else ml(cov + 0.02)}}

    def summary(self, h, a, hs, as_):
        r = self.rng
        blocks = []
        for t, pts in ((a, as_), (h, hs)):
            short = t.split()[-1]
            if self.key == "nba":
                ath = []
                star = int(pts * 0.28)
                rest = pts - star
                for j in range(8):
                    p = star if j == 0 else rest // 7
                    ath.append({"athlete": {"id": f"{self.ab[t]}-{j}", "displayName": f"{short} {'Star' if j == 0 else 'Player ' + str(j)}",
                                            "position": {"abbreviation": "G" if j < 3 else "F"}},
                                "stats": [str(34 if j == 0 else 22), f"{p // 2}-{p}", "2-6", "3-4", "1", "5",
                                          str(8 if j == 0 else 4), str(7 if j == 0 else 2), "1", "0", "2", "2", "+3", str(p)]})
                blocks.append({"team": {"displayName": t}, "statistics": [{
                    "keys": ["minutes", "fieldGoalsMade-fieldGoalsAttempted", "threePointFieldGoalsMade-threePointFieldGoalsAttempted",
                             "freeThrowsMade-freeThrowsAttempted", "offensiveRebounds", "defensiveRebounds", "rebounds",
                             "assists", "steals", "blocks", "turnovers", "fouls", "plusMinus", "points"],
                    "athletes": ath}]})
            else:
                fw = [{"athlete": {"id": f"{self.ab[t]}-F{j}", "displayName": f"{short} Forward {j}", "position": {"abbreviation": "C"}},
                       "stats": [str(1 if (j == 0 and pts) else 0), str(1 if j < 2 else 0), str(r.randint(1, 5)), "18:30", "+1"]}
                      for j in range(6)]
                gl = [{"athlete": {"id": f"{self.ab[t]}-G1", "displayName": f"{short} Goalie", "position": {"abbreviation": "G"}},
                       "stats": [str(r.randint(20, 35)), str(r.randint(22, 38)), str(2), ".910", "60:00"]}]
                blocks.append({"team": {"displayName": t}, "statistics": [
                    {"name": "forwards", "keys": ["goals", "assists", "shotsTotal", "timeOnIce", "plusMinus"], "athletes": fw},
                    {"name": "goalies", "keys": ["saves", "shotsAgainst", "goalsAgainst", "savePercentage", "timeOnIce"], "athletes": gl}]})
        return {"boxscore": {"teams": [], "players": blocks}}

    def injuries(self):
        up = sorted((e for e in self.events if not e["_done"]), key=lambda e: e["date"])[0]
        t = up["_away"]
        if self.key == "nba":
            who = {"id": f"{self.ab[t]}-0", "displayName": f"{t.split()[-1]} Star", "position": {"abbreviation": "G"}}
        else:
            who = {"id": f"{self.ab[t]}-G1", "displayName": f"{t.split()[-1]} Goalie", "position": {"abbreviation": "G"}}
        self.injured_team = t
        return {"injuries": [{"displayName": t, "injuries": [
            {"status": "Out", "date": self.now.strftime("%Y-%m-%dT%H:%MZ"), "shortComment": "Out tonight.",
             "athlete": who, "details": {"type": "Knee"}}]}]}

    def get(self, url, params=None, tries=3):
        if url.endswith("/scoreboard"):
            d = params["dates"]
            a, b = (d.split("-") + [d])[:2]
            out = [e for e in self.events if a <= parse_iso(e["date"]).strftime("%Y%m%d") <= b]
            return {"events": out}
        if url.endswith("/summary"):
            return self.summaries[params["event"]]
        if url.endswith("/injuries"):
            return self.injuries()
        raise AssertionError(url)


def test_poisson():
    g = GD.PoissonGame(3.0, 3.0)
    a, e, b = g.margin_probs(0)
    assert e == 0 and abs(a + b - 1) < 1e-9 and 0.5 < a < 0.52
    a, e, b = g.margin_probs(1.5)
    assert 0.2 < a < 0.4
    a, e, b = g.total_probs(6)
    assert e > 0.1 and abs(a + e + b - 1) < 1e-9
    lh, la = GD.poisson_from_targets(6.2, 0.62)
    g2 = GD.PoissonGame(lh, la)
    assert abs(g2.p_home_win() - 0.62) < 0.003 and abs(lh + la - 6.2) < 1e-6
    print("poisson ok: 3v3 home", round(g.p_home_win(), 3), "| targets ->", round(lh, 2), round(la, 2))


def run_league(key, seed, save_to=None):
    w = World(key, seed)
    lg = LEAGUES[key]
    espn._get = w.get
    PL.time = type("T", (), {"sleep": staticmethod(lambda s: None)})
    espn.time = PL.time
    tmp = tempfile.mkdtemp()
    con = D.connect(Path(tmp) / "t.db")
    PL.update_games(con, lg, seasons_back=1)
    PL.update_injuries(con, lg)
    n = con.execute("SELECT COUNT(*) FROM games WHERE league=?", (key,)).fetchone()[0]
    assert n == len(w.events), (n, len(w.events))
    pl = con.execute("SELECT COUNT(*) FROM games WHERE league=? AND players_loaded=1", (key,)).fetchone()[0]
    cur_done = sum(1 for e in w.events if e["_done"] and e["season"]["year"] == w.season)
    assert pl == cur_done, (pl, cur_done)   # box scores only for this season
    up = sum(1 for e in w.events if not e["_done"])
    lines = con.execute("SELECT COUNT(DISTINCT game_id) FROM odds").fetchone()[0]
    assert lines == up, (lines, up)
    PL.backfill_closing_lines(con, lg, 1)
    closed = con.execute("SELECT COUNT(DISTINCT o.game_id) FROM odds o JOIN games g ON g.id=o.game_id "
                         "WHERE g.completed=1 AND o.fetched_utc < g.date_utc").fetchone()[0]
    assert closed == sum(1 for e in w.events if e["_done"]), closed
    # second run: finished months are skipped
    calls = {"n": 0}
    orig = w.get

    def counting(url, params=None, tries=3):
        calls["n"] += 1
        return orig(url, params, tries)
    espn._get = counting
    PL.update_games(con, lg, seasons_back=1, fetch_players=False)
    assert calls["n"] <= 3, calls["n"]
    espn._get = w.get

    games = B.rows(con, "SELECT * FROM games WHERE completed=1")
    r = M.fit(games, lg, w.now)
    est = np.array([r.rating[t] for t in w.teams])
    tru = np.array([w.true[t] for t in w.teams])
    corr = float(np.corrcoef(est, tru)[0, 1])
    assert corr > 0.6, corr

    cfg = PL.model_cfg(PL.load_config(Path(tmp) / "x.ini"))
    out, alld = R.write(con, [lg], cfg, "America/Detroit", Path(tmp) / "r.html")
    d = alld["leagues"][key]
    assert d["board"] and all(g["moneylines"] and g["spreads"] and g["totals"] for g in d["board"])
    for g in d["board"]:
        for k in ("home", "away", "home_ml", "away_ml", "over", "under"):
            b = g["best"][k]
            assert 0 < b["p_win"] < 1, (k, b)
        assert g["why"]["reasons"]
    rested = [g for g in d["board"] if g["rest"]["home"] or g["rest"]["away"]]
    assert rested, "back to back not detected"
    ig = next(g for g in d["board"] if w.injured_team in (g["home"], g["away"]))
    side = "home" if ig["home"] == w.injured_team else "away"
    hit = max(ig["injuries"][side], key=lambda p: p["pts"])
    if key == "nba":
        assert hit["pts"] > 1.5, hit
    else:
        assert hit["starter_g"] and abs(hit["pts"] - 0.25) < 1e-9, hit
        assert ig["lam"] and all(0.5 < x < 6 for x in ig["lam"])
        pl_ = ig["best"]["home"]["point"]
        assert abs(pl_) == 1.5, pl_
    bt = d["backtest"]
    assert bt["bet_w"] + bt["bet_l"] > 50 and len(bt["seasons"]) >= 1, (bt["bet_w"], bt["bet_l"], bt["seasons"])
    print(f"   graded: {bt['bet_w']}-{bt['bet_l']} units {bt['units']:+.1f} | " +
          " | ".join(f"{x['label']} {x['bet_w']}-{x['bet_l']} {x['units']:+.1f}" for x in bt["seasons"]))
    cal = d["calibration"]
    assert cal["n"] > 100, cal["n"]
    print(f"{lg.name} ok: {n} games, rating corr {corr:.2f}, {len(d['board'])} upcoming, "
          f"{sum(len(g['plays']) for g in d['board'])} flagged plays, injury hit {hit['pts']:.2f} {lg.unit} ({hit['why']}), "
          f"back to backs {len(rested)}, calibration {cal['n']} games fitted={cal['fitted']} sd={cal['win_sd']}")
    print("   why:", ig["why"]["reasons"].get(side + "_ml", [])[:2])
    print("   alt:", ig["home_abbr"], ig["alt"])
    return con, tmp


if __name__ == "__main__":
    test_poisson()
    con1, t1 = run_league("nba", 3)
    con2, t2 = run_league("nhl", 5)
    print("all daily league tests passed")
