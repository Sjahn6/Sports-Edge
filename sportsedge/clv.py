"""Closing line tracking: did the numbers we flagged beat where DraftKings closed?

Every update saves each flagged bet the first time it shows up (the number you
could have bet). Once the game starts, the last line before kickoff is the
closing line. Beating it consistently is the quickest honest sign of a real
edge: it shows up in a few weeks, where wins and losses take seasons.

clv = how much better the flagged number was than the closing number, as a share
of the bet: the closing price's implied chance (moved for any change in points),
times the payout at the price we flagged, minus 1. Same number as the close = 0,
a better number is positive. Betting into the close with a steady +2 to 3% is
what profitable bettors look like.

The same numbers decide which bet types the site will call a Pick:
  - totals start benched (they lost in every sport in the two season replay)
    and come back once they beat the close over enough bets
  - any other bet type gets benched if it keeps losing to the close
"""
import json
from statistics import NormalDist

from . import board as B
from . import db as D
from . import model as M

MIN_N = 40          # tracked bets before a bet type is judged
PROMOTE = 0.01      # benched type comes back when average clv is above +1%
BENCH = -0.005      # active type is benched when average clv falls below -0.5%
START_BENCHED = {"total"}
_N = NormalDist()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracked (
    league TEXT NOT NULL, game_id TEXT NOT NULL, market TEXT NOT NULL, side TEXT NOT NULL,
    label TEXT, game TEXT, kickoff TEXT, book TEXT,
    first_utc TEXT, first_point REAL, first_price INTEGER, first_p REAL, first_ev REAL, tier TEXT,
    last_utc TEXT, last_point REAL, last_price INTEGER,
    close_point REAL, close_price INTEGER, close_fair REAL, clv REAL, moved REAL,
    result TEXT, profit REAL,
    PRIMARY KEY (league, game_id, market, side)
);
"""

_MK = {"spread": "spreads", "total": "totals", "moneyline": "h2h"}
_OTHER = {"home": "away", "away": "home", "over": "under", "under": "over"}


def ensure(con):
    con.executescript(SCHEMA)


def _label(g, pl):
    if pl["side"] in ("home", "away"):
        team = B.short(g, pl["side"])
        if pl["market"] == "moneyline":
            return f"{team} to win"
        return f'{team} {pl["point"]:+g}' if pl["point"] else f"{team} PK"
    return f'{pl["side"].title()} {pl["point"]:g}'


def record(con, league, board, now_iso):
    """Save every flagged bet on the board: first sighting kept, latest line updated."""
    ensure(con)
    for g in board:
        if g["kickoff"] <= now_iso:
            continue
        gd = {"home": g["home"], "away": g["away"], "home_abbr": g["home_abbr"], "away_abbr": g["away_abbr"],
              "home_short": g.get("home_short"), "away_short": g.get("away_short")}
        for pl in g["plays"] + g.get("benched_plays", []):
            tier = "Lean" if pl.get("benched") else "Pick"
            con.execute(
                "INSERT INTO tracked (league, game_id, market, side, label, game, kickoff, book, first_utc, "
                "first_point, first_price, first_p, first_ev, tier, last_utc, last_point, last_price) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(league, game_id, market, side) DO UPDATE SET "
                "last_utc=excluded.last_utc, last_point=excluded.last_point, last_price=excluded.last_price",
                (league.key, g["id"], pl["market"], pl["side"], _label(gd, pl),
                 f'{g["away_short"]} @ {g["home_short"]}', g["kickoff"], pl["book"], now_iso,
                 pl["point"], pl["price"], pl["p_cover"], pl["ev"], tier, now_iso, pl["point"], pl["price"]))
    con.commit()


def _gained(market, side, ours, close):
    """Points better than the closing number (positive = we got the better line)."""
    if ours is None or close is None:
        return 0.0
    if market == "spread":
        return ours - close            # +3.5 when it closed +2.5 = 1 point better
    return (close - ours) if side == "over" else (ours - close)


def closing_value(league, row, quotes):
    """(fair chance from the closing market, clv, points gained, close point, close price)."""
    mk = _MK[row["market"]]
    mine = quotes.get((row["book"], mk, row["side"]))
    other = quotes.get((row["book"], mk, _OTHER[row["side"]]))
    if not mine or not other:   # fall back to any book that has both sides
        for (b, m, s), q in quotes.items():
            if m == mk and s == row["side"] and (b, m, _OTHER[s]) in quotes:
                mine, other = q, quotes[(b, m, _OTHER[s])]
                break
    if not mine or not other:
        return None
    p0 = M.implied(mine["price"])
    gained = 0.0
    fair = p0
    if row["market"] != "moneyline":
        sd = league.margin_sd if row["market"] == "spread" else league.total_sd
        gained = _gained(row["market"], row["side"], row["first_point"], mine["point"])
        p0 = min(max(p0, 0.01), 0.99)
        fair = _N.cdf(_N.inv_cdf(p0) + gained / sd)
    clv = fair * M.american_to_decimal(row["first_price"]) - 1
    return fair, clv, gained, mine["point"], mine["price"]


def grade(con, league, now_iso):
    """Fill closing values for games that have started, and results once they're final."""
    ensure(con)
    todo = B.rows(con, "SELECT t.*, g.completed, g.home_score, g.away_score, g.date_utc FROM tracked t "
                       "JOIN games g ON g.id=t.game_id WHERE t.league=? AND g.date_utc<=? "
                       "AND (t.clv IS NULL OR t.result IS NULL)", (league.key, now_iso))
    for r in todo:
        if r["clv"] is None:
            cv = closing_value(league, r, B.latest_quotes(con, r["game_id"], before=r["date_utc"]))
            if cv:
                fair, clv, gained, cp, cpr = cv
                con.execute("UPDATE tracked SET close_fair=?, clv=?, moved=?, close_point=?, close_price=? "
                            "WHERE league=? AND game_id=? AND market=? AND side=?",
                            (fair, clv, gained, cp, cpr, r["league"], r["game_id"], r["market"], r["side"]))
        if r["result"] is None and r["completed"] and r["home_score"] is not None:
            margin, total = r["home_score"] - r["away_score"], r["home_score"] + r["away_score"]
            pl = {"market": r["market"], "side": r["side"], "point": r["first_point"], "price": r["first_price"]}
            from .backtest import _grade
            res, profit = _grade(pl, margin, total)
            con.execute("UPDATE tracked SET result=?, profit=? WHERE league=? AND game_id=? AND market=? AND side=?",
                        (res, profit, r["league"], r["game_id"], r["market"], r["side"]))
    con.commit()


