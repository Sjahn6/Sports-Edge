"""Builds everything the dashboard shows: the game board with edges, team tables,
player trends. Pure data in, plain dicts out."""
from datetime import timedelta
from statistics import median

from . import model as M
from .util import parse_iso

# Stats to feature per player category (NFL). Unknown categories fall back to
# whatever numeric stats ESPN sends.
FEATURED = {
    "passing": ["completions", "passingAttempts", "passingYards", "passingTouchdowns", "interceptions"],
    "rushing": ["rushingAttempts", "rushingYards", "rushingTouchdowns", "longRushing"],
    "receiving": ["receptions", "receivingTargets", "receivingYards", "receivingTouchdowns"],
    "defensive": ["totalTackles", "sacks", "tacklesForLoss", "passesDefended"],
    "interceptions": ["interceptions"],
    "kicking": ["fieldGoalsMade", "fieldGoalAttempts", "totalKickingPoints"],
}
FEATURED_BY_LEAGUE = {
    "nfl": FEATURED,
    "nba": {"players": ["minutes", "points", "rebounds", "assists", "threePointFieldGoalsMade", "steals", "blocks"]},
    "nhl": {"forwards": ["goals", "assists", "shotsTotal", "timeOnIce", "plusMinus"],
            "defenses": ["goals", "assists", "shotsTotal", "timeOnIce", "blockedShots"],
            "goalies": ["saves", "shotsAgainst", "goalsAgainst", "savePercentage", "timeOnIce"]},
}
STAT_LABELS = {
    "completions": "Cmp", "passingAttempts": "Att", "passingYards": "Pass Yds",
    "passingTouchdowns": "Pass TD", "interceptions": "INT", "rushingAttempts": "Car",
    "rushingYards": "Rush Yds", "rushingTouchdowns": "Rush TD", "longRushing": "Long",
    "receptions": "Rec", "receivingTargets": "Tgt", "receivingYards": "Rec Yds",
    "receivingTouchdowns": "Rec TD", "totalTackles": "Tkl", "sacks": "Sacks",
    "tacklesForLoss": "TFL", "passesDefended": "PD", "fieldGoalsMade": "FGM",
    "fieldGoalAttempts": "FGA", "totalKickingPoints": "Pts",
    "minutes": "Min", "points": "Pts", "rebounds": "Reb", "assists": "Ast", "threePointFieldGoalsMade": "3PM",
    "steals": "Stl", "blocks": "Blk", "goals": "G", "shotsTotal": "SOG", "timeOnIce": "TOI",
    "plusMinus": "+/-", "blockedShots": "Blk", "saves": "Saves", "shotsAgainst": "SA",
    "goalsAgainst": "GA", "savePercentage": "Sv%",
}


def rows(con, sql, args=()):
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def latest_quotes(con, game_id, before=None):
    """Latest price per (book, market, side) for one game, optionally only snapshots
    taken before `before` (closing line)."""
    q = "SELECT * FROM odds WHERE game_id=?"
    args = [game_id]
    if before:
        q += " AND fetched_utc <= ?"
        args.append(before)
    q += " ORDER BY fetched_utc"
    out = {}
    for r in rows(con, q, args):
        out[(r["book"], r["market"], r["side"])] = r
    return out


def consensus(quotes):
    """Median home spread point and median total across books."""
    hs = [q["point"] for (b, m, s), q in quotes.items() if m == "spreads" and s == "home" and q["point"] is not None]
    ts = [q["point"] for (b, m, s), q in quotes.items() if m == "totals" and s == "over" and q["point"] is not None]
    return (median(hs) if hs else None), (median(ts) if ts else None)


def _side(probs, take_above, price, other_price, kelly_frac):
    above, push, below = probs
    p_win = above if take_above else below
    ev = M.expected_value(p_win, push, price)
    nv = M.no_vig(price, other_price)[0] if other_price is not None else M.implied(price)
    return {
        "p_win": p_win, "p_push": push,
        "p_cover": p_win / (1 - push) if push < 1 else 0.0,   # ignoring pushes
        "nv": nv, "ev": ev, "kelly": M.kelly(p_win, push, price, kelly_frac),
        "fair": M.to_american(p_win / (1 - push)) if push < 1 else None,
    }


