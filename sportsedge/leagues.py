"""Per league settings."""
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class League:
    key: str
    name: str
    espn_path: str          # site.api.espn.com/apis/site/v2/sports/<espn_path>
    odds_key: str           # The Odds API sport key (optional source)
    schedule: str           # "weekly" (NFL) or "daily" (NBA, NHL)
    model: str              # "normal" (points) or "poisson" (goals)
    unit: str               # "pts" or "goals"
    margin_sd: float        # spread of final margins around the expected margin (normal model)
    total_sd: float         # spread of final totals around the expected total (normal model)
    hfa_prior: float        # home edge prior, in points or goals
    half_life_days: float   # how fast old games fade from the ratings
    ridge_lambda: float     # pull of each team rating toward average (games worth)
    rest_penalty: float     # cost of playing the second night of a back to back
    key_numbers: tuple      # margins games land on most often (NFL)
    warn_gap: float         # model vs market gap that triggers "check news"
    season_start: tuple     # (month, day) a season's schedule starts, for daily pulls
    season_end: tuple       # (month, day) the last playoff games
    enabled: bool = True


LEAGUES = {
    "nfl": League(
        key="nfl", name="NFL", espn_path="football/nfl", odds_key="americanfootball_nfl",
        schedule="weekly", model="normal", unit="pts",
        margin_sd=13.5, total_sd=10.0, hfa_prior=1.6, half_life_days=120.0, ridge_lambda=3.0,
        rest_penalty=0.0, key_numbers=(3, 7, 10, 14), warn_gap=4.0,
        season_start=(9, 1), season_end=(2, 20),
    ),
    "nba": League(
        key="nba", name="NBA", espn_path="basketball/nba", odds_key="basketball_nba",
        schedule="daily", model="normal", unit="pts",
        margin_sd=12.5, total_sd=18.5, hfa_prior=2.0, half_life_days=90.0, ridge_lambda=4.0,
        rest_penalty=1.2, key_numbers=(), warn_gap=5.0,
        season_start=(10, 1), season_end=(6, 30),
    ),
    "nhl": League(
        key="nhl", name="NHL", espn_path="hockey/nhl", odds_key="icehockey_nhl",
        schedule="daily", model="poisson", unit="goals",
        margin_sd=2.3, total_sd=2.3, hfa_prior=0.15, half_life_days=150.0, ridge_lambda=12.0,
        rest_penalty=0.12, key_numbers=(), warn_gap=0.6,
        season_start=(10, 1), season_end=(6, 30),
    ),
}


def season_for(league_key: str, when) -> int:
    """ESPN labels the NFL season by the year it starts, NBA and NHL by the year it ends."""
    if league_key == "nfl":
        return when.year if when.month >= 3 else when.year - 1
    return when.year + 1 if when.month >= 8 else when.year


def season_window(league: League, season: int):
    """(start, end) dates of a daily league's season, playoffs included."""
    sm, sd = league.season_start
    em, ed = league.season_end
    start = datetime(season - 1, sm, sd, tzinfo=timezone.utc)
    end = datetime(season, em, ed, tzinfo=timezone.utc)
    return start, end
