"""Walk forward test for this season: each week is predicted using only games
before it, then graded. Grading against lines only works for games whose lines
this app saved before kickoff, so the betting half of the test grows as you
keep running `update` through the season."""
from . import board as B
from . import calibration as CAL
from . import model as M


def run(con, league, cfg, season, preds=None):
    games = B.rows(con, "SELECT * FROM games WHERE league=? AND completed=1 ORDER BY date_utc", (league.key,))
    if preds is None:
        preds = CAL.walk_forward(games, league, {season}, cfg)
    if "win_sd" not in cfg and league.model != "poisson":
        cfg = dict(cfg, win_sd=CAL.run(games, league, season, cfg)["win_sd"])
    rows, bets = [], []
    for p in preds:
        if p["season"] != season:
            continue
        g = p["g"]
        close = B.latest_quotes(con, g["id"], before=g["date_utc"])
        sp, tot = B.consensus(close)
        margin, total = p["margin"], p["total"]
        mkt_margin = None if sp is None or league.model == "poisson" else -sp
        rows.append({"pred_margin": p["pm"], "margin": margin, "market_margin": mkt_margin,
                     "pred_total": p["pt"], "total": total, "market_total": tot})
        if not close:
            continue
        ev = B.evaluate_game(g, p["ratings"], close, league, cfg)
        for pl in ev["plays"]:
            if pl["market"] == "moneyline":
                res = margin if pl["side"] == "home" else -margin
            elif pl["market"] == "spread":
                res = (margin + pl["point"]) if pl["side"] == "home" else (-margin + pl["point"])
            else:
                res = (total - pl["point"]) if pl["side"] == "over" else (pl["point"] - total)
            outcome = "W" if res > 0 else "L" if res < 0 else "P"
            profit = (M.american_to_decimal(pl["price"]) - 1) if outcome == "W" else (-1 if outcome == "L" else 0)
            bets.append({"week": g["week"] or g["date_utc"][5:10], "game": f'{B.short(g, "away")} @ {B.short(g, "home")}',
                         "market": pl["market"], "side": pl["side"], "point": pl["point"], "price": pl["price"],
                         "book": pl["title"], "ev": pl["ev"], "result": outcome, "profit": profit})

    def mae(kp, kt):
        xs = [abs(r[kp] - r[kt]) for r in rows]
        return (sum(xs) / len(xs)) if xs else None

    def mae_pair(kp, kt):
        ps = [r for r in rows if r["market_" + kt] is not None]
        if not ps:
            return None, None, 0
        return (sum(abs(r[kp] - r[kt]) for r in ps) / len(ps),
                sum(abs(r["market_" + kt] - r[kt]) for r in ps) / len(ps), len(ps))

    mm, km, nm = mae_pair("pred_margin", "margin")
    mt, kt, nt = mae_pair("pred_total", "total")
    w = sum(b["result"] == "W" for b in bets)
    l = sum(b["result"] == "L" for b in bets)
    ml = [b for b in bets if b["market"] == "moneyline" and b["price"] <= cfg.get("band_high", -160)
          and b["price"] >= cfg.get("band_low", -260)]
    mw = sum(b["result"] == "W" for b in ml)
    return {
        "season": season, "games": len(rows),
        "margin_mae": mae("pred_margin", "margin"), "total_mae": mae("pred_total", "total"),
        "vs_market": {"margin_model": mm, "margin_market": km, "n_margin": nm,
                      "total_model": mt, "total_market": kt, "n_total": nt},
        "bets": bets, "bet_w": w, "bet_l": l, "bet_p": sum(b["result"] == "P" for b in bets),
        "units": sum(b["profit"] for b in bets),
        "hit": (w / (w + l)) if (w + l) else None,
        "chalk": {"n": len(ml), "w": mw, "units": sum(b["profit"] for b in ml)},
    }