def _market_p_home(quotes):
    """Median no vig home win chance across books' moneylines."""
    ps = []
    for (b, m, s), q in quotes.items():
        if m == "h2h" and s == "home" and (b, "h2h", "away") in quotes:
            ps.append(M.no_vig(q["price"], quotes[(b, "h2h", "away")]["price"])[0])
    return median(ps) if ps else None


def build_game(league, cfg, ratings, g, quotes, adj_margin=0.0, adj_total=0.0, adj_home_pts=None):
    """Projection for one game after adjustments and the market blend, as a GameDist."""
    from . import gamedist as GD
    blend = cfg["market_blend"]
    neutral = bool(g["neutral"])
    mkt_spread, mkt_total = consensus(quotes)
    slope = cfg.get("slope") or 1.0     # undo the ratings' pull toward average
    if league.model == "poisson":
        lh = ratings.points(g["home"], g["away"], True, neutral)
        la = ratings.points(g["away"], g["home"], False, neutral)
        t0, d0 = lh + la, (lh - la) * slope
        lh, la = (t0 + d0) / 2, (t0 - d0) / 2
        if adj_home_pts:
            lh += adj_home_pts[0]
            la += adj_home_pts[1]
        home_ot = cfg.get("home_ot", 0.52)
        model = GD.PoissonGame(lh, la, home_ot)
        mp = _market_p_home(quotes)
        if mp is None and mkt_total is None:
            return model, model, mkt_spread, mkt_total
        t = model.exp_total if mkt_total is None else (1 - blend) * model.exp_total + blend * mkt_total
        ph = model.p_home_win() if mp is None else (1 - blend) * model.p_home_win() + blend * mp
        lh2, la2 = GD.poisson_from_targets(t, ph, home_ot)
        return model, GD.PoissonGame(lh2, la2, home_ot), mkt_spread, mkt_total
    base_m = ratings.margin(g["home"], g["away"], neutral) * slope + adj_margin
    base_t = ratings.total(g["home"], g["away"], neutral) + adj_total
    # The calibrated spread comes from model-only predictions, which are noisier than a
    # projection blended with DraftKings. Using it on blended games squashes every win
    # chance toward 50/50 and makes all underdogs look underpriced, so blended games use
    # the league's normal spread around a market-quality projection.
    model_sd = cfg.get("win_sd") or league.margin_sd
    win_sd = league.margin_sd if mkt_spread is not None else model_sd
    # Better still: read DraftKings' own spread-to-moneyline curve off this game, so a
    # moneyline only shows an edge when the model really disagrees about the margin.
    mp = _market_p_home(quotes)
    if mkt_spread is not None and mp is not None and abs(mkt_spread) >= 1.5 and 0.02 < mp < 0.98:
        from statistics import NormalDist
        z = NormalDist().inv_cdf(mp)
        if z * (-mkt_spread) > 0:   # favorite on the spread is also the moneyline favorite
            win_sd = min(max(abs(mkt_spread) / abs(z), league.margin_sd * 0.6), league.margin_sd * 1.4)
    model = GD.NormalGame(base_m, base_t, league.margin_sd, league.total_sd, model_sd)
    em = base_m if mkt_spread is None else (1 - blend) * base_m + blend * (-mkt_spread)
    et = base_t if mkt_total is None else (1 - blend) * base_t + blend * mkt_total
    return model, GD.NormalGame(em, et, league.margin_sd, league.total_sd, win_sd), mkt_spread, mkt_total


