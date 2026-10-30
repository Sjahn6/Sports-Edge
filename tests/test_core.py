"""Offline tests. Builds a fake NFL season shaped like ESPN and Odds API responses,
runs the whole pipeline on it, and checks the math. Run: python tests/test_core.py"""
import json
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
from sportsedge import espn, odds  # noqa: E402
from sportsedge import model as M  # noqa: E402
from sportsedge import pipeline as PL  # noqa: E402
from sportsedge import report as R  # noqa: E402
from sportsedge.leagues import LEAGUES, season_for  # noqa: E402
from sportsedge.util import now_utc, parse_iso  # noqa: E402

NFL = LEAGUES["nfl"]
TEAMS = ["Arizona Cardinals", "Atlanta Falcons", "Baltimore Ravens", "Buffalo Bills", "Carolina Panthers",
         "Chicago Bears", "Cincinnati Bengals", "Cleveland Browns", "Dallas Cowboys", "Denver Broncos",
         "Detroit Lions", "Green Bay Packers", "Houston Texans", "Indianapolis Colts", "Jacksonville Jaguars",
         "Kansas City Chiefs", "Las Vegas Raiders", "Los Angeles Chargers", "Los Angeles Rams", "Miami Dolphins",
         "Minnesota Vikings", "New England Patriots", "New Orleans Saints", "New York Giants", "New York Jets",
         "Philadelphia Eagles", "Pittsburgh Steelers", "San Francisco 49ers", "Seattle Seahawks",
         "Tampa Bay Buccaneers", "Tennessee Titans", "Washington Commanders"]
ABBR = {t: t.split()[-1][:4].upper() for t in TEAMS}
assert len(set(ABBR.values())) == 32

rng = random.Random(7)
TRUE = {t: rng.gauss(0, 5.5) for t in TEAMS}
OFF = {t: rng.gauss(0, 3.5) for t in TEAMS}
DEF = {t: TRUE[t] - OFF[t] for t in TEAMS}  # so rating ~ off - def_allowed


def sim_score(h, a):
    exp_h = 22 + OFF[h] - (DEF[a] * -1) * 0 + 0.8 + (TRUE[h] - TRUE[a]) / 2
    exp_a = 22 + OFF[a] + -0.8 - (TRUE[h] - TRUE[a]) / 2
    return max(0, int(round(rng.gauss(exp_h, 9.5)))), max(0, int(round(rng.gauss(exp_a, 9.5))))


def espn_event(gid, season, week, date, h, a, completed, hs=None, as_=None):
    def comp(t, side, sc):
        return {"homeAway": side, "score": str(sc) if sc is not None else "0",
                "team": {"id": str(TEAMS.index(t) + 1), "displayName": t, "abbreviation": ABBR[t], "color": COLORS.get(t, "")}}
    return {"id": gid, "date": date, "season": {"year": season, "type": 2}, "week": {"number": week},
            "competitions": [{"date": date, "neutralSite": False,
                              "status": {"type": {"completed": completed, "state": "post" if completed else "pre"}},
                              "competitors": [comp(h, "home", hs), comp(a, "away", as_)]}]}


def add_espn_odds(ev, h, a, new_style):
    fair = -(TRUE[h] - TRUE[a] + 1.6)
    sp = round(fair * 2) / 2
    ph = M.win_prob(-fair, 11.5)
    hml, aml = ml(ph + 0.022), ml(1 - ph + 0.022)
    if new_style:
        o = {"provider": {"id": "40", "name": "DraftKings", "displayName": "DraftKings"},
             "pointSpread": {"home": {"close": {"line": f"{sp:+g}", "odds": "-110"}},
                             "away": {"close": {"line": f"{-sp:+g}", "odds": "-110"}}},
             "total": {"over": {"close": {"line": "o44.5", "odds": "-112"}},
                       "under": {"close": {"line": "u44.5", "odds": "-108"}}},
             "moneyline": {"home": {"close": {"odds": f"{hml:+d}"}}, "away": {"close": {"odds": f"{aml:+d}"}}}}
    else:
        fav = h if sp < 0 else a
        o = {"provider": {"id": "40", "name": "DraftKings"},
             "details": f"{ABBR[fav]} {-abs(sp):g}" if sp != 0 else "EVEN",
             "overUnder": 44.5, "spread": sp, "overOdds": -110, "underOdds": -110,
             "homeTeamOdds": {"favorite": sp < 0, "moneyLine": hml, "spreadOdds": -110},
             "awayTeamOdds": {"favorite": sp > 0, "moneyLine": aml, "spreadOdds": -110}}
    ev["competitions"][0]["odds"] = [o]


