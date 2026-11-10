"""One recommended bet per game, at any price.

For each market (winner, spread, total) the model's call is the side it thinks
is more likely to hit. The recommendation is the call with the best track
record for that confidence level over the last two seasons (from the walk
forward replay), so a 75% winner call (right ~80% of the time) beats a 54%
spread call (right ~50%). Injuries, rest, recent form and weather are already
in the projection behind these chances.
"""
MARKETS = (("moneyline", ("home_ml", "away_ml"), ("home", "away")),
           ("spread", ("home", "away"), ("home", "away")),
           ("total", ("over", "under"), ("over", "under")))


def _bucket(c, p):
    for b in (c or {}).get("buckets", []):
        if b["lo"] <= p < b["hi"] or (b["hi"] >= 1.0 and p >= b["lo"]):
            return b
    return None


def rate_for(calls, market, p, pooled=None):
    """How often calls this sure were right: this sport's group if it has 25+ games,
    else the same group across all three sports, else this sport's overall rate."""
    c = (calls or {}).get(market)
    if not c or not c.get("n"):
        return None, 0, False
    b = _bucket(c, p)
    if b and b["n"] >= 25:
        return b["right"] / b["n"], b["n"], True
    pb = _bucket((pooled or {}).get(market), p)
    if pb and pb["n"] >= 25:
        return pb["right"] / pb["n"], pb["n"], True
    return c["right"] / c["n"], c["n"], False


def pool(calls_by_league):
    out = {}
    for calls in calls_by_league:
        for m, c in (calls or {}).items():
            o = out.setdefault(m, {"n": 0, "right": 0, "buckets": [dict(b, n=0, right=0) for b in c["buckets"]]})
            o["n"] += c["n"]
            o["right"] += c["right"]
            for ob, b in zip(o["buckets"], c["buckets"]):
                ob["n"] += b["n"]
                ob["right"] += b["right"]
    return out


def conf_word(rate):
    if rate is None:
        return ""
    if rate >= 0.72:
        return "Strong"
    if rate >= 0.62:
        return "Confident"
    if rate >= 0.555:
        return "Lean"
    return "Coin flip"


TOP_N = 5
TOP_SLACK = 0.02   # a pick qualifies if calls this sure were right within 2 points of what its price needs, or better


def top_rank(cands, n=TOP_N):
    """The day's n most confident picks, at any price, as long as the track record for
    calls that sure keeps up with the price. A -400 favorite needs 80% to break even, so it
    only makes it if calls that sure were right about 78%+ of the time. Coin flips are out.
    (Replay: -0.8% a bet, 71% hit, vs -1.1% with a fixed -250 cutoff and -4.3% with no rules.)"""
    from .model import implied
    ok = [c for c in cands if c["rec"] and c["rec"]["conf"] != "Coin flip" and c["rec"]["rate"] is not None
          and c["rec"]["rate"] - implied(c["rec"]["price"]) >= -TOP_SLACK]
    ok.sort(key=lambda c: (c["rec"]["rate"], c["rec"]["p_cover"]), reverse=True)
    return ok[:n]


def recommend(g, calls, league_key, pooled=None):
    best = None
    for market, keys, sides in MARKETS:
        if market == "spread" and league_key == "nhl":
            continue   # puck lines are almost always +/-1.5 at long prices
        a, b = g["best"].get(keys[0]), g["best"].get(keys[1])
        if not a or not b:
            continue
        i = 0 if a["p_cover"] >= b["p_cover"] else 1
        x = (a, b)[i]
        rate, n, grouped = rate_for(calls, market, x["p_cover"], pooled if market == "moneyline" else None)
        cand = {"market": market, "side": sides[i], "key": keys[i], "point": x["point"], "price": x["price"],
                "book": x["book"], "title": x["title"], "p_cover": x["p_cover"], "ev": x["ev"],
                "rate": rate, "n": n, "grouped": grouped, "conf": conf_word(rate)}
        score = (rate if rate is not None else x["p_cover"], market == "moneyline")
        if best is None or score > best[0]:
            best = (score, cand)
    return best[1] if best else None
