"""Walk forward test: each week is predicted using only games before it, then graded.

Picks are graded against the closing line saved before each game (from ESPN's
history for past seasons, from the app's own pulls going forward). Injury reports
from past games aren't kept, so past picks are graded without injury adjustments.
"""
from collections import defaultdict

from . import board as B
from . import calibration as CAL
from . import model as M


def _grade(pl, margin, total):
    if pl["market"] == "moneyline":
        res = margin if pl["side"] == "home" else -margin
    elif pl["market"] == "spread":
        res = (margin + pl["point"]) if pl["side"] == "home" else (-margin + pl["point"])
    else:
        res = (total - pl["point"]) if pl["side"] == "over" else (pl["point"] - total)
    outcome = "W" if res > 0 else "L" if res < 0 else "P"
    profit = (M.american_to_decimal(pl["price"]) - 1) if outcome == "W" else (-1 if outcome == "L" else 0)
    return outcome, profit


def _summary(bets, cfg):
    w = sum(b["result"] == "W" for b in bets)
    l = sum(b["result"] == "L" for b in bets)
    band = [b for b in bets if b["market"] == "moneyline"
            and cfg.get("band_low", -260) <= b["price"] <= cfg.get("band_high", -160)]
    by_mkt = {}
    for m in ("moneyline", "spread", "total"):
        bm = [b for b in bets if b["market"] == m]
        mw = sum(b["result"] == "W" for b in bm)
        ml = sum(b["result"] == "L" for b in bm)
        by_mkt[m] = {"n": len(bm), "w": mw, "l": ml, "units": sum(b["profit"] for b in bm),
                     "hit": mw / (mw + ml) if (mw + ml) else None}
    return {
        "bet_w": w, "bet_l": l, "bet_p": sum(b["result"] == "P" for b in bets),
        "units": sum(b["profit"] for b in bets), "hit": (w / (w + l)) if (w + l) else None,
        "roi": (sum(b["profit"] for b in bets) / len(bets)) if bets else None,
        "chalk": {"n": len(band), "w": sum(b["result"] == "W" for b in band),
                  "units": sum(b["profit"] for b in band)},
        "by_market": by_mkt,
    }