def evaluate_game(g, ratings, quotes, league, cfg, inj=None, form=None, rest=None):
    """inj: injuries.game_adjustment() result, or None (backtest has no history).
    form: {team: {"last3": avg margin, "season": avg margin, "n": games}}
    rest: {"home": penalty, "away": penalty} for back to backs."""
    from . import gamedist as GD
    kf = cfg["kelly_fraction"]
    neutral = bool(g["neutral"])
    base_margin = ratings.margin(g["home"], g["away"], neutral)
    base_total = ratings.total(g["home"], g["away"], neutral)
    inj_margin = inj["margin"] if inj else 0.0
    inj_total = inj["total"] if inj else 0.0
    rest = rest or {"home": 0.0, "away": 0.0}
    rest_margin = -rest["home"] + rest["away"]
    # per team scoring shifts (used directly by the goals model)
    dh = (-inj["home"]["off"] + inj["away"]["def"]) if inj else 0.0
    da = (-inj["away"]["off"] + inj["home"]["def"]) if inj else 0.0
    dh -= rest["home"] / 2
    da -= rest["away"] / 2
    dh += rest["away"] / 2
    da += rest["home"] / 2
    model, game, mkt_spread, mkt_total = build_game(
        league, cfg, ratings, g, quotes, inj_margin + rest_margin, inj_total, (dh, da))
    model_margin, model_total = model.exp_margin, model.exp_total
    exp_margin, exp_total = game.exp_margin, game.exp_total

    books = sorted({b for (b, m, s) in quotes})
    spread_rows, total_rows, ml_rows = [], [], []
    for b in books:
        h, a = quotes.get((b, "spreads", "home")), quotes.get((b, "spreads", "away"))
        if h and a and h["point"] is not None and a["point"] is not None:
            # home covers when margin > -home_point ; away covers when margin < away_point
            hs = _side(game.margin_probs(-h["point"]), True, h["price"], a["price"], kf)
            as_ = _side(game.margin_probs(a["point"]), False, a["price"], h["price"], kf)
            spread_rows.append({"book": b, "title": h["book_title"],
                                "home": {"point": h["point"], "price": h["price"], **hs},
                                "away": {"point": a["point"], "price": a["price"], **as_}})
        o, u = quotes.get((b, "totals", "over")), quotes.get((b, "totals", "under"))
        if o and u and o["point"] is not None:
            os_ = _side(game.total_probs(o["point"]), True, o["price"], u["price"], kf)
            us_ = _side(game.total_probs(u["point"]), False, u["price"], o["price"], kf)
            total_rows.append({"book": b, "title": o["book_title"],
                               "over": {"point": o["point"], "price": o["price"], **os_},
                               "under": {"point": u["point"], "price": u["price"], **us_}})
        hm, am = quotes.get((b, "h2h", "home")), quotes.get((b, "h2h", "away"))
        if hm and am:
            mp = game.margin_probs(0)
            hside = _side(mp, True, hm["price"], am["price"], kf)
            aside = _side(mp, False, am["price"], hm["price"], kf)
            if mp[1] > 0:   # normal model: a 0 margin means overtime, split it
                for sd_, add in ((hside, mp[1] / 2), (aside, mp[1] / 2)):
                    sd_["p_win"] += add
                    sd_["p_push"] = 0.0
                    sd_["p_cover"] = sd_["p_win"]
                hside["ev"] = M.expected_value(hside["p_win"], 0, hm["price"])
                aside["ev"] = M.expected_value(aside["p_win"], 0, am["price"])
                hside["kelly"] = M.kelly(hside["p_win"], 0, hm["price"], kf)
                aside["kelly"] = M.kelly(aside["p_win"], 0, am["price"], kf)
                hside["fair"], aside["fair"] = M.to_american(hside["p_win"]), M.to_american(aside["p_win"])
            if isinstance(game, GD.NormalGame):   # calibrated win spread for outright wins
                pw = game.p_home_win()
                for sd_, p, pr in ((hside, pw, hm["price"]), (aside, 1 - pw, am["price"])):
                    sd_.update(p_win=p, p_push=0.0, p_cover=p, ev=M.expected_value(p, 0, pr),
                               kelly=M.kelly(p, 0, pr, kf), fair=M.to_american(p))
            ml_rows.append({"book": b, "title": hm["book_title"],
                            "home": {"point": None, "price": hm["price"], **hside},
                            "away": {"point": None, "price": am["price"], **aside}})

    def best(rs, side):
        cands = [dict(r[side], book=r["book"], title=r["title"]) for r in rs]
        return max(cands, key=lambda c: c["ev"]) if cands else None

    plays = []
    for market, rs, sides in (("spread", spread_rows, ("home", "away")), ("total", total_rows, ("over", "under")),
                              ("moneyline", ml_rows, ("home", "away"))):
        for s_ in sides:
            bst = best(rs, s_)
            # two bars: expected return, and a real win-chance edge over the price. The
            # second keeps long-shot underdogs (whose payouts magnify small errors) from
            # crowding out favorites.
            if bst and bst["ev"] >= cfg["min_ev"] and bst["p_cover"] - M.implied(bst["price"]) >= cfg.get("min_edge", 0.03):
                plays.append({"market": market, "side": s_, **bst})
    plays.sort(key=lambda p: -p["ev"])

    tp = cfg.get("target_price", -200)
    alt = {"price": tp,
           "home": GD.alt_line(game, tp, "spread", "home"), "away": GD.alt_line(game, tp, "spread", "away"),
           "over": GD.alt_line(game, tp, "total", "over"), "under": GD.alt_line(game, tp, "total", "under")}

    out = {
        "id": g["id"], "kickoff": g["date_utc"], "week": g["week"],
        "home": g["home"], "away": g["away"], "home_abbr": g["home_abbr"], "away_abbr": g["away_abbr"],
        "home_short": short(g, "home"), "away_short": short(g, "away"),
        "home_color": g.get("home_color"), "away_color": g.get("away_color"),
        "neutral": neutral,
        "base_margin": base_margin, "base_total": base_total,
        "inj_margin": inj_margin, "inj_total": inj_total, "rest": rest, "rest_margin": rest_margin,
        "model_margin": model_margin, "model_total": model_total,
        "market_spread": mkt_spread, "market_total": mkt_total,
        "exp_margin": exp_margin, "exp_total": exp_total,
        "model_spread": -model_margin,
        "spreads": spread_rows, "totals": total_rows, "moneylines": ml_rows,
        "p_home": game.p_home_win(), "p_home_model": model.p_home_win(), "alt": alt,
        "lam": [game.lam_home, game.lam_away] if isinstance(game, GD.PoissonGame) else None,
        "best": {"home": best(spread_rows, "home"), "away": best(spread_rows, "away"),
                 "over": best(total_rows, "over"), "under": best(total_rows, "under"),
                 "home_ml": best(ml_rows, "home"), "away_ml": best(ml_rows, "away")},
        "plays": plays,
        "disagree": None if mkt_spread is None or league.model == "poisson" else abs(-model_margin - mkt_spread),
        "injuries": None if not inj else {"home": inj["home"]["players"], "away": inj["away"]["players"],
                                          "home_off": inj["home"]["off"], "home_def": inj["home"]["def"],
                                          "away_off": inj["away"]["off"], "away_def": inj["away"]["def"]},
    }
    if league.model == "poisson":
        mp = _market_p_home(quotes)
        out["disagree_p"] = None if mp is None else abs(model.p_home_win() - mp)
    out["why"] = explain(g, ratings, out, league, cfg, form or {})
    return out


