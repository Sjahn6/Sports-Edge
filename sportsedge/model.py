"""Power ratings and probabilities.

Margin model:  home_margin = HFA + rating[home] - rating[away] + noise
Points model:  points[t vs o] = mu + offense[t] + defense[o] + home_bump + noise
                 (defense > 0 means that team gives up more points)

Both are fit by weighted ridge regression: recent games count more (exponential
fade with a half life), and every team is pulled toward league average, which
keeps early season ratings sane. Last season's games carry over with a small
weight, which acts as the preseason prior.
"""
import math
from dataclasses import dataclass, field

import numpy as np

from .util import parse_iso


@dataclass
class Ratings:
    teams: list
    rating: dict
    hfa: float
    mu: float
    offense: dict
    defense: dict
    home_pts: float
    games_used: int
    weight_used: float
    extra: dict = field(default_factory=dict)

    def margin(self, home, away, neutral=False):
        return (0.0 if neutral else self.hfa) + self.rating.get(home, 0.0) - self.rating.get(away, 0.0)

    def points(self, team, opp, is_home, neutral=False):
        bump = 0.0 if neutral else (self.home_pts / 2 if is_home else -self.home_pts / 2)
        return self.mu + self.offense.get(team, 0.0) + self.defense.get(opp, 0.0) + bump

    def total(self, home, away, neutral=False):
        return self.points(home, away, True, neutral) + self.points(away, home, False, neutral)


def _weights(games, as_of, half_life):
    w = []
    for g in games:
        age = (as_of - parse_iso(g["date_utc"])).total_seconds() / 86400.0
        w.append(0.5 ** (max(age, 0.0) / half_life))
    return np.array(w)


def fit(games: list, league, as_of) -> Ratings:
    """games: completed game dicts dated before as_of."""
    games = [g for g in games if g["completed"] and g["home_score"] is not None
             and g.get("season_type") != 1 and parse_iso(g["date_utc"]) < as_of]   # no preseason
    # drop exhibition opponents (Pro Bowl AFC/NFC, All-Star teams, overseas clubs): a real
    # team plays at least 5 regular season games in the data
    reg = {}
    for g in games:
        if g.get("season_type") == 2:
            reg[g["home"]] = reg.get(g["home"], 0) + 1
            reg[g["away"]] = reg.get(g["away"], 0) + 1
    games = [g for g in games if reg.get(g["home"], 0) >= 5 and reg.get(g["away"], 0) >= 5]
    teams = sorted({g["home"] for g in games} | {g["away"] for g in games})
    idx = {t: i for i, t in enumerate(teams)}
    n, T = len(games), len(teams)
    if n == 0:
        return Ratings(teams, {}, league.hfa_prior, 0.0, {}, {}, league.hfa_prior, 0, 0.0)

    w = _weights(games, as_of, league.half_life_days)
    lam = league.ridge_lambda
    lam_hfa = 25.0  # HFA is noisy in small samples; lean on the prior

    # ---- margin model: [team ratings..., hfa]
    X = np.zeros((n, T + 1))
    y = np.zeros(n)
    for k, g in enumerate(games):
        X[k, idx[g["home"]]] = 1.0
        X[k, idx[g["away"]]] = -1.0
        X[k, T] = 0.0 if g["neutral"] else 1.0
        y[k] = g["home_score"] - g["away_score"]
    P = np.diag([lam] * T + [lam_hfa])
    prior = np.zeros(T + 1)
    prior[T] = league.hfa_prior
    A = X.T @ (X * w[:, None]) + P
    b = X.T @ (w * y) + P @ prior
    beta = np.linalg.solve(A, b)
    rating = {t: float(beta[idx[t]]) for t in teams}
    hfa = float(beta[T])
    # center ratings so average team = 0
    avg = sum(rating.values()) / T
    rating = {t: r - avg for t, r in rating.items()}

    # ---- points model: [mu, offense..., defense..., home_bump]
    m = 2 * n
    Xp = np.zeros((m, 2 + 2 * T))
    yp = np.zeros(m)
    wp = np.repeat(w, 2)
    for k, g in enumerate(games):
        for j, (t, o, pts, home) in enumerate(((g["home"], g["away"], g["home_score"], 1),
                                                (g["away"], g["home"], g["away_score"], 0))):
            r = 2 * k + j
            Xp[r, 0] = 1.0
            Xp[r, 1 + idx[t]] = 1.0
            Xp[r, 1 + T + idx[o]] = 1.0
            Xp[r, 1 + 2 * T] = 0.0 if g["neutral"] else (0.5 if home else -0.5)
            yp[r] = pts
    Pp = np.diag([1e-6] + [lam] * (2 * T) + [lam_hfa])
    priorp = np.zeros(2 + 2 * T)
    priorp[1 + 2 * T] = league.hfa_prior
    Ap = Xp.T @ (Xp * wp[:, None]) + Pp
    bp = Xp.T @ (wp * yp) + Pp @ priorp
    bet = np.linalg.solve(Ap, bp)
    mu = float(bet[0])
    off = {t: float(bet[1 + idx[t]]) for t in teams}
    dfn = {t: float(bet[1 + T + idx[t]]) for t in teams}
    # keep mu meaningful: shift averages into mu
    oa, da = sum(off.values()) / T, sum(dfn.values()) / T
    off = {t: v - oa for t, v in off.items()}
    dfn = {t: v - da for t, v in dfn.items()}
    mu += oa + da

    return Ratings(teams, rating, hfa, mu, off, dfn, float(bet[1 + 2 * T]), n, float(w.sum()))