def run(con, league, cfg, season, preds=None):
    games = B.rows(con, "SELECT * FROM games WHERE league=? AND completed=1 ORDER BY date_utc", (league.key,))
    if preds is None:
        preds = CAL.walk_forward(games, league, {season - 1, season}, cfg)
    if "win_sd" not in cfg:
        c = CAL.run(games, league, season, cfg, preds)
        cfg = dict(cfg, win_sd=c["win_sd"], slope=c["slope"])

    # the live board stretches the model to the market's spread of outcomes each slate;
    # do the same per period here so past picks are made the way today's are
    periods = defaultdict(list)
    for p in preds:
        periods[(p["season"], p["period"])].append(p)

    # straight calls, price ignored: the side the model thinks is more likely in each market
    EDGES = {"moneyline": [0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 1.01],
             "spread": [0.5, 0.53, 0.56, 0.6, 1.01], "total": [0.5, 0.53, 0.56, 0.6, 1.01]}
    calls = {m: {"n": 0, "right": 0, "buckets": [{"lo": e[i], "hi": min(e[i + 1], 1.0), "n": 0, "right": 0}
                                                  for i in range(len(e) - 1)]} for m, e in EDGES.items()}
    rows, bets, hist = [], [], []
    for key in sorted(periods):
        grp = periods[key]
        r0 = grp[0]["ratings"]
        pcfg = dict(cfg, slope=B.market_scale(league, r0, [p["g"] for p in grp], con))
        for p in grp:
            g = p["g"]
            close = B.latest_quotes(con, g["id"], before=g["date_utc"])
            sp, tot = B.consensus(close)
            margin, total = p["margin"], p["total"]
            mkt_margin = None if sp is None or league.model == "poisson" else -sp
            rows.append({"season": p["season"], "pred_margin": p["pm"], "margin": margin,
                         "market_margin": mkt_margin, "pred_total": p["pt"], "total": total,
                         "market_total": tot})
            if not close:
                continue
            ev = B.evaluate_game(g, p["ratings"], close, league, pcfg)
            for m, keys, sides in (("moneyline", ("home_ml", "away_ml"), ("home", "away")),
                                   ("spread", ("home", "away"), ("home", "away")), ("total", ("over", "under"), ("over", "under"))):
                a, b2 = ev["best"].get(keys[0]), ev["best"].get(keys[1])
                if not a or not b2:
                    continue
                take = 0 if a["p_cover"] >= b2["p_cover"] else 1
                c = (a, b2)[take]
                res, _ = _grade({"market": m, "side": sides[take], "point": c["point"], "price": c["price"]}, margin, total)
                if res == "P":
                    continue
                cm = calls[m]
                cm["n"] += 1
                cm["right"] += res == "W"
                for bk in cm["buckets"]:
                    if bk["lo"] <= c["p_cover"] < bk["hi"] or (bk["hi"] == 1.0 and c["p_cover"] >= 1.0):
                        bk["n"] += 1
                        bk["right"] += res == "W"
                        break
            hb, hr = {}, {}
            for kk, mk, sd in (("home_ml", "moneyline", "home"), ("away_ml", "moneyline", "away"), ("home", "spread", "home"),
                               ("away", "spread", "away"), ("over", "total", "over"), ("under", "total", "under")):
                x = ev["best"].get(kk)
                if x:
                    hb[kk] = {"p_cover": x["p_cover"], "price": x["price"], "point": x["point"], "ev": x["ev"], "book": "", "title": ""}
                    hr[kk] = _grade({"market": mk, "side": sd, "point": x["point"], "price": x["price"]}, margin, total)
            hist.append({"gid": g["id"], "date": g["date_utc"], "season": p["season"], "best": hb, "res": hr,
                         "game": f'{B.short(g, "away")} @ {B.short(g, "home")}', "g": {k: g[k] for k in ("home", "away", "home_abbr", "away_abbr", "home_short", "away_short") if k in g}})
            picks, _ = B.select_picks(ev["plays"], set(cfg.get("benched") or []))
            pick_ids = {id(x) for x in picks}
            for pl in ev["plays"]:
                outcome, profit = _grade(pl, margin, total)
                bets.append({"season": p["season"], "week": g["week"] or g["date_utc"][5:10],
                             "date": g["date_utc"][:10],
                             "game": f'{B.short(g, "away")} @ {B.short(g, "home")}',
                             "market": pl["market"], "side": pl["side"], "point": pl["point"],
                             "price": pl["price"], "book": pl["title"], "ev": pl["ev"],
                             "label": (B.short(g, pl["side"]) + (" to win" if pl["market"] == "moneyline"
                                       else (" " + (f"{pl['point']:+g}" if pl["point"] else "PK"))))
                                      if pl["side"] in ("home", "away")
                                      else f'{pl["side"].title()} {pl["point"]:g}',
                             "result": outcome, "profit": profit, "pick": id(pl) in pick_ids})

    def mae_pair(rs, kp, kt):
        ps = [r for r in rs if r["market_" + kt] is not None]
        if not ps:
            return None, None, 0
        return (sum(abs(r[kp] - r[kt]) for r in ps) / len(ps),
                sum(abs(r["market_" + kt] - r[kt]) for r in ps) / len(ps), len(ps))

    def mae(rs, kp, kt):
        return (sum(abs(r[kp] - r[kt]) for r in rs) / len(rs)) if rs else None

    # grade under today's rules: bet types on the bench (see clv.py) aren't Picks
    benched = set(cfg.get("benched") or [])
    all_bets = bets
    bets = [b for b in all_bets if b["pick"]]   # one per game, no favorites, no benched types

    seasons = []
    for s in sorted({r["season"] for r in rows}):
        rs = [r for r in rows if r["season"] == s]
        bs = [b for b in bets if b["season"] == s]
        label = str(s) if league.key == "nfl" else f"{s - 1}-{str(s)[-2:]}"
        seasons.append({"season": s, "label": label, "games": len(rs),
                        "lined": sum(1 for r in rs if r["market_total"] is not None or r["market_margin"] is not None),
                        **_summary(bs, cfg)})

    mm, km, nm = mae_pair(rows, "pred_margin", "margin")
    mt, kt, nt = mae_pair(rows, "pred_total", "total")
    bets.sort(key=lambda b: b["date"], reverse=True)
    return {
        "season": season, "games": len(rows),
        "margin_mae": mae(rows, "pred_margin", "margin"), "total_mae": mae(rows, "pred_total", "total"),
        "vs_market": {"margin_model": mm, "margin_market": km, "n_margin": nm,
                      "total_model": mt, "total_market": kt, "n_total": nt},
        "seasons": seasons,
        "bets": bets[:400],
        **_summary(bets, cfg),
        "by_market": _summary(all_bets, cfg)["by_market"],   # every bet type, benched ones too
        "benched": sorted(benched),
        "calls": calls,
        "_hist": hist,   # every replayed game's options, used for the Top 5 replay (not sent to the page)
        "old_rules": {k: v for k, v in _summary(all_bets, cfg).items() if k != "by_market"},
    }
