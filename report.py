"""Writes the dashboard: one self contained HTML file with every league, opened in your browser."""
import json
from pathlib import Path

from . import backtest as BT
from . import board as B
from . import calibration as CAL
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
    board = B.build_board(con, league, cfg, ratings, now, season=season)
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
    bt = BT.run(con, league, cfg, season, preds)
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
        "board": board, "teams": teams, "players": players, "backtest": bt,
    }


def write(con, leagues, cfg, tz, out_path):
    if not isinstance(leagues, (list, tuple)):
        leagues = [leagues]
    data = {"generated": now_utc().isoformat(timespec="seconds"), "tz": tz,
            "order": [lg.key for lg in leagues],
            "leagues": {lg.key: build_data(con, lg, cfg, tz) for lg in leagues}}
    html = TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(data, default=float).replace("</", "<\\/")
    html = html.replace("/*__DATA__*/null", payload)
    Path(out_path).write_text(html, encoding="utf-8")
    return out_path, data
