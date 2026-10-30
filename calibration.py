"""Self check for win probabilities.

Replays last season and this one a week at a time: fit the ratings on games
before the week, predict the week, record what happened. Then:
  * fits the spread of outcomes the NFL/NBA win chances should use (win_sd).
    The NHL goals model already produces win chances, so it is only checked.
  * shows, by bucket, how often teams the model favored at 60-65%, 65-70% ...
    actually won.
Uses only scores, so it works before any lines have been saved.
"""
import math
from collections import defaultdict
from datetime import timedelta

from . import model as M
from .util import parse_iso


def periods(games, league, seasons, skip_days=21):
    """Groups of games to predict together: NFL weeks from week 4, otherwise
    calendar weeks starting three weeks into the season."""
    out = defaultdict(list)
    first = {}
    for g in games:
        if g["completed"] and g["season"] in seasons and g.get("season_type") != 1:
            d = parse_iso(g["date_utc"])
            first[g["season"]] = min(first.get(g["season"], d), d)
    for g in games:
        if not g["completed"] or g["season"] not in seasons or g.get("season_type") == 1:
            continue
        if league.schedule == "weekly":
            if g["season_type"] == 2 and g["week"] and g["week"] >= 4:
                out[(g["season"], g["week"])].append(g)
        else:
            d = parse_iso(g["date_utc"])
            if d - first[g["season"]] < timedelta(days=skip_days):
                continue
            monday = (d - timedelta(days=d.weekday())).date().isoformat()
            out[(g["season"], monday)].append(g)
    return out


def walk_forward(games, league, seasons, cfg=None):
    from .board import build_game
    cfg = dict(cfg or {"market_blend": 0.0})
    cfg["market_blend"] = 0.0
    preds = []
    groups = periods(games, league, seasons)
    for key in sorted(groups):
        grp = groups[key]
        start = min(parse_iso(g["date_utc"]) for g in grp)
        r = M.fit(games, league, start)
        if r.games_used < 40:
            continue
        for g in grp:
            model, _, _, _ = build_game(league, cfg, r, g, {})
            preds.append({"season": key[0], "period": key[1], "g": g, "ratings": r,
                          "pm": model.exp_margin, "pt": model.exp_total,
                          "p_model": model.p_home_win(),
                          "margin": g["home_score"] - g["away_score"],
                          "total": g["home_score"] + g["away_score"]})
    return preds


def fit_win_sd(preds, default):
    """Best sd for P(home wins) = Phi(pm / sd), by likelihood over a grid."""
    pts = [p for p in preds if p["margin"] != 0]
    if len(pts) < 120:
        return default, len(pts)
    best, best_ll = default, -1e18
    for i in range(int(default * 5), int(default * 18) + 1):
        sd = i / 10
        ll = 0.0
        for p in pts:
            q = M.win_prob(p["pm"], sd)
            q = min(max(q, 1e-6), 1 - 1e-6)
            ll += math.log(q if p["margin"] > 0 else 1 - q)
        if ll > best_ll:
            best, best_ll = sd, ll
    return best, len(pts)


def buckets(preds, pfun):
    """Favorite's model win chance vs how often the favorite won."""
    edges = [0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 1.01]
    rows = [{"lo": edges[i], "hi": min(edges[i + 1], 1.0), "n": 0, "pred": 0.0, "won": 0} for i in range(len(edges) - 1)]
    for p in preds:
        if p["margin"] == 0:
            continue
        q = pfun(p)
        fav_home = q >= 0.5
        qf = q if fav_home else 1 - q
        won = (p["margin"] > 0) == fav_home
        for r in rows:
            if r["lo"] <= qf < r["hi"]:
                r["n"] += 1
                r["pred"] += qf
                r["won"] += won
                break
    for r in rows:
        r["pred"] = r["pred"] / r["n"] if r["n"] else None
        r["actual"] = r["won"] / r["n"] if r["n"] else None
    return rows


def fit_slope(preds):
    """Ratings are pulled toward average, so projected margins come out too small.
    Fit actual margin ~ slope x projected margin (through the origin) on games the
    model hadn't seen, and scale projections by it."""
    if len(preds) < 150:
        return 1.0
    num = sum(p["pm"] * p["margin"] for p in preds)
    den = sum(p["pm"] * p["pm"] for p in preds)
    return min(max(num / den, 0.8), 1.8) if den > 0 else 1.0


def run(games, league, season, cfg=None, preds=None):
    preds = preds if preds is not None else walk_forward(games, league, {season - 1, season}, cfg)
    slope = fit_slope(preds)
    if league.model == "poisson":
        from .gamedist import PoissonGame
        n = sum(1 for p in preds if p["margin"] != 0)

        def pf(p):
            t, d = p["pt"], p["pm"] * slope
            return PoissonGame((t + d) / 2, (t - d) / 2).p_home_win()
        return {"win_sd": None, "slope": slope, "n": n, "default_sd": None, "fitted": False,
                "buckets": buckets(preds, pf)}
    scaled = [dict(p, pm=p["pm"] * slope) for p in preds]
    sd, n = fit_win_sd(scaled, league.margin_sd)
    return {"win_sd": sd, "slope": slope, "n": n, "default_sd": league.margin_sd, "fitted": n >= 120,
            "buckets": buckets(scaled, lambda p: M.win_prob(p["pm"], sd))}
