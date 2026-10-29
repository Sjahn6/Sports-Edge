# SportsEdge (NFL, NBA, NHL)

Tracks every NFL, NBA and NHL game, team and player box score and injury report,
saves DraftKings' lines, and shows where its numbers disagree with the book.
Switch leagues with the buttons at the top; the number on each button is how many
+EV favorites are in your -200 range.

To put it online for your phone (and friends), see SETUP_GITHUB.md.

## First run (Mac)

1. Double click `SportsEdge.command`. If macOS blocks it, right click it, choose Open, then Open again.
2. The first run sets up and loads last season plus this season for all three leagues (about 10 minutes). After that it takes under a minute.
3. Your browser opens `report.html`.

Everything is free. No accounts, no keys, no credits.

Run `./.venv/bin/python run.py doctor` from Terminal in this folder any time to check the ESPN feeds (schedule, lines, box scores, injuries).

## What each tab shows

* **-200 plays** (opens first): moneyline favorites priced -160 to -260 where the model's win chance beats what the price requires (66.7% at -200), with the cushion, EV and a Why for each. Below that, the alternate spread and total the model would still bet at -200 for every game, to check against your book's alternate line menu. Then a check of whether teams the model gave 65 to 75% actually won that often in past games.
* **Best bets**: every side where the expected return at DraftKings clears your minimum (2% by default). Model win %, the book's price with the juice removed, fair price, how many points the model disagrees by, EV, and a quarter Kelly stake as a percent of bankroll. A "check news" tag means a 4+ point disagreement, which is usually injury or quarterback news.
* **Games**: model line vs market line for every game in the next 8 days, cover chances at the best number, every book's prices, and a **Why** section: how the line was built step by step, projected score, injuries, and plain English reasons for each pick. Best bets has a Why button on every row too.
* **Injuries**: ESPN's injury report with each player's projected point impact on this week's line and how it was figured.
* **Teams**: power rating, offense, defense, record, scoring, and ATS and over/under records.
* **Players**: passing, rushing, receiving, defense and kicking. Season average, last 3 games, and a trend line.
* **Model check**: a week by week test where the model only sees games before each week. Compares its misses to the market's and grades flagged bets once lines have been saved before kickoff.

## Commands

```
python run.py            update, rebuild, open (what the .command file does)
python run.py update     scores, box scores and lines
python run.py lines      lines only (fast, free)
python run.py report     rebuild the dashboard from saved data
python run.py backtest   model check in the terminal
python run.py doctor     test every ESPN feed for each league
python run.py --league nhl    any command for one league only
```

Flags: `--no-odds`, `--no-players`, `--no-open`, `--full` (refetch everything).

## Lines

Sportsbook lines come free from ESPN's schedule feed, which shows DraftKings' spread, total and moneyline for each game. They're saved on every update, so the last snapshot before kickoff becomes the closing line used for ATS records and bet grading. Update at least once on game day so each game gets a line saved before it starts.

Alternate lines aren't in ESPN's feed. The -200 plays tab shows the line the model would still take at -200 so you can check DraftKings' alternate menu yourself.

Optional: set `source = espn,oddsapi` and add a free key from https://the-odds-api.com to also compare every US book (each pull uses 3 of 500 monthly credits).

To update automatically, open Terminal and run `crontab -e`, then add (edit the path):

```
0 9,17 * * * cd "$HOME/Documents/SportsEdge" && ./.venv/bin/python run.py --no-open >> run.log 2>&1
```

## How the model works

All three leagues:

* Each team gets a power rating from a weighted ridge regression on final margins: recent games count more (120 day half life), every team is pulled toward average, last season acts as the preseason starting point, and home field is estimated (starting near 1.6 points).
* A separate offense and defense fit predicts each team's points, which drives totals.
* Injuries: every player on ESPN's report gets a point value. Starting QB is 3.5 pts scaled by his yards per attempt (about 1.75 to 5.6). Skill players are worth their share of team scrimmage yards, defenders their share of tackles and sacks, linemen a small flat amount. That is multiplied by the chance he sits (Out 100%, Doubtful 85%, Questionable 25%) and by how much he has played in the last 3 games, so long term injuries already reflected in the ratings don't count twice. Offense losses lower that team's points; defense losses raise the opponent's. Values are in config.ini under [injuries].
* Moneyline win chances come from the same projected margin. How spread out results are around it is tuned from a replay of every week of last season and this one (each week predicted only from earlier games), so a 70% call should win about 70% of the time.
* The expected margin and total blend 70% model with 30% market consensus (`market_blend` in config.ini). Set it to 0 to see the raw model.
* Results are treated as normal around that expectation (13.5 points for margins, 10 for totals), with whole number lines given a real push chance.
* EV uses each book's actual price, the no vig number removes the juice from both sides of that book's market, and the best book is whichever has the highest EV for that side.

NBA:
* Same rating approach in points, with a 90 day fade and home court starting near 2 points.
* Back to backs cost the tired team 1.2 points.
* Injuries: each player's share of team production (points + 1.2 rebounds + 1.5 assists) above a bench player's share, x 22. A star with a quarter of his team's production is worth about 4 points.

NHL:
* Ratings are in goals with a 150 day fade (hockey is noisy, so it needs a longer memory).
* Instead of a normal curve, each team's goals are projected and every final score is counted, with ties going to overtime. That prices moneylines, the ±1.5 puck line and totals exactly, and alternate lines only show real hockey numbers.
* The market blend works on the moneyline and total, since the puck line is always ±1.5.
* A starting goalie on the injury report costs 0.25 goals. Skaters are valued by their share of team points. Back to backs cost 0.12 goals.
* The model can't see who's starting in goal unless the starter is hurt, so check the starters before betting.

Honest limits: injury values are rules of thumb, not true replacement level, the backtest can't grade them because old injury reports aren't saved, it doesn't know weather, late scratches or confirmed goalies, NFL margins bunch on 3 and 7 more than a normal curve assumes, and betting markets are sharp. Treat it as a screen for where to look, and let the Model check tab earn your trust over a few weeks.

## Data

* ESPN's public site API (free, no key, unofficial, so formats can change; `doctor` will tell you).
* Lines: ESPN (DraftKings), optionally The Odds API v4 for every book.
* Everything is saved in `sportsedge.db` (SQLite) in this folder.

