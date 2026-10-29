"""SQLite storage. One file (sportsedge.db) holds every game, stat line and line snapshot."""
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id TEXT PRIMARY KEY,
    league TEXT NOT NULL,
    season INTEGER,
    season_type INTEGER,
    week INTEGER,
    date_utc TEXT NOT NULL,
    home_id TEXT, home TEXT NOT NULL, home_abbr TEXT,
    away_id TEXT, away TEXT NOT NULL, away_abbr TEXT,
    home_score REAL, away_score REAL,
    completed INTEGER NOT NULL DEFAULT 0,
    neutral INTEGER NOT NULL DEFAULT 0,
    players_loaded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_games_league_date ON games(league, date_utc);

CREATE TABLE IF NOT EXISTS team_game_stats (
    game_id TEXT NOT NULL, team TEXT NOT NULL, stat TEXT NOT NULL,
    value_num REAL, value_text TEXT,
    PRIMARY KEY (game_id, team, stat)
);

CREATE TABLE IF NOT EXISTS player_stats (
    game_id TEXT NOT NULL, league TEXT NOT NULL,
    player_id TEXT NOT NULL, player TEXT, team TEXT, position TEXT,
    category TEXT NOT NULL, stat TEXT NOT NULL,
    value_num REAL, value_text TEXT,
    PRIMARY KEY (game_id, player_id, category, stat)
);
CREATE INDEX IF NOT EXISTS ix_player ON player_stats(league, player_id);

CREATE TABLE IF NOT EXISTS odds (
    league TEXT NOT NULL, event_id TEXT NOT NULL, game_id TEXT,
    commence_utc TEXT, home TEXT, away TEXT,
    book TEXT NOT NULL, book_title TEXT, market TEXT NOT NULL, side TEXT NOT NULL,
    point REAL, price INTEGER NOT NULL, fetched_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_odds_game ON odds(game_id, market, book, side, fetched_utc);

CREATE TABLE IF NOT EXISTS injuries (
    league TEXT NOT NULL, team TEXT NOT NULL, player_id TEXT, player TEXT, position TEXT,
    status TEXT, detail TEXT, comment TEXT, date TEXT, fetched_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_inj ON injuries(league, team);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(games)")}
    for c in ("home_short", "away_short"):   # added after the first version
        if c not in cols:
            con.execute(f"ALTER TABLE games ADD COLUMN {c} TEXT")
    return con


def get_meta(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(con, key, value):
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))


def upsert_game(con, g: dict):
    con.execute(
        """INSERT INTO games (id, league, season, season_type, week, date_utc,
               home_id, home, home_abbr, home_short, away_id, away, away_abbr, away_short,
               home_score, away_score, completed, neutral)
           VALUES (:id, :league, :season, :season_type, :week, :date_utc,
               :home_id, :home, :home_abbr, :home_short, :away_id, :away, :away_abbr, :away_short,
               :home_score, :away_score, :completed, :neutral)
           ON CONFLICT(id) DO UPDATE SET
               season=excluded.season, season_type=excluded.season_type,
               week=excluded.week, date_utc=excluded.date_utc,
               home_id=excluded.home_id, home=excluded.home, home_abbr=excluded.home_abbr,
               away_id=excluded.away_id, away=excluded.away, away_abbr=excluded.away_abbr,
               home_short=excluded.home_short, away_short=excluded.away_short,
               home_score=excluded.home_score, away_score=excluded.away_score,
               completed=excluded.completed, neutral=excluded.neutral""",
        g,
    )


def save_box(con, game_id, league, team_rows, player_rows):
    con.execute("DELETE FROM team_game_stats WHERE game_id=?", (game_id,))
    con.execute("DELETE FROM player_stats WHERE game_id=?", (game_id,))
    con.executemany(
        "INSERT OR REPLACE INTO team_game_stats VALUES (?,?,?,?,?)",
        [(game_id, r["team"], r["stat"], r["value_num"], r["value_text"]) for r in team_rows],
    )
    con.executemany(
        "INSERT OR REPLACE INTO player_stats VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(game_id, league, r["player_id"], r["player"], r["team"], r["position"],
          r["category"], r["stat"], r["value_num"], r["value_text"]) for r in player_rows],
    )
    con.execute("UPDATE games SET players_loaded=1 WHERE id=?", (game_id,))


def replace_injuries(con, league, rows):
    """The injury report is a current snapshot, so each pull replaces the last."""
    con.execute("DELETE FROM injuries WHERE league=?", (league,))
    con.executemany(
        """INSERT INTO injuries (league, team, player_id, player, position, status, detail, comment, date, fetched_utc)
           VALUES (:league, :team, :player_id, :player, :position, :status, :detail, :comment, :date, :fetched_utc)""",
        rows,
    )


def save_odds(con, rows):
    con.executemany(
        """INSERT INTO odds (league, event_id, game_id, commence_utc, home, away,
               book, book_title, market, side, point, price, fetched_utc)
           VALUES (:league, :event_id, :game_id, :commence_utc, :home, :away,
               :book, :book_title, :market, :side, :point, :price, :fetched_utc)""",
        rows,
    )