def short(g, side):
    from .util import nickname
    return g.get(side + "_short") or nickname(g[side]) or g.get(side + "_abbr") or g[side]


def _fmt_line(p):
    return "PK" if p == 0 else f"{p:+.1f}".replace(".0", "")


def explain(g, r, ev, league, cfg, form):
    """The numbers behind each game, plus plain English reasons for the top picks."""
    neutral = ev["neutral"]
    U = league.unit
    HOME = {"nfl": "Home field", "nba": "Home court", "nhl": "Home ice"}.get(league.key, "Home edge")
    small = league.margin_sd * 0.03          # smallest effect worth a sentence
    H, A = short(g, "home"), short(g, "away")
    hp = r.points(g["home"], g["away"], True, neutral)
    ap = r.points(g["away"], g["home"], False, neutral)
    if ev["injuries"]:
        i = ev["injuries"]
        hp += -i["home_off"] + i["away_def"]
        ap += -i["away_off"] + i["home_def"]
    hp += (-ev["rest"]["home"] + ev["rest"]["away"]) / 2
    ap += (-ev["rest"]["away"] + ev["rest"]["home"]) / 2
    fh, fa = form.get(g["home"], {}), form.get(g["away"], {})
    factors = {
        "home_rating": r.rating.get(g["home"], 0.0), "away_rating": r.rating.get(g["away"], 0.0),
        "hfa": 0.0 if neutral else r.hfa,
        "home_off": r.offense.get(g["home"], 0.0), "home_def": r.defense.get(g["home"], 0.0),
        "away_off": r.offense.get(g["away"], 0.0), "away_def": r.defense.get(g["away"], 0.0),
        "home_pts": hp, "away_pts": ap, "mu": r.mu, "unit": U,
        "home_form": fh, "away_form": fa,
        "blend": cfg["market_blend"], "margin_sd": league.margin_sd, "total_sd": league.total_sd,
    }

    steps = []  # how the projected margin was built, in order
    steps.append({"label": f"{H} rating minus {A} rating",
                  "value": factors["home_rating"] - factors["away_rating"]})
    if not neutral:
        steps.append({"label": f"{HOME} for {H}", "value": factors["hfa"]})
    if ev["inj_margin"]:
        steps.append({"label": "Injuries", "value": ev["inj_margin"]})
    for side, ab in (("home", H), ("away", A)):
        if ev["rest"][side]:
            steps.append({"label": f"{ab} on a back to back", "value": ev["rest"][side] * (-1 if side == "home" else 1)})
    steps.append({"label": "Model margin", "value": ev["model_margin"], "subtotal": True})
    if league.model == "poisson":
        if ev["exp_margin"] != ev["model_margin"]:
            steps.append({"label": f"Blended {int(round(cfg['market_blend'] * 100))}% toward the market's moneyline and total",
                          "value": ev["exp_margin"] - ev["model_margin"]})
    elif ev["market_spread"] is not None:
        steps.append({"label": f"Blended {int(round(cfg['market_blend'] * 100))}% toward market ({H} {_fmt_line(ev['market_spread'])})",
                      "value": ev["exp_margin"] - ev["model_margin"]})
    steps.append({"label": f"Projected {H} margin", "value": ev["exp_margin"], "subtotal": True})

    def team_lines(team, abbr, off, opp_def, side):
        out = []
        if abs(off) >= small * 4:
            out.append(f"{abbr} offense scores {abs(off):.{1 if U == 'pts' else 2}f} {U} a game {'more' if off > 0 else 'less'} than average")
        if abs(opp_def) >= small * 4:
            out.append(f"facing a defense that allows {abs(opp_def):.{1 if U == 'pts' else 2}f} {'more' if opp_def > 0 else 'fewer'} than average")
        return out

    def form_line(abbr, f):
        if not f or f.get("n", 0) < 3:
            return None
        d = f["last3"] - f["season"]
        if abs(d) < league.margin_sd * 0.3:
            return None
        return (f"{abbr} has been {'hot' if d > 0 else 'cold'}: {f['last3']:+.1f} avg margin the last 3 "
                f"vs {f['season']:+.1f} on the season")

    def inj_lines(side, abbr):
        if not ev["injuries"]:
            return []
        out = []
        for p in ev["injuries"][side]:
            if p["pts"] >= small:
                out.append(f"{abbr} {p['pos']} {p['player']} is {p['status'].lower()} "
                           f"({p['why']}), worth {p['pts']:.{1 if U == 'pts' else 2}f} {U}")
        return out[:3]

    reasons = {}
    for key in ("home", "away", "over", "under", "home_ml", "away_ml"):
        b = ev["best"][key]
        if not b:
            continue
        lines = []
        if key.endswith("_ml"):
            side = key[:-3]
            abbr, opp = (H, A) if side == "home" else (A, H)
            team_margin = ev["exp_margin"] if side == "home" else -ev["exp_margin"]
            p = b["p_win"]
            if league.model == "poisson":
                me, them = (hp, ap) if side == "home" else (ap, hp)
                lines.append(f"Projection: {abbr} {me:.2f} goals, {opp} {them:.2f}. Counting every possible score "
                             f"(and overtime when tied), that's a {p * 100:.1f}% chance {abbr} wins.")
            else:
                lines.append(f"Projection: {abbr} {'wins' if team_margin > 0 else 'loses'} by {abs(team_margin):.1f}, "
                             f"which the model turns into a {p * 100:.1f}% chance to win outright.")
            rd = (factors["home_rating"] - factors["away_rating"]) * (1 if side == "home" else -1)
            lines.append(f"Power ratings: {abbr} {factors[side + '_rating']:+.{1 if U == 'pts' else 2}f} vs {opp} "
                         f"{factors[('away' if side == 'home' else 'home') + '_rating']:+.{1 if U == 'pts' else 2}f}"
                         + ("" if neutral else f", {HOME.lower()} {factors['hfa']:+.{1 if U == 'pts' else 2}f} for {H}") + ".")
            lines += inj_lines("away" if side == "home" else "home", opp)
            for sd_, ab in (("home", H), ("away", A)):
                if ev["rest"][sd_]:
                    lines.append(f"{ab} is on the second night of a back to back.")
            lines += inj_lines(side, abbr)
            fl = form_line(abbr, fh if side == "home" else fa)
            if fl:
                lines.append(fl + ".")
            be = M.implied(b["price"])
            lines.append(f"At {b['price']:+d} you need to win {be * 100:.1f}% to break even. "
                         f"Model says {p * 100:.1f}%, a {(p - be) * 100:+.1f}% cushion. Fair price {b['fair']:+d}.")
            if b["price"] < 0:
                lines.append(f"Risk ${-b['price']} to win $100. EV {b['ev'] * 100:+.1f}% per dollar risked.")
            reasons[key] = lines
            continue
        if key in ("home", "away"):
            abbr, opp = (H, A) if key == "home" else (A, H)
            team_margin = ev["exp_margin"] if key == "home" else -ev["exp_margin"]
            cover_by = team_margin + b["point"]
            lines.append(f"Projection: {abbr} {'wins' if team_margin > 0 else 'loses'} by {abs(team_margin):.1f}. "
                         f"Taking {abbr} {_fmt_line(b['point'])} gives a {abs(cover_by):.1f} {U[:-1] if U == 'pts' else 'goal'} "
                         f"{'cushion' if cover_by > 0 else 'shortfall'}.")
            rd = (factors["home_rating"] - factors["away_rating"]) * (1 if key == "home" else -1)
            lines.append(f"Power ratings: {abbr} {factors[key + '_rating']:+.1f} vs {opp} "
                         f"{factors[('away' if key == 'home' else 'home') + '_rating']:+.1f} "
                         f"({'edge' if rd > 0 else 'gap'} of {abs(rd):.1f} on a neutral field)"
                         + ("" if neutral else f", {HOME.lower()} {factors['hfa']:+.1f} for {H}") + ".")
            other = "away" if key == "home" else "home"
            lines += inj_lines(other, opp)
            lines += inj_lines(key, abbr)
            fl = form_line(abbr, fh if key == "home" else fa)
            if fl:
                lines.append(fl + ".")
            fo = form_line(opp, fa if key == "home" else fh)
            if fo:
                lines.append(fo + ".")
            if league.model == "poisson":
                need = "win by 2 or more" if b["point"] < 0 else "lose by 1 at most, or win"
                lines.append(f"Puck line {_fmt_line(b['point'])}: {abbr} needs to {need}. "
                             f"{ev['lam'][0 if key == 'home' else 1]:.2f} vs {ev['lam'][1 if key == 'home' else 0]:.2f} projected goals.")
            crosses = [k for k in league.key_numbers if min(abs(b["point"]), abs(team_margin)) < k < max(abs(b["point"]), abs(team_margin))]
            if crosses and cover_by > 0:
                lines.append(f"The gap crosses key number{'s' if len(crosses) > 1 else ''} "
                             f"{', '.join(map(str, crosses))}, where NFL games end most often.")
            elif cover_by > 0 and abs(cover_by) < 1.5:
                lines.append("Thin cushion: the edge comes mostly from price, not projection.")
        else:
            diff = ev["exp_total"] - b["point"] if key == "over" else b["point"] - ev["exp_total"]
            blended = "" if ev["market_total"] is None else f", {ev['exp_total']:.1f} after blending with the market"
            lines.append(f"Projection: {H} {hp:.1f}, {A} {ap:.1f} = {hp + ap:.1f}{blended}, "
                         f"vs a line of {b['point']:g} ({diff:+.1f} toward the {key}).")
            lines += team_lines(g["home"], H, factors["home_off"], factors["away_def"], "home")
            lines += team_lines(g["away"], A, factors["away_off"], factors["home_def"], "away")
            if ev["inj_total"]:
                lines.append(f"Injuries move the total {ev['inj_total']:+.{1 if U == 'pts' else 2}f}.")
                lines += inj_lines("home", H)[:2] + inj_lines("away", A)[:2]
        lines.append(f"Model gives {b['p_cover'] * 100:.1f}% to win vs {b['nv'] * 100:.1f}% priced in at "
                     f"{b['title']} ({b['price']:+d}), fair price {b['fair']:+d}: EV {b['ev'] * 100:+.1f}%."
                     if b.get("fair") is not None else
                     f"Model gives {b['p_cover'] * 100:.1f}% vs {b['nv'] * 100:.1f}% priced in: EV {b['ev'] * 100:+.1f}%.")
        if ev["disagree"] is not None and ev["disagree"] >= league.warn_gap and key in ("home", "away"):
            lines.append(f"Caution: model and market are {league.warn_gap:g}+ {U} apart. Check news the model can't see.")
        if ev.get("disagree_p") is not None and ev["disagree_p"] >= 0.08:
            lines.append("Caution: model and market disagree by 8%+ on who wins. Check the starting goalies and news first.")
        reasons[key] = lines
    return {"factors": factors, "steps": steps, "reasons": reasons}