# ------------------------------------------------------------- probabilities

def _phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def line_probs(expected, line, sd, integer=True):
    """Probabilities that an outcome X (normal with mean=expected) lands above,
    on, or below `line`. Integer outcomes (points) use a half point continuity
    correction so whole number lines get a real push chance.

    Spread use: X = home margin, line = -home_spread  -> above = home covers.
    Total use:  X = total points, line = total        -> above = over.
    """
    if integer:
        if float(line).is_integer():
            above = 1.0 - _phi((line + 0.5 - expected) / sd)
            below = _phi((line - 0.5 - expected) / sd)
            push = max(0.0, 1.0 - above - below)
        else:
            above = 1.0 - _phi((line - expected) / sd)
            below = 1.0 - above
            push = 0.0
    else:
        above = 1.0 - _phi((line - expected) / sd)
        below, push = 1.0 - above, 0.0
    return above, push, below


def win_prob(exp_margin, sd):
    """Chance the team with this expected margin wins outright. Games the normal
    curve puts at exactly 0 are overtime games, split evenly."""
    above, push, below = line_probs(exp_margin, 0, sd)
    return above + push / 2


def alt_line_for_price(exp, sd, price, mode):
    """Most aggressive half point line that is still +EV at `price`.

    mode "spread": exp = team's expected margin. Returns x, the points the team
        gets (negative = laying points); the bet wins when margin + x > 0.
        Smaller x is more aggressive.
    mode "over":  exp = expected total. Returns the highest total still +EV.
    mode "under": exp = expected total. Returns the lowest total still +EV.
    None if no line in range works."""
    best = None
    for i in range(-160, 161):
        v = i / 2
        if mode == "spread":
            above, push, _ = line_probs(exp, -v, sd)
            ok = expected_value(above, push, price) >= 0
            if ok and (best is None or v < best):
                best = v
        else:
            if v <= 0:
                continue
            above, push, below = line_probs(exp, v, sd)
            p_win = above if mode == "over" else below
            if expected_value(p_win, push, price) >= 0:
                if best is None or (v > best if mode == "over" else v < best):
                    best = v
    return best


def american_to_decimal(price: int) -> float:
    return 1.0 + (price / 100.0 if price > 0 else 100.0 / abs(price))


def implied(price: int) -> float:
    return 1.0 / american_to_decimal(price)


def no_vig(price_a: int, price_b: int):
    a, b = implied(price_a), implied(price_b)
    return a / (a + b), b / (a + b)


def expected_value(p_win, p_push, price):
    """Profit per $1 risked."""
    p_loss = 1.0 - p_win - p_push
    return p_win * (american_to_decimal(price) - 1.0) - p_loss


def kelly(p_win, p_push, price, fraction=0.25):
    b = american_to_decimal(price) - 1.0
    p_loss = 1.0 - p_win - p_push
    f = (b * p_win - p_loss) / b
    return max(0.0, f * fraction)


def to_american(p):
    if p <= 0 or p >= 1:
        return None
    return int(round(-100 * p / (1 - p))) if p >= 0.5 else int(round(100 * (1 - p) / p))
