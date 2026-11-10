"""Writes the dashboard: one self contained HTML file with every league, opened in your browser."""
import json
from pathlib import Path

from . import backtest as BT
from . import board as B
from . import calibration as CAL
from . import clv as CLV
from . import recommend as REC
from . import db as D
from . import model as M
from .leagues import season_for
from .util import now_utc

TEMPLATE = Path(__file__).with_name("report_template.html")


def build_data(con, league, cfg, tz):
    now = now_utc()
    season = season_for(league.key, now)
    games = B.rows(con, "SELECT * FROM games WHERE league=? AND completed=1", (league.key,))
    ratings = M.fit(games, league, now)
    preds = CAL.walk_forward(games, league, {season - 1, season}, cfg)
    calib = CAL.run(games, league, season, cfg, preds)
    cfg = dict(cfg, win_sd=calib["win_sd"], slope=calib["slope"])
    now_iso = now.isoformat(timespec="seconds")
    CLV.grade(con, league, now_iso)
    clv = CLV.status(con, league)
    board = B.build_board(con, league, cfg, ratings, now, season=season)
    cfg = dict(cfg, benched=CLV.bench(board, clv))
    bt = BT.run(con, league, cfg, season, preds)
    hist = bt.pop("_hist", [])
    inj_rows = B.rows(con, "SELECT team, player, position, status, detail, comment, date FROM injuries "
                           "WHERE league=? ORDER BY team, player", (league.key,))
    impact = {}
    for g in board:
        if g.get("injuries"):
            for side in ("home", "away"):
                for p in g["injuries"][side]:
                    impact[(g[side], p["player"])] = p
    for r in inj_rows:
        p = impact.get((r["team"], r["player"]))
        r["pts"] = p["pts"] if p else None
        r["why"] = p["why"] if p else ""
        r["chance"] = p["chance"] if p else None
    teams = B.team_table(con, league, season, ratings)
    players = B.player_data(con, league, season)
    n_games = con.execute("SELECT COUNT(*) FROM games WHERE league=? AND season=? AND completed=1",
                          (league.key, season)).fetchone()[0]
    return {
        "key": league.key, "league": league.name, "season": season, "unit": league.unit,
        "model": league.model, "warn_gap": league.warn_gap,
        "season_label": str(season) if league.key == "nfl" else f"{season - 1}-{str(season)[-2:]}",
        "games_played": n_games,
        "credits": D.get_meta(con, f"{league.key}:odds_credits_remaining"),
        "last_pull": D.get_meta(con, f"{league.key}:odds_last_pull"),
        "odds_source": D.get_meta(con, f"{league.key}:odds_source"),
        "cfg": cfg, "margin_sd": league.margin_sd, "total_sd": league.total_sd,
        "ratings": {"hfa": ratings.hfa, "mu": ratings.mu, "games": ratings.games_used},
        "injuries": inj_rows, "injuries_pulled": D.get_meta(con, f"{league.key}:injuries_pulled"),
        "calibration": calib,
        "_hist": hist,
        "board": board, "teams": teams, "players": players, "backtest": bt,
        "clv": {"status": clv, "recent": CLV.recent(con, league), "min_n": CLV.MIN_N},
        "weather_pulled": D.get_meta(con, f"{league.key}:weather_pulled"),
        "weather_error": D.get_meta(con, f"{league.key}:weather_error"),
    }


def finish(con, leagues, data):
    """Recommended bet per game (needs every sport's track record for small groups),
    then save today's flagged and recommended bets for tracking."""
    now_iso = now_utc().isoformat(timespec="seconds")
    pooled = REC.pool([data["leagues"][lg.key]["backtest"]["calls"] for lg in leagues])
    for lg in leagues:
        L = data["leagues"][lg.key]
        for g in L["board"]:
            g["rec"] = REC.recommend(g, L["backtest"]["calls"], lg.key, pooled)
        CLV.record(con, lg, L["board"], now_iso)
        first = {(r["game_id"], r["market"], r["side"]): r for r in B.rows(
            con, "SELECT game_id, market, side, first_point, first_price, first_utc FROM tracked WHERE league=?",
            (lg.key,))}
        for g in L["board"]:
            for pl in g["plays"] + g["benched_plays"]:
                f = first.get((g["id"], pl["market"], pl["side"]))
                if f:
                    pl["first_point"], pl["first_price"], pl["first_utc"] = f["first_point"], f["first_price"], f["first_utc"]
        L["_recorded"] = True
        L["recs"] = {"status": CLV.rec_status(con, lg),
                     "recent": [{k: r[k] for k in ("label", "game", "kickoff", "market", "first_price", "first_point",
                                                   "first_p", "result", "profit")} for r in CLV.rec_rows(con, lg)[:60]]}


