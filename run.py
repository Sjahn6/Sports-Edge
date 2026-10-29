#!/usr/bin/env python3
"""SportsEdge command line.

  python run.py            update everything, rebuild the dashboard, open it
  python run.py update     pull new scores, box scores and lines
  python run.py report     rebuild the dashboard from what's saved
  python run.py lines      pull lines only (free from ESPN)
  python run.py doctor     check that ESPN and The Odds API both answer
  python run.py backtest   print the walk forward model check
"""
import argparse
import sys
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from sportsedge import db as D  # noqa: E402
from sportsedge import pipeline as PL  # noqa: E402
from sportsedge import report as R  # noqa: E402
from sportsedge import backtest as BT  # noqa: E402
from sportsedge.leagues import LEAGUES, season_for  # noqa: E402
from sportsedge.util import now_utc  # noqa: E402

CONFIG = HERE / "config.ini"


def open_db(cp):
    return D.connect(HERE / cp.get("general", "database"))


def cmd_update(cp, args, odds=True):
    con = open_db(cp)
    for lg in PL.enabled_leagues(cp, args.league):
        PL.log(f"{lg.name}: updating")
        if not args.lines_only or "espn" in cp.get("odds", "source"):
            PL.update_games(con, lg, cp.getint("model", "seasons_back"),
                            fetch_players=not args.no_players and not args.lines_only, full=args.full,
                            espn_odds="espn" in cp.get("odds", "source"))
        try:
            if not args.lines_only:
                PL.update_injuries(con, lg)
        except Exception as e:
            PL.log(f"  ! injuries: {e}")
        if odds and not args.no_odds and "oddsapi" in cp.get("odds", "source"):
            try:
                PL.update_odds(con, lg, cp)
            except Exception as e:
                PL.log(f"  ! lines: {e}")
    con.close()


def cmd_report(cp, args, open_it=True):
    con = open_db(cp)
    lgs = PL.enabled_leagues(cp, args.league)
    if not lgs:
        return
    out = HERE / cp.get("general", "report")
    _, data = R.write(con, lgs, PL.model_cfg(cp), cp.get("general", "timezone"), out)
    for k in data["order"]:
        d = data["leagues"][k]
        n_plays = sum(len(g["plays"]) for g in d["board"])
        PL.log(f"{d['league']}: {len(d['board'])} upcoming games, {n_plays} flagged bets")
    PL.log(f"Dashboard written to {out}")
    con.close()
    if open_it and not args.no_open:
        webbrowser.open(out.as_uri())


def _doctor_league(lg):
    from sportsedge import espn
    from datetime import timedelta
    now = now_utc()
    s = season_for(lg.key, now)
    if lg.schedule == "weekly":
        games = espn.fetch_week(lg, s, 1)
        print(f"  schedule: OK ({len(games)} games in week 1)")
        for w in range(1, 19):
            gs, od = espn.fetch_week(lg, s, w, with_odds=True)
            if any(not g["completed"] for g in gs):
                up = [g for g in gs if not g["completed"]]
                break
        else:
            up, od = [], []
    else:
        a = now - timedelta(days=4)
        b = now + timedelta(days=4)
        games, od = espn.fetch_dates(lg, f"{a:%Y%m%d}-{b:%Y%m%d}", with_odds=True)
        rng = "OK" if games else "range not supported, will go day by day"
        print(f"  schedule: {rng} ({len(games)} games in the 8 days around today)")
        up = [g for g in games if not g["completed"]]
    n_lines = len({r["game_id"] for r in od})
    book = ", ".join(sorted({r["book_title"] for r in od}))
    print(f"  lines (free): {'OK' if n_lines else 'NONE YET'} ({n_lines} of {len(up)} upcoming games{', ' + book if book else ''})")
    inj = espn.fetch_injuries(lg, "now")
    print(f"  injuries: {'OK' if inj else 'EMPTY'} ({len(inj)} players on {len({r['team'] for r in inj})} teams)")
    done = [g for g in games if g["completed"]]
    if done:
        t, p = espn.fetch_summary(lg, done[0]["id"])
        cats = sorted({r["category"] for r in p})
        print(f"  box score: OK ({len(p)} player stat values; categories: {', '.join(cats)})")


def cmd_doctor(cp, args):
    import requests
    ok = True
    for lg in PL.enabled_leagues(cp, args.league):
        print(f"{lg.name}")
        try:
            _doctor_league(lg)
        except Exception as e:
            ok = False
            print(f"  ESPN: FAILED ({e})")
    key = cp.get("odds", "api_key").strip()
    if "oddsapi" not in cp.get("odds", "source"):
        print("Odds API: not used (source = espn). Optional, only needed to compare every sportsbook.")
    elif not key:
        ok = False
        print("Odds API: source includes oddsapi but there's no key. Get a free one at https://the-odds-api.com")
    else:
        try:
            r = requests.get("https://api.the-odds-api.com/v4/sports", params={"apiKey": key}, timeout=20)
            r.raise_for_status()
            print(f"Odds API: OK, key accepted (credits left: {r.headers.get('x-requests-remaining')}; this check is free)")
        except Exception as e:
            ok = False
            print(f"Odds API: FAILED ({e})")
    print("All good." if ok else "Fix the items above, then run again.")


def cmd_backtest(cp, args):
    con = open_db(cp)
    for lg in PL.enabled_leagues(cp, args.league):
        s = season_for(lg.key, now_utc())
        b = BT.run(con, lg, PL.model_cfg(cp), s)
        print(f"{lg.name} {s}: {b['games']} games tested ({lg.unit})")
        if b["margin_mae"] is not None:
            print(f"  average miss  margin {b['margin_mae']:.1f}   total {b['total_mae']:.1f}")
        v = b["vs_market"]
        if v["n_margin"]:
            print(f"  vs market on {v['n_margin']} games  margin model {v['margin_model']:.1f} / market {v['margin_market']:.1f}")
        n = b["bet_w"] + b["bet_l"]
        if n:
            print(f"  flagged bets {b['bet_w']}-{b['bet_l']}-{b['bet_p']}  hit {b['hit']*100:.1f}%  units {b['units']:+.2f}")
        else:
            print("  no graded bets yet (needs lines saved before kickoff)")
    con.close()


def main():
    ap = argparse.ArgumentParser(description="SportsEdge")
    ap.add_argument("command", nargs="?", default="all",
                    choices=["all", "update", "report", "lines", "doctor", "backtest"])
    ap.add_argument("--league", help="nfl, nba or nhl (default: all in config.ini)")
    ap.add_argument("--no-players", action="store_true", help="skip player box scores")
    ap.add_argument("--no-odds", action="store_true", help="skip pulling lines")
    ap.add_argument("--no-open", action="store_true", help="don't open the dashboard")
    ap.add_argument("--full", action="store_true", help="refetch every week, even finished ones")
    args = ap.parse_args()
    args.lines_only = args.command == "lines"
    cp = PL.load_config(CONFIG)

    if args.command == "doctor":
        return cmd_doctor(cp, args)
    if args.command == "backtest":
        return cmd_backtest(cp, args)
    if args.command in ("all", "update", "lines"):
        cmd_update(cp, args)
    if args.command in ("all", "report"):
        cmd_report(cp, args)


if __name__ == "__main__":
    main()
