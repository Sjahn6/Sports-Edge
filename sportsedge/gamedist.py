"""What the final score of a game could look like, per sport.

NFL / NBA: margin and total are normal curves around the projection, with whole
           number outcomes (a line of 3 can push).
NHL:       each team's goals are Poisson around its projected goals. A tie after
           regulation goes to overtime/shootout, which adds one goal for the winner,
           so the final margin is never 0 and puck lines (+/-1.5) work out exactly.

Every model answers the same three questions, so pricing code does not care
which sport it is:
    margin_probs(line) -> P(home margin > line), P(== line), P(< line)
    total_probs(line)  -> P(total > line), P(== line), P(< line)
    p_home_win()
"""
import math

from . import model as M


class NormalGame:
    def __init__(self, exp_margin, exp_total, margin_sd, total_sd, win_sd=None):
        self.exp_margin, self.exp_total = exp_margin, exp_total
        self.margin_sd, self.total_sd = margin_sd, total_sd
        self.win_sd = win_sd or margin_sd

    def margin_probs(self, line):
        return M.line_probs(self.exp_margin, line, self.margin_sd)

    def total_probs(self, line):
        return M.line_probs(self.exp_total, line, self.total_sd)

    def p_home_win(self):
        return M.win_prob(self.exp_margin, self.win_sd)


def _pois(lam, kmax=16):
    p = [math.exp(-lam)]
    for k in range(1, kmax + 1):
        p.append(p[-1] * lam / k)
    s = sum(p)
    return [x / s for x in p]


class PoissonGame:
    def __init__(self, lam_home, lam_away, home_ot=0.52):
        self.lam_home, self.lam_away = max(lam_home, 0.2), max(lam_away, 0.2)
        ph, pa = _pois(self.lam_home), _pois(self.lam_away)
        margin, total = {}, {}
        for h, x in enumerate(ph):
            for a, y in enumerate(pa):
                p = x * y
                if h == a:   # overtime / shootout: winner gets one more goal
                    for m, t, q in ((1, h + a + 1, home_ot), (-1, h + a + 1, 1 - home_ot)):
                        margin[m] = margin.get(m, 0.0) + p * q
                        total[t] = total.get(t, 0.0) + p * q
                else:
                    margin[h - a] = margin.get(h - a, 0.0) + p
                    total[h + a] = total.get(h + a, 0.0) + p
        self._margin, self._total = margin, total
        self.exp_margin = sum(m * p for m, p in margin.items())
        self.exp_total = sum(t * p for t, p in total.items())
        self.p_reg_tie = sum(x * y for x, y in zip(ph, pa))

    @staticmethod
    def _split(dist, line):
        above = sum(p for v, p in dist.items() if v > line + 1e-9)
        eq = sum(p for v, p in dist.items() if abs(v - line) < 1e-9)
        return above, eq, max(0.0, 1.0 - above - eq)

    def margin_probs(self, line):
        return self._split(self._margin, line)

    def total_probs(self, line):
        return self._split(self._total, line)

    def p_home_win(self):
        return self.margin_probs(0)[0]


def poisson_from_targets(total, p_home, home_ot=0.52):
    """Goal rates that produce this expected total and home win chance."""
    lo, hi = -3.0, 3.0
    for _ in range(40):
        d = (lo + hi) / 2
        g = PoissonGame((total + d) / 2, (total - d) / 2, home_ot)
        if g.p_home_win() < p_home:
            lo = d
        else:
            hi = d
    d = (lo + hi) / 2
    return (total + d) / 2, (total - d) / 2


HOCKEY_SPREADS = [-3.5, -2.5, -1.5, -0.5, 1.5, 2.5, 3.5]     # -0.5 = the moneyline
HOCKEY_TOTALS = [x + 0.5 for x in range(2, 10)]


def alt_line(game, price, kind, side):
    """Most aggressive half point line still +EV at `price`.

    kind "spread": side "home"/"away"; returns the points that side gets
        (negative = laying). The bet wins when that side's margin + x > 0.
    kind "total":  side "over" returns the highest total still +EV,
                   side "under" the lowest."""
    best = None
    if isinstance(game, PoissonGame):
        rng = HOCKEY_SPREADS if kind == "spread" else HOCKEY_TOTALS
    else:
        rng = [i / 2 for i in range(-80, 81)] if kind == "spread" else [i / 2 for i in range(1, 600)]
    for v in rng:
        if kind == "spread":
            if side == "home":
                above, eq, below = game.margin_probs(-v)
                p_win, push = above, eq
            else:
                above, eq, below = game.margin_probs(v)
                p_win, push = below, eq
            if M.expected_value(p_win, push, price) >= 0 and (best is None or v < best):
                best = v
        else:
            above, eq, below = game.total_probs(v)
            p_win = above if side == "over" else below
            if M.expected_value(p_win, eq, price) >= 0:
                if best is None or (v > best if side == "over" else v < best):
                    best = v
    return best
