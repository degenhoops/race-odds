# race-odds

Pulls Formula 1 and NASCAR odds from **Bovada** and **FanDuel** and saves them into one clean CSV, with every market and driver name lined up so the two books can be compared row for row.

One script, one dependency, no browser.

```
2026-10-27 14:04:52 INFO Linked events: nascar cup series = xfinity 500
2026-10-27 14:04:52 INFO   fanduel  F1 Mexico Grand Prix   race_winner=22, top3=22, top10=22, fastest_lap=22 ...
2026-10-27 14:04:52 INFO Done: 487 rows (bovada=77, fanduel=410) saved to racing_odds.csv
```

## What it does

Both sportsbooks load their odds from internal JSON APIs. `race.py` calls those endpoints directly with `requests`, runs each reply through a parser for that book, and writes rows with identical columns no matter where they came from.

On top of the raw prices it handles the annoying parts of comparing books:

- **Driver names** are normalized, so "Andrea Kimi Antonelli", "Kimi Antonelli" and "Pérez" vs "Perez" all land on one key.
- **Markets** are sorted into shared labels. Bovada's "Race Lines | Outright" and FanDuel's "GP - Race Winner" both become `race_winner`.
- **Events** are matched across books, even when the names have nothing in common. Bovada calls the race "Xfinity 500" and FanDuel calls it "NASCAR - Cup Series - Race". Both end up with the same `event_key`.
- **Prices** come out as American odds, decimal odds and implied probability. The book's own decimal price is used when it has one, since American odds are rounded.

## Setup

Needs Python 3.9 or newer.

```bash
git clone https://github.com/degenhoops/race-odds.git
cd race-odds
pip install -r requirements.txt
```

## Usage

```bash
python race.py                      # both books
python race.py --book bovada        # one book
python race.py --filter formula     # only rows whose series, path or event contain "formula"
python race.py --limit 10           # read at most 10 events per book (default 30)
python race.py --dump               # also save raw API replies to debug/
python race.py --fd-state pa        # FanDuel state host (default nj)
```

Run it once or twice a day. It is not built to hammer the books on a loop.

### Flags

| Flag | What it does |
|---|---|
| `--book` | `all` (default), `bovada` or `fanduel` |
| `--filter TEXT` | Keep rows whose series, path or event name contains the text |
| `--limit N` | Most events to read per book, default 30 |
| `--dump` | Save every raw reply to `debug/` for troubleshooting |
| `--fd-state XX` | Two-letter FanDuel state code for the API host, default `nj` |
| `--fd-url URL` | FanDuel landing page request copied from the browser (or env `FANDUEL_URL`) |
| `--fd-event-url URL` | FanDuel event page request copied from the browser (or env `FANDUEL_EVENT_URL`) |

## Output

All files are written next to `race.py`.

| File | What's in it |
|---|---|
| `racing_odds.csv` | The latest run. Replaced every time |
| `racing_odds_history.csv` | Every run appended. Use it to track line moves |
| `race.log` | Rotating log, capped at about 4 MB total |
| `debug/` | Raw replies, only when `--dump` is used |

The CSVs are saved with a UTF-8 BOM so Excel shows accented names correctly. If `racing_odds.csv` is open in Excel during a run, that run is saved under a timestamped name instead of failing.

### Columns

| Column | Example | Notes |
|---|---|---|
| `book` | `fanduel` | |
| `event_id` | `36240103` | The book's own event ID |
| `series` | `Formula 1` | |
| `path` | `Mexico GP > Formula 1 > Motor Sports` | The book's category path |
| `event` | `F1 Mexico Grand Prix` | Event name as the book shows it |
| `event_key` | `mexico` | **Join key** for the event across books |
| `start_utc` | `2026-10-30T18:30:00+00:00` | |
| `market` | `GP - Top 3 Finish` | Market name as the book shows it |
| `market_key` | `top3` | **Join key** for the market across books |
| `selection` | `Max Verstappen` | |
| `selection_norm` | `max verstappen` | **Join key** for the driver or team |
| `line` | `2.5` | Only for markets that have one |
| `american` | `+135` | |
| `decimal` | `2.35` | |
| `implied_prob` | `0.4255` | `1 / decimal`, vig included |
| `pulled_utc` | `2026-10-27T19:04:34+00:00` | When the run happened |