def team_form(con, league_key, season):
    games = rows(con, "SELECT home, away, home_score, away_score FROM games WHERE league=? AND season=? "
                      "AND completed=1 ORDER BY date_utc", (league_key, season))
    m = {}
    for g in games:
        d = g["home_score"] - g["away_score"]
        m.setdefault(g["home"], []).append(d)
        m.setdefault(g["away"], []).append(-d)
    return {t: {"last3": sum(v[-3:]) / len(v[-3:]), "season": sum(v) / len(v), "n": len(v)} for t, v in m.items()}


def build_board(con, league, cfg, ratings, now, days_ahead=8, season=None):
    from . import injuries as INJ
    from .leagues import season_for
    season = season or season_for(league.key, now)
    lo = (now - timedelta(hours=4)).isoformat()
    hi = (now + timedelta(days=days_ahead)).isoformat()
    games = rows(con, "SELECT * FROM games WHERE league=? AND completed=0 AND date_utc >= ? AND date_utc <= ? "
                      "ORDER BY date_utc", (league.key, lo[:16], hi[:16]))
    icfg = cfg.get("injuries") or {}
    use = INJ.usage(con, league.key, season) if icfg.get("enabled") else None
    form = team_form(con, league.key, season)
    cfg = dict(cfg, slope=market_scale(league, ratings, games, con))
    out = []
    for g in games:
        inj = INJ.game_adjustment(con, league.key, season, g, icfg, use) if use else None
        rest = back_to_back(con, league, g)
        out.append(evaluate_game(g, ratings, latest_quotes(con, g["id"]), league, cfg, inj, form, rest))
    return out