def build_world():
    now = now_utc()
    season = season_for("nfl", now)
    weeks = {}   # (season, stype, week) -> events
    summaries = {}
    gid = 400000
    for s, nweeks, offset_weeks in ((season - 1, 18, 60), (season, 10, 0)):
        for w in range(1, nweeks + 1):
            order = TEAMS[:]
            rng.shuffle(order)
            evs = []
            if s == season:
                base = now + timedelta(days=3 + 7 * (w - 8))  # weeks 1-7 done, 8 upcoming
            else:
                base = now - timedelta(days=7 * (offset_weeks - w))
            for i in range(0, 32, 2):
                h, a = order[i], order[i + 1]
                gid += 1
                date = (base + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%MZ")
                done = parse_iso(date) < now
                hs, as_ = sim_score(h, a) if done else (None, None)
                ev_ = espn_event(str(gid), s, w, date, h, a, done, hs, as_)
                if s == season or done:   # closing lines stay on finished games
                    add_espn_odds(ev_, h, a, new_style=(i % 4 == 0))
                evs.append(ev_)
                if done:
                    summaries[str(gid)] = fake_summary(h, a, hs, as_)
            weeks[(s, 2, w)] = {"events": evs}
    return now, season, weeks, summaries


def fake_summary(h, a, hs, as_):
    def team_block(t, pts):
        qb = {"athlete": {"id": f"qb-{ABBR[t]}", "displayName": f"{t.split()[-1]} QB", "position": {"abbreviation": "QB"}},
              "stats": [f"{rng.randint(15, 30)}/{rng.randint(30, 42)}", str(rng.randint(150, 350)), "7.1", str(rng.randint(0, 3)), str(rng.randint(0, 2))]}
        rb = {"athlete": {"id": f"rb-{ABBR[t]}", "displayName": f"{t.split()[-1]} RB", "position": {"abbreviation": "RB"}},
              "stats": [str(rng.randint(8, 25)), str(rng.randint(20, 130)), "4.2", str(rng.randint(0, 2)), "18"]}
        return {"team": {"displayName": t},
                "statistics": [
                    {"name": "passing", "keys": ["completions/passingAttempts", "passingYards", "yardsPerPassAttempt", "passingTouchdowns", "interceptions"], "athletes": [qb]},
                    {"name": "rushing", "keys": ["rushingAttempts", "rushingYards", "yardsPerRushAttempt", "rushingTouchdowns", "longRushing"], "athletes": [rb]},
                ]}
    return {"boxscore": {
        "teams": [{"team": {"displayName": t}, "statistics": [
            {"name": "totalYards", "displayValue": str(rng.randint(250, 450)), "label": "Total Yards"},
            {"name": "thirdDownEff", "displayValue": f"{rng.randint(3, 8)}-{rng.randint(10, 15)}", "label": "3rd down"},
            {"name": "turnovers", "displayValue": str(rng.randint(0, 3)), "label": "Turnovers"}]} for t in (a, h)],
        "players": [team_block(a, as_), team_block(h, hs)]}}


def ml(p):
    p = min(max(p, 0.03), 0.97)
    return int(round(-100 * p / (1 - p))) if p >= 0.5 else int(round(100 * (1 - p) / p))


def fake_odds(weeks, season, now):
    out = []
    for (s, st, w), data in weeks.items():
        if s != season:
            continue
        for ev in data["events"]:
            c = ev["competitions"][0]
            h = next(x for x in c["competitors"] if x["homeAway"] == "home")["team"]["displayName"]
            a = next(x for x in c["competitors"] if x["homeAway"] == "away")["team"]["displayName"]
            if parse_iso(ev["date"]) < now:
                continue
            fair = -(TRUE[h] - TRUE[a] + 1.6)
            ph = M.win_prob(-fair, 11.5)
            books = []
            for key, title, wig in (("draftkings", "DraftKings", 0), ("fanduel", "FanDuel", 0.5), ("betmgm", "BetMGM", -0.5)):
                sp = round((fair + wig) * 2) / 2
                books.append({"key": key, "title": title, "markets": [
                    {"key": "spreads", "outcomes": [{"name": h, "price": -110, "point": sp}, {"name": a, "price": -110, "point": -sp}]},
                    {"key": "totals", "outcomes": [{"name": "Over", "price": -110, "point": 44.5}, {"name": "Under", "price": -110, "point": 44.5}]},
                    {"key": "h2h", "outcomes": [{"name": h, "price": ml(ph + 0.022)}, {"name": a, "price": ml(1 - ph + 0.022)}]}]})
            out.append({"id": "odds-" + ev["id"], "sport_key": "americanfootball_nfl", "commence_time": ev["date"].replace("Z", ":00Z"),
                        "home_team": h, "away_team": a, "bookmakers": books})
    return out


def fake_injuries():
    def inj(pid, name, pos, status, side="Left", typ="Knee"):
        return {"status": status, "date": "2026-10-28T18:00Z", "shortComment": f"{name} ({typ.lower()}) listed {status.lower()}.",
                "athlete": {"id": pid, "displayName": name, "position": {"abbreviation": pos}},
                "type": {"description": status}, "details": {"type": typ, "side": side}}
    return {"injuries": [
        {"displayName": "Detroit Lions", "injuries": [
            inj("qb-LION", "Lions QB", "QB", "Out", "Right", "Shoulder"),
            inj("rb-LION", "Lions RB", "RB", "Questionable", "Not Specified", "Ankle"),
            inj("ol-1", "Some Tackle", "OT", "Out")]},
        {"displayName": "Kansas City Chiefs", "injuries": [
            inj("old-1", "Long Gone WR", "WR", "Injured Reserve")]},
    ]}


# ------------------------------------------------------------------- tests

def test_probabilities():
    a, p, b = M.line_probs(0, 0.5, 13.5)
    assert abs(a + b - 1) < 1e-9 and p == 0
    a, p, b = M.line_probs(3, 3, 13.5)
    assert p > 0.02 and abs(a + p + b - 1) < 1e-9 and abs(a - b) < 1e-9
    assert abs(sum(M.no_vig(-110, -110)) - 1) < 1e-12
    assert abs(M.expected_value(0.5238, 0, -110)) < 1e-3
    assert M.to_american(0.6) == -150 and M.to_american(0.4) == 150
    assert M.kelly(0.5, 0, -110) == 0.0
    print("probabilities ok")


def test_parsers():
    ev = espn_event("1", 2026, 3, "2026-09-28T17:00Z", "Detroit Lions", "Chicago Bears", True, 31, 17)
    g = espn.parse_event(ev, "nfl")
    assert g["home"] == "Detroit Lions" and g["home_score"] == 31 and g["completed"] == 1
    assert g["date_utc"].startswith("2026-09-28T17:00:00")
    t, p = espn.parse_summary(fake_summary("Detroit Lions", "Chicago Bears", 31, 17))
    stats = {r["stat"] for r in p}
    assert {"completions", "passingAttempts", "passingYards", "rushingYards"} <= stats
    assert any(r["stat"] == "thirdDownEff_made" for r in t)
    assert parse_iso("2026-10-26T00:15Z") == parse_iso("2026-10-26T00:15:00+00:00")
    print("parsers ok")


def test_end_to_end(save_to=None):
    now, season, weeks, summaries = build_world()

    def fake_get(url, params=None, tries=3):
        if url.endswith("/scoreboard"):
            return weeks.get((params["dates"], params["seasontype"], params["week"]), {"events": []})
        if url.endswith("/summary"):
            return summaries[params["event"]]
        if url.endswith("/injuries"):
            return fake_injuries()
        raise AssertionError(url)

    espn._get = fake_get
    espn.time = type("T", (), {"sleep": staticmethod(lambda s: None)})
    PL.time = espn.time
    events = fake_odds(weeks, season, now)
    odds.fetch = lambda *a, **k: (events, {"remaining": "498", "used": "2", "last_cost": "2"})

    with tempfile.TemporaryDirectory() as tmp:
        con = D.connect(Path(tmp) / "t.db")
        PL.update_games(con, NFL, seasons_back=1)
        n = con.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        done = con.execute("SELECT COUNT(*) FROM games WHERE completed=1").fetchone()[0]
        assert n == 16 * 28, n
        assert done == 16 * 25, done
        assert con.execute("SELECT COUNT(*) FROM games WHERE players_loaded=1").fetchone()[0] == done

        PL.update_injuries(con, NFL)
        assert con.execute("SELECT COUNT(*) FROM injuries").fetchone()[0] == 4

        # one time backfill of closing lines for finished games
        PL.backfill_closing_lines(con, NFL, 1)
        closed = con.execute("SELECT COUNT(DISTINCT o.game_id) FROM odds o JOIN games g ON g.id=o.game_id "
                             "WHERE g.completed=1 AND o.fetched_utc < g.date_utc").fetchone()[0]
        assert closed == done, (closed, done)
        PL.backfill_closing_lines(con, NFL, 1)   # second call is a no-op
        assert con.execute("SELECT COUNT(*) FROM odds o JOIN games g ON g.id=o.game_id WHERE g.completed=1").fetchone()[0] == closed * 6
        con.execute("DELETE FROM odds WHERE game_id IN (SELECT id FROM games WHERE completed=1)")
        con.commit()
        # free ESPN (DraftKings) lines came in with the schedule
        dk = B.rows(con, "SELECT * FROM odds WHERE book='espn:draftkings'")
        assert len({r["game_id"] for r in dk}) == 16 * 3, len({r["game_id"] for r in dk})
        assert {r["market"] for r in dk} == {"spreads", "totals", "h2h"}
        for gid in {r["game_id"] for r in dk}:
            q = {(r["market"], r["side"]): r for r in dk if r["game_id"] == gid}
            g = B.rows(con, "SELECT * FROM games WHERE id=?", (gid,))[0]
            fair = -(TRUE[g["home"]] - TRUE[g["away"]] + 1.6)
            assert q[("spreads", "home")]["point"] == round(fair * 2) / 2, (q[("spreads", "home")]["point"], fair)
            assert q[("spreads", "home")]["point"] == -q[("spreads", "away")]["point"]
            assert q[("totals", "over")]["point"] == 44.5
            assert (q[("h2h", "home")]["price"] < 0) == (fair < 0)
        print("espn lines ok:", len(dk), "DraftKings prices")

        cp = PL.load_config(Path(tmp) / "missing.ini")
        cp.set("odds", "api_key", "x")
        PL.update_odds(con, NFL, cp)   # optional Odds API path still works
        matched = con.execute("SELECT COUNT(DISTINCT game_id) FROM odds WHERE game_id IS NOT NULL").fetchone()[0]
        assert matched == 16 * 3, matched

        # second run should skip finished weeks and prior season
        calls = {"n": 0}

        def counting(url, params=None, tries=3):
            calls["n"] += 1
            return fake_get(url, params, tries)
        espn._get = counting
        PL.update_games(con, NFL, seasons_back=1)
        assert calls["n"] <= 11, calls["n"]   # only current season weeks with unfinished games

        games = B.rows(con, "SELECT * FROM games WHERE completed=1")
        r = M.fit(games, NFL, now)
        est = np.array([r.rating[t] for t in TEAMS])
        true = np.array([TRUE[t] for t in TEAMS])
        corr = float(np.corrcoef(est, true)[0, 1])
        assert corr > 0.55, corr  # NFL is noisy: 7 games per team
        assert 30 < r.mu * 2 < 60, r.mu

        cfg = PL.model_cfg(cp)
        out, alld = R.write(con, [NFL], cfg, "America/Detroit", Path(tmp) / "report.html")
        data = alld["leagues"]["nfl"]
        assert len(data["board"]) == 16, len(data["board"])
        g0 = data["board"][0]
        assert len(g0["spreads"]) == 4 and g0["best"]["home"] and g0["best"]["over"]
        assert len(data["teams"]) == 32
        assert data["players"]["players"]
        assert all(g["moneylines"] for g in data["board"])
        assert all(g["alt"]["home"] is not None and g["alt"]["over"] is not None for g in data["board"])
        assert data["calibration"]["n"] > 200, data["calibration"]["n"]
        print("calibration: win_sd", data["calibration"]["win_sd"], "games", data["calibration"]["n"],
              [(round(b["pred"], 2), round(b["actual"], 2), b["n"]) for b in data["calibration"]["buckets"] if b["n"]])
        chalk = [(g["home_abbr"] if k == "home_ml" else g["away_abbr"], g["best"][k]["price"], round(g["best"][k]["p_win"], 3), round(g["best"][k]["ev"], 3))
                 for g in data["board"] for k in ("home_ml", "away_ml") if -260 <= g["best"][k]["price"] <= -160]
        print("chalk sample:", chalk[:5])
        g1 = data["board"][0]
        print("alt sample:", g1["home_abbr"], round(g1["exp_margin"], 1), g1["alt"])
        det = next(g for g in data["board"] if "Detroit Lions" in (g["home"], g["away"]))
        side = "home" if det["home"] == "Detroit Lions" else "away"
        qb = next(p for p in det["injuries"][side] if p["player"] == "Lions QB")
        assert qb["starter_qb"] and qb["pts"] > 1.5, qb
        rb = next(p for p in det["injuries"][side] if p["player"] == "Lions RB")
        assert 0 < rb["pts"] < qb["pts"], rb
        sign = -1 if side == "home" else 1
        assert sign * det["inj_margin"] > 1.5, det["inj_margin"]
        assert det["inj_total"] < 0
        assert det["why"]["steps"] and any(s["label"] == "Injuries" for s in det["why"]["steps"])
        assert all(det["why"]["reasons"][k] for k in det["why"]["reasons"])
        kc = next((g for g in data["board"] if "Kansas City Chiefs" in (g["home"], g["away"])), None)
        if kc:  # IR player who hasn't played is already baked in
            s2 = "home" if kc["home"] == "Kansas City Chiefs" else "away"
            assert all(p["pts"] == 0 for p in kc["injuries"][s2])
        print("injury impact ok: Lions QB out", round(qb["pts"], 2), "pts, game margin moved", round(det["inj_margin"], 2))
        print("why sample:", det["why"]["reasons"].get(side)[:2])
        html = Path(out).read_text()
        assert "/*__DATA__*/null" not in html and "SportsEdge" in html
        json.loads(json.dumps(alld, default=float))
        if save_to:
            Path(save_to).write_text(html)
        print(f"end to end ok: rating corr {corr:.2f}, {sum(len(g['plays']) for g in data['board'])} flagged plays, "
              f"backtest {data['backtest']['games']} games, margin MAE {data['backtest']['margin_mae']:.1f}")
        return out, data


if __name__ == "__main__":
    test_probabilities()
    test_parsers()
    test_end_to_end(sys.argv[1] if len(sys.argv) > 1 else None)
    print("all tests passed")