To compare books, join on `event_key` + `market_key` + `selection_norm`.

### Market keys

| Key | Covers |
|---|---|
| `race_winner` | Outright race winner |
| `championship_winner` | Season championship and futures winners |
| `constructor_championship` | Constructors' championship |
| `constructor_winner` | Team of the winning driver, winning car |
| `top2` `top3` `top4` `top5` `top6` `top10` | Top N finish (podium counts as `top3`) |
| `exact_2` `exact_3` ... | Finish in exactly that position |
| `points` | Points finish |
| `fastest_lap` | Driver with the fastest lap |
| `team_fastest_lap` | Team with the fastest lap |
| `qualifying_winner` | Pole / qualifying winner |
| `qualifying_constructor` | Team or car of the pole sitter |
| `both_drivers_points` `both_drivers_top5` `both_drivers_top3` | Both team drivers hit the mark |
| `team_driver_points` `team_driver_top5` `team_driver_top3` | Either team driver hits the mark |
| `team_most_points` | Team to score the most points |
| `head_to_head` | Driver matchups |
| `combo` | Doubles like "Win Race and Fastest Lap" |
| `safety_car` | Safety car yes/no |
| `last_place` | Last position |
| `classified` | Classified finish |
| `other` | Anything not listed above |

## How events get matched

1. Each event name is boiled down to the words that identify it. "Mexico GP" and "F1 Mexico Grand Prix" both become `mexico`. Years and filler words like "grand", "prix" and "race" are dropped.
2. Country spellings go through `EVENT_ALIASES`, so "Mexican Grand Prix" and "Mexico City GP" also become `mexico`.
3. Events that still don't match get linked by series and type. If each book has exactly one Cup Series race on the board, those are the same race, whatever they're called. Same for each championship. The log shows every link it makes.

Anything left unmatched is listed in the log under `Events only one book has`. A race showing up there usually needs a new `EVENT_ALIASES` entry. A future that only one book offers showing up there is normal.

Driver spellings that differ between books go in `ALIASES` at the top of `race.py`.

## Scheduling

**Linux / macOS (cron)**, twice a day at 9:00 and 17:00:

```
0 9,17 * * * cd /path/to/race-odds && /usr/bin/python3 race.py > /dev/null 2>&1
```

**Windows (Task Scheduler):** create a basic task, set the program to `python`, the argument to `race.py`, and "Start in" to the folder that holds `race.py`.

A lock file stops two runs from writing at once. If a run crashes, the lock clears itself after 30 minutes.

## Troubleshooting

### FanDuel event pages

FanDuel's motorsport landing page carries most markets. Each event also has its own page with the full list, and FanDuel changes the parameters that page expects from time to time. `race.py` tries a few known request shapes and keeps the first one that answers. If none of them answer, the log says so and the run keeps the landing page markets.

To fix it for good:

1. Open any race on [sportsbook.fanduel.com](https://sportsbook.fanduel.com) in Chrome or Edge.
2. Press `F12`, open the **Network** tab, and type `event-page` in the filter box.
3. Refresh the page, right click the request, and choose **Copy > Copy URL**.
4. Run `python race.py --fd-event-url "PASTE_URL_HERE"`, or set it once as the `FANDUEL_EVENT_URL` environment variable.

The landing page works the same way: filter for `content-managed-page` and pass it with `--fd-url`.

### Blocked, 403 or 429

The book is turning requests away or rate limiting. Wait a few hours. Running less often helps. FanDuel also only answers from states where it operates, so try `--fd-state` with your state.

### Odd FanDuel start dates

FanDuel sometimes lists placeholder dates years out on NASCAR events. The row is still fine, it just sorts late.

### Something changed and a book returns nothing

Run with `--dump` and look at the raw reply in `debug/`. The parsers (`parse_bovada`, `parse_fanduel`) are plain functions with no network code, so a format change is usually a few lines to patch.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Data saved |
| `1` | Nothing scraped |
| `2` | Nothing scraped and a book blocked the request |
| `130` | Stopped with Ctrl+C |

## Disclaimer

Personal-use tool. Not affiliated with, endorsed by or connected to Bovada, FanDuel or any sportsbook. It reads the same public odds the books show on their own websites. Check each site's terms before using it, don't resell the data, and keep the request rate low. Nothing here is betting advice.