def market_scale(league, ratings, games, con):
    """How much to stretch the model's margins so they spread out like DraftKings'.

    A model built from scores knows less than the betting market, so its margins sit
    closer to even. Blended as is, every favorite would look overpriced and every
    underdog underpriced. Matching the model's spread to the market's across today's
    slate keeps the disagreements about which team is better and drops that lean."""
    from . import gamedist as GD
    pairs = []
    for g in games:
        q = latest_quotes(con, g["id"])
        neutral = bool(g["neutral"])
        if league.model == "poisson":
            mp = _market_p_home(q)
            _, tot = consensus(q)
            if mp is None:
                continue
            lh = ratings.points(g["home"], g["away"], True, neutral)
            la = ratings.points(g["away"], g["home"], False, neutral)
            mh, ma = GD.poisson_from_targets(tot or (lh + la), mp)
            pairs.append((lh - la, mh - ma))
        else:
            sp, _ = consensus(q)
            if sp is None:
                continue
            pairs.append((ratings.margin(g["home"], g["away"], neutral), -sp))
    if len(pairs) < 6:
        return 1.0
    rm = (sum(m * m for m, _ in pairs) / len(pairs)) ** 0.5
    rk = (sum(k * k for _, k in pairs) / len(pairs)) ** 0.5
    if rm <= 1e-9:
        return 1.0
    return min(max(rk / rm, 0.8), 3.0)