def _local_day(iso, tz):
    from zoneinfo import ZoneInfo
    from datetime import datetime
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo(tz)).date().isoformat()


def top5(con, leagues, data, tz):
    """Today's and upcoming days' 5 most confident picks across every sport, the same
    rule replayed over the last two seasons, and the live record since tracking began."""
    from collections import defaultdict
    now_iso = now_utc().isoformat(timespec="seconds")
    pooled = REC.pool([data["leagues"][lg.key]["backtest"]["calls"] for lg in leagues])
    # live: rank each day's games that haven't started
    byday = defaultdict(list)
    for lg in leagues:
        for g in data["leagues"][lg.key]["board"]:
            if g.get("rec") and g["kickoff"] > now_iso:
                byday[_local_day(g["kickoff"], tz)].append({"lg": lg.key, "g": g, "rec": g["rec"]})
    days = {}
    for d in sorted(byday)[:3]:
        top = REC.top_rank(byday[d])
        days[d] = [{"league": c["lg"], "id": c["g"]["id"], "rank": i + 1} for i, c in enumerate(top)]
        for i, c in enumerate(top):
            c["g"]["top5"] = i + 1
            con.execute("UPDATE tracked SET top5_last=? WHERE league=? AND game_id=? AND market=? AND side=?",
                        (now_iso, c["lg"], c["g"]["id"], c["rec"]["market"], c["rec"]["side"]))
    runs = json.loads(D.get_meta(con, "top5_runs", "[]"))
    runs = (runs + [now_iso])[-3000:]
    D.set_meta(con, "top5_runs", json.dumps(runs))
    con.commit()
    # live record: a game counts if it was in the Top 5 on the last update before it started
    live = []
    for lg in leagues:
        for r in B.rows(con, "SELECT * FROM tracked WHERE league=? AND top5_last IS NOT NULL AND result IS NOT NULL", (lg.key,)):
            before = [t for t in runs if t < r["kickoff"]]
            if before and r["top5_last"] == before[-1]:
                live.append(r)
    # replay: the same rule on every day of the last two seasons
    hist = defaultdict(list)
    for lg in leagues:
        L = data["leagues"][lg.key]
        for h in L.pop("_hist", []):
            r = REC.recommend({"best": h["best"]}, L["backtest"]["calls"], lg.key, pooled)
            if not r:
                continue
            res, profit = h["res"][r["key"]]
            hist[_local_day(h["date"], tz)].append({"lg": lg.key, "rec": r, "res": res, "profit": profit, "game": h["game"],
                                                   "date": h["date"][:10]})
    picks = []
    for d, cs in hist.items():
        for i, c in enumerate(REC.top_rank(cs)):
            picks.append(dict(c, rank=i + 1, day=d))

    def summ(ps):
        w = sum(p["res"] == "W" for p in ps)
        l = sum(p["res"] == "L" for p in ps)
        u = sum(p["profit"] for p in ps)
        need = sum(M.implied(p["rec"]["price"]) for p in ps) / len(ps) if ps else None
        return {"n": len(ps), "w": w, "l": l, "p": len(ps) - w - l, "units": u, "need": need,
                "hit": w / (w + l) if w + l else None, "roi": u / len(ps) if ps else None}
    by_rank = [dict(summ([p for p in picks if p["rank"] == k]), rank=k) for k in range(1, REC.TOP_N + 1)]
    top2 = summ([p for p in picks if p["rank"] <= 2])
    by_lg = {k: summ([p for p in picks if p["lg"] == k]) for k in [lg.key for lg in leagues]}
    live_s = summ([{"res": r["result"], "profit": r["profit"] or 0, "rec": {"price": r["first_price"]}} for r in live])
    return {"days": days, "slack": REC.TOP_SLACK, "n": REC.TOP_N,
            "replay": {"all": summ(picks), "top2": top2, "by_rank": by_rank, "by_league": by_lg,
                       "days": len({p["day"] for p in picks})},
            "live": live_s}


def write(con, leagues, cfg, tz, out_path):
    if not isinstance(leagues, (list, tuple)):
        leagues = [leagues]
    data = {"generated": now_utc().isoformat(timespec="seconds"), "tz": tz,
            "order": [lg.key for lg in leagues],
            "leagues": {lg.key: build_data(con, lg, cfg, tz) for lg in leagues}}
    finish(con, leagues, data)
    data["top5"] = top5(con, leagues, data, tz)
    for k in data["leagues"]:
        data["leagues"][k].pop("_hist", None)
        data["leagues"][k].pop("_recorded", None)
    html = TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(data, default=float).replace("</", "<\\/")
    html = html.replace("/*__DATA__*/null", payload)
    Path(out_path).write_text(html, encoding="utf-8")
    return out_path, data