def status(con, league):
    """Per bet type: how the flagged numbers did against the close, and whether it's
    active (Pick), benched (Lean only), or still testing."""
    ensure(con)
    out = {}
    for m in ("moneyline", "spread", "total"):
        rs = B.rows(con, "SELECT clv, result, profit FROM tracked WHERE league=? AND market=? AND clv IS NOT NULL",
                    (league.key, m))
        n = len(rs)
        avg = (sum(r["clv"] for r in rs) / n) if n else None
        beat = sum(r["clv"] > 0 for r in rs)
        done = [r for r in rs if r["result"] in ("W", "L", "P")]
        start = "benched" if m in START_BENCHED else "active"
        if n < MIN_N:
            state = start
        elif start == "benched":
            state = "active" if avg > PROMOTE else "benched"
        else:
            state = "benched" if avg < BENCH else "active"
        out[m] = {"n": n, "beat": beat, "avg": avg, "state": state, "proven": n >= MIN_N,
                  "w": sum(r["result"] == "W" for r in done), "l": sum(r["result"] == "L" for r in done),
                  "units": sum(r["profit"] or 0 for r in done)}
    prev = D.get_meta(con, f"{league.key}:bench_state")
    cur = json.dumps({m: v["state"] for m, v in out.items()})
    if prev != cur:
        D.set_meta(con, f"{league.key}:bench_state", cur)
    return out


def recent(con, league, limit=60):
    ensure(con)
    return B.rows(con, "SELECT label, game, kickoff, market, side, tier, first_point, first_price, last_point, "
                       "last_price, close_point, close_price, clv, moved, result, profit FROM tracked "
                       "WHERE league=? ORDER BY kickoff DESC LIMIT ?", (league.key, limit))


def bench(board, st):
    """Move flagged bets in benched bet types out of Picks (they're still tracked)."""
    off = {m for m, v in st.items() if v["state"] == "benched"}
    for g in board:
        keep, moved = [], []
        for pl in g["plays"]:
            (moved if pl["market"] in off else keep).append(dict(pl, benched=pl["market"] in off))
        g["plays"], g["benched_plays"] = keep, moved
    return sorted(off)