def back_to_back(con, league, g):
    """Rest penalty per side when a team also played the day before."""
    out = {"home": 0.0, "away": 0.0}
    if not league.rest_penalty:
        return out
    start = parse_iso(g["date_utc"])
    lo = (start - timedelta(hours=30)).isoformat()
    for side in ("home", "away"):
        t = g[side]
        prev = con.execute("SELECT COUNT(*) FROM games WHERE league=? AND id<>? AND (home=? OR away=?) "
                           "AND date_utc >= ? AND date_utc < ?",
                           (league.key, g["id"], t, t, lo, g["date_utc"])).fetchone()[0]
        if prev:
            out[side] = league.rest_penalty
    return out


# ----------------------------------------------------------------- teams

def team_table(con, league, season, ratings):
    games = rows(con, "SELECT * FROM games WHERE league=? AND season=? AND completed=1 ORDER BY date_utc",
                 (league.key, season))
    t = {}
    for name in ratings.teams:
        t[name] = {"team": name, "abbr": "", "w": 0, "l": 0, "tie": 0, "pf": 0.0, "pa": 0.0, "gp": 0,
                   "ats_w": 0, "ats_l": 0, "ats_p": 0, "ou_o": 0, "ou_u": 0, "ou_p": 0,
                   "res": [], "hw": 0, "hl": 0, "aw": 0, "al": 0,
                   "rating": ratings.rating.get(name, 0.0), "off": ratings.offense.get(name, 0.0),
                   "def": ratings.defense.get(name, 0.0)}
    for g in games:
        if g.get("season_type") not in (2, 3) or g["home"] not in t or g["away"] not in t:
            continue   # preseason, All-Star and exhibition games don't count toward records
        for side, opp in (("home", "away"), ("away", "home")):
            name = g[side]
            if name not in t:
                continue
            r = t[name]
            r["abbr"] = g[side + "_abbr"]
            pf, pa = g[side + "_score"], g[opp + "_score"]
            r["gp"] += 1
            r["pf"] += pf
            r["pa"] += pa
            r["w" if pf > pa else "l" if pf < pa else "tie"] += 1
            r["res"].append("W" if pf > pa else "L" if pf < pa else "T")
            if pf != pa:
                r[("h" if side == "home" else "a") + ("w" if pf > pa else "l")] += 1
        close = latest_quotes(con, g["id"], before=g["date_utc"])
        sp, tot = consensus(close)
        margin = g["home_score"] - g["away_score"]
        if sp is not None:
            res = margin + sp
            for side, sign in (("home", 1), ("away", -1)):
                if g[side] in t:
                    v = sign * res
                    t[g[side]]["ats_w" if v > 0 else "ats_l" if v < 0 else "ats_p"] += 1
        if tot is not None:
            tp = g["home_score"] + g["away_score"]
            for side in ("home", "away"):
                if g[side] in t:
                    t[g[side]]["ou_o" if tp > tot else "ou_u" if tp < tot else "ou_p"] += 1
    out = list(t.values())
    for r in out:
        r["ppg"] = r["pf"] / r["gp"] if r["gp"] else None
        r["papg"] = r["pa"] / r["gp"] if r["gp"] else None
        last = r.pop("res")[-5:]
        r["last5"] = "".join(last)
        r["l5w"], r["l5l"] = last.count("W"), last.count("L")
    out.sort(key=lambda r: -r["rating"])
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out


# ----------------------------------------------------------------- players

def player_data(con, league, season):
    gl = rows(con, "SELECT id, date_utc, week, home, away FROM games WHERE league=? AND season=? AND completed=1 "
                   "ORDER BY date_utc", (league.key, season))
    order = {g["id"]: i for i, g in enumerate(gl)}
    meta = {g["id"]: g for g in gl}
    if not gl:
        return {"stat_labels": STAT_LABELS, "players": []}
    ph = ",".join("?" * len(gl))
    data = rows(con, f"SELECT * FROM player_stats WHERE game_id IN ({ph}) AND value_num IS NOT NULL",
                [g["id"] for g in gl])
    featured = FEATURED_BY_LEAGUE.get(league.key, {})
    present = {}
    for r in data:
        present.setdefault(r["category"], set()).add(r["stat"])
    use = {}
    for cat, st in present.items():
        f = [k for k in featured.get(cat, []) if k in st]
        use[cat] = f or sorted(st)[:6]   # ESPN renamed something: show what's there
    players = {}
    for r in data:
        cat = r["category"]
        if r["stat"] not in use[cat]:
            continue
        p = players.setdefault(r["player_id"], {"id": r["player_id"], "name": r["player"], "team": r["team"],
                                                "pos": r["position"], "cats": {}, "_last": -1})
        i = order[r["game_id"]]
        if i > p["_last"]:
            p["_last"], p["team"] = i, r["team"]
        c = p["cats"].setdefault(cat, {})
        c.setdefault(r["stat"], {})[i] = r["value_num"]

    out = []
    for p in players.values():
        cats = {}
        for cat, stats in p["cats"].items():
            gids = sorted({i for s in stats.values() for i in s})
            keys = [k for k in use[cat] if k in stats]
            if not keys:
                continue
            cats[cat] = {
                "keys": keys,
                "games": [{"wk": meta[gl[i]["id"]]["week"],
                           "opp": gl[i]["away"] if gl[i]["home"] == p["team"] else gl[i]["home"]} for i in gids],
                "vals": {k: [stats[k].get(i, 0.0) for i in gids] for k in keys},
            }
        if cats:
            out.append({"id": p["id"], "name": p["name"], "team": p["team"], "pos": p["pos"], "cats": cats})
    out.sort(key=lambda p: p["name"])
    return {"stat_labels": STAT_LABELS, "players": out}
