#!/usr/bin/env python3
"""race.py: pull F1 and NASCAR odds from Bovada and FanDuel into one CSV.

Personal-use tool. Not affiliated with any sportsbook, not for resale, not betting advice.
See README.md for setup, flags and output columns.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import random
import re
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

try:
    import requests
except ImportError:
    requests = None

HERE = Path(__file__).resolve().parent
LATEST = HERE / "racing_odds.csv"
HISTORY = HERE / "racing_odds_history.csv"
LOG_FILE = HERE / "race.log"
DEBUG_DIR = HERE / "debug"
LOCK_FILE = HERE / ".race.lock"
LOCK_MAX_AGE_S = 1800
CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 25
RETRY_STATUS = {408, 500, 502, 503, 504}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0.0.0 Safari/537.36")

BOVADA_BASE = "https://www.bovada.lv"
BOVADA_API = "/services/sports/event/coupon/events/A/description/motor-sports"
BOVADA_QS = {"marketFilterId": "def", "preMatchOnly": "true", "lang": "en"}

FANDUEL_AK = "FhMFpcPWXMeyZxOx"
FANDUEL_TZ = "America/New_York"
FANDUEL_PAGES = ({"page": "SPORT", "eventTypeId": "8"},)
# Request shapes FanDuel's site has used for event pages. The first one that answers is kept.
FANDUEL_EVENT_VARIANTS = (
    {"tab": "popular", "useCombinedTouchpoints": "false", "useQuickBets": "true"},
    {"betexRegion": "GBR", "capiJurisdiction": "intl", "currencyCode": "USD",
     "exchangeLocale": "en_US", "includePrices": "true", "language": "en",
     "priceHistory": "1", "regionCode": "NAMERICA"},
    {},
)
EVENT_PAGE_GIVE_UP = 2

FIELDS = ["book", "event_id", "series", "path", "event", "event_key", "start_utc", "market",
          "market_key", "selection", "selection_norm", "line", "american", "decimal",
          "implied_prob", "pulled_utc"]

log = logging.getLogger("race")


class FetchError(Exception):
    """A site couldn't be read: bad status, bad reply, or bad setup."""


class Blocked(FetchError):
    """The site refused the request (401/403/429 or a block page). Never retried.

    `partial` keeps rows already collected so a block mid-run doesn't lose them.
    """

    def __init__(self, message: str, partial: list | None = None):
        super().__init__(message)
        self.partial = partial or []


@dataclass(frozen=True)
class Options:
    limit: int = 30
    save_raw: bool = False
    fd_state: str = "nj"
    fd_url: str = ""
    fd_event_url: str = ""


# ---------- names, events, markets, prices ----------

ALIASES = {
    "alexander albon": "alex albon",
    "andrea kimi antonelli": "kimi antonelli",
    "checo perez": "sergio perez",
}

EVENT_ALIASES = {
    "mexican": "mexico", "mexico city": "mexico", "british": "great britain",
    "japanese": "japan", "italian": "italy", "spanish": "spain", "dutch": "netherlands",
    "belgian": "belgium", "austrian": "austria", "hungarian": "hungary",
    "canadian": "canada", "australian": "australia", "brazilian": "brazil",
    "sao paulo": "brazil", "saudi arabian": "saudi arabia",
}

EVENT_NOISE = {"gp", "grand", "prix", "race", "specialbets", "formula", "f1", "motor", "sports"}
YEAR = re.compile(r"^20\d\d$")

# Series detection for cross-book linking. Checked in order, first match wins.
SERIES_TAGS = [
    ("truck", re.compile(r"truck")),
    ("oreilly", re.compile(r"o ?reilly|xfinity series")),
    ("cup", re.compile(r"\bcup\b")),
    ("f1", re.compile(r"formula 1|\bf1\b")),
    ("indycar", re.compile(r"indy ?car")),
]
FUTURES = re.compile(r"futures|championship|season")

OVER_UNDER = re.compile(r"^(?:over|under)\s+(\d+(?:\.\d+)?)$", re.IGNORECASE)
EXTRA_LETTERS = str.maketrans({"đ": "d", "ł": "l", "ø": "o", "ß": "ss", "æ": "ae"})
FRACTION = re.compile(r"\.(\d+)")

FINISH = re.compile(r"podium|top ?(\d+)\b|points")
WINNER_RULE = re.compile(r"outright|\brace winner$|^(championship )?winner$|championship")
TEAM = re.compile(r"\bteam\b|\bcar\b|constructor")


def norm_name(text: str) -> str:
    """Lowercase, strip accents, punctuation and Jr/Sr so one name matches across books."""
    n = unicodedata.normalize("NFKD", str(text).lower()).translate(EXTRA_LETTERS)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = re.sub(r"['’`]", "", n)
    n = re.sub(r"(?<!\d)\.|\.(?!\d)", "", n)
    n = re.sub(r"[^a-z0-9.]+", " ", n)
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


def selection_norm(selection: str) -> str:
    key = norm_name(selection)
    return ALIASES.get(key, key)


def event_key(event: str) -> str:
    """"Mexico GP" and "F1 Mexico Grand Prix" both give "mexico"."""
    words = [w for w in norm_name(event).split() if w not in EVENT_NOISE and not YEAR.match(w)]
    key = " ".join(words) or norm_name(event)
    return EVENT_ALIASES.get(key, key)


def finish_tag(m: str) -> str:
    x = FINISH.search(m)
    if not x:
        return ""
    if x.group(0) == "podium":
        return "top3"
    return f"top{x.group(1)}" if x.group(1) else "points"


def market_key(market: str, event: str) -> str:
    """Sort a market into a label shared by all books, else "other"."""
    m = market.lower()
    if re.search(r"head.to.head|/rest of field|/any other", m):
        return "head_to_head"
    if re.search(r"double|win race and", m):
        return "combo"
    if re.search(r"^qual|qualif|pole|fastest qualifier", m):
        return "qualifying_constructor" if TEAM.search(m) else "qualifying_winner"
    if re.search(r"\bboth\b.*\bdrivers?\b", m):
        return "both_drivers_" + (finish_tag(m) or "other")
    if re.search(r"team to have a driver|any driver", m):
        return "team_driver_" + (finish_tag(m) or "other")
    if "fastest lap" in m:
        return "team_fastest_lap" if TEAM.search(m) else "fastest_lap"
    if "most points" in m:
        return "team_most_points"
    if re.search(r"team of winning driver|winning (car|team|constructor)|car to win|which car will win", m):
        return "constructor_winner"
    if re.search(r"last (position|place)", m):
        return "last_place"
    exact = re.search(r"exactly (\d+)", m)
    if exact:
        return f"exact_{exact.group(1)}"
    if "safety car" in m:
        return "safety_car"
    if "classif" in m:
        return "classified"
    tag = finish_tag(m)
    if tag:
        return tag
    if WINNER_RULE.search(m):
        combined = m + " " + event.lower()
        if "championship" in combined or "futures" in combined:
            return "constructor_championship" if "constructor" in combined else "championship_winner"
        return "race_winner"
    return "other"


def to_float(value: object) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def to_american(value: object) -> str:
    """'EVEN', '+150', 150 or -110 -> '+150' style text. '' if it isn't valid odds."""
    if value in (None, ""):
        return ""
    text = str(value).strip().upper()
    if text in ("EVEN", "EV", "PK"):
        return "+100"
    num = to_float(text)
    if num is None:
        return ""
    n = int(round(num))
    if abs(n) < 100:
        return ""
    return f"+{n}" if n > 0 else str(n)


def american_to_decimal(american: str) -> float:
    n = int(american)
    return 1 + n / 100 if n > 0 else 1 + 100 / abs(n)


def decimal_to_american(decimal: float) -> str:
    if decimal <= 1:
        return ""
    if decimal >= 2:
        return f"+{round((decimal - 1) * 100)}"
    return str(-round(100 / (decimal - 1)))


def iso_utc(value: object) -> str:
    """Any timestamp shape -> 2026-11-01T20:00:00+00:00. Unparseable text is returned as is."""
    if not value:
        return ""
    text = FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"),
                        str(value).replace("Z", "+00:00"), count=1)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def ms_to_iso(ms: object) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def as_dict(obj: object, what: str) -> dict:
    if not isinstance(obj, dict):
        raise FetchError(f"{what}: unexpected reply shape ({type(obj).__name__})")
    return obj


def make_row(*, book: str, event_id: object, series: str, path: str, event: str, start: object,
             market: str, selection: str, line: object, american: object,
             decimal: object, pulled: str) -> dict | None:
    """One CSV row, or None if there's no usable price."""
    # The book's decimal is more exact than its rounded American price, so it wins.
    dec = to_float(decimal)
    am = to_american(american)
    if (dec is None or dec <= 1) and am:
        dec = american_to_decimal(am)
    if dec is None or dec <= 1 or not selection:
        return None
    if not am:
        am = decimal_to_american(dec)

    line_text = "" if str(line) in ("None", "", "0", "0.0") else str(line)
    if not line_text:
        ou = OVER_UNDER.match(selection.strip())
        line_text = ou.group(1) if ou else ""
    return {
        "book": book, "event_id": str(event_id), "series": series, "path": path, "event": event,
        "event_key": event_key(event), "start_utc": iso_utc(start), "market": market,
        "market_key": market_key(market, event), "selection": selection,
        "selection_norm": selection_norm(selection), "line": line_text, "american": am,
        "decimal": round(dec, 3), "implied_prob": round(1 / dec, 4), "pulled_utc": pulled,
    }


# ---------- parsers: JSON in, rows out ----------

def parse_bovada(data: object, pulled: str) -> list[dict]:
    """Bovada nests path groups > events > display groups > markets > outcomes."""
    rows = []
    for group in data if isinstance(data, list) else []:
        names = [p.get("description", "") for p in group.get("path", [])]
        path = " > ".join(names)
        series = names[-2] if len(names) >= 2 else ""
        for ev in group.get("events", []):
            for dg in ev.get("displayGroups", []):
                for mkt in dg.get("markets", []):
                    if mkt.get("status") in ("S", "C"):
                        continue
                    name = mkt.get("description", "")
                    if dg.get("description") and dg["description"] != name:
                        name = f"{dg['description']} | {name}"
                    for out in mkt.get("outcomes", []):
                        if out.get("status") in ("S", "C"):
                            continue
                        price = out.get("price") or {}
                        row = make_row(
                            book="bovada", event_id=ev.get("id", ""), series=series, path=path,
                            event=ev.get("description", ""), start=ms_to_iso(ev.get("startTime")),
                            market=name, selection=out.get("description", ""),
                            line=price.get("handicap"), american=price.get("american"),
                            decimal=price.get("decimal"), pulled=pulled)
                        if row:
                            rows.append(row)
    return rows


def parse_fanduel(data: object, pulled: str) -> list[dict]:
    """FanDuel keeps events, markets and competitions in separate tables under "attachments"."""
    att = (data.get("attachments") if isinstance(data, dict) else None) or {}
    events = att.get("events") or {}
    competitions = att.get("competitions") or {}
    rows = []
    for mkt in (att.get("markets") or {}).values():
        if mkt.get("marketStatus") not in (None, "OPEN"):
            continue
        ev = events.get(str(mkt.get("eventId"))) or {}
        competition = (competitions.get(str(ev.get("competitionId"))) or {}).get("name", "")
        for runner in mkt.get("runners") or []:
            if runner.get("runnerStatus") not in (None, "ACTIVE"):
                continue
            odds = runner.get("winRunnerOdds") or {}
            row = make_row(
                book="fanduel", event_id=mkt.get("eventId", ""), series=competition,
                path=competition, event=ev.get("name", ""), start=ev.get("openDate"),
                market=mkt.get("marketName", ""), selection=runner.get("runnerName", ""),
                line=runner.get("handicap"),
                american=(odds.get("americanDisplayOdds") or {}).get("americanOdds"),
                decimal=((odds.get("trueOdds") or {}).get("decimalOdds") or {}).get("decimalOdds"),
                pulled=pulled)
            if row:
                rows.append(row)
    return rows


# ---------- HTTP ----------

class Http:
    """requests.Session with timeouts, one retry for flaky errors, and block detection."""

    def __init__(self, referer: str = ""):
        if requests is None:
            raise FetchError("The 'requests' package is missing. Run: pip install -r requirements.txt")
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        if referer:
            self.s.headers["Referer"] = referer

    def get(self, url: str, params: dict | None = None) -> object:
        """GET and return parsed JSON. 401/403/429 or HTML raises Blocked. 5xx gets one retry."""
        shown = url.split("?")[0]
        last = "request failed"
        for attempt in range(2):
            if attempt:
                time.sleep(2)
            try:
                r = self.s.get(url, params=params, timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S))
            except requests.RequestException as e:
                # Not str(e): it holds the full URL with the key, and this text goes to the log.
                last = f"{type(e).__name__} ({shown})"
                continue
            code = r.status_code
            if code in (401, 403, 429):
                raise Blocked(f"HTTP {code} from {shown} (blocked, rate limited, or not offered "
                              "in your region)")
            if code in RETRY_STATUS:
                last = f"HTTP {code} from {shown}"
                continue
            if code != 200:
                raise FetchError(f"HTTP {code} from {shown}")
            try:
                return r.json()
            except ValueError:
                if r.text.lstrip().startswith("<"):
                    raise Blocked(f"Got an HTML page instead of JSON from {shown} "
                                  "(probably a block or captcha page)") from None
                last = f"reply from {shown} was not valid JSON"
        raise FetchError(last)


def dump(name: str, obj: object) -> None:
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        safe = re.sub(r"[^\w.-]+", "_", name)
        (DEBUG_DIR / safe).write_text(json.dumps(obj, indent=1), encoding="utf-8")
        log.info("  raw reply saved: debug/%s", safe)
    except (OSError, TypeError, ValueError) as e:
        log.warning("Could not save debug/%s: %s", name, e)


# ---------- scrapers ----------

def scrape_bovada(pulled: str, opts: Options) -> list[dict]:
    http = Http(BOVADA_BASE + "/sports/motor-sports")
    data = http.get(BOVADA_BASE + BOVADA_API, BOVADA_QS)
    if opts.save_raw:
        dump("bovada_raw.json", data)
    return parse_bovada(data, pulled)


def fd_url_parts(url: str, flag: str, need_ak: bool) -> tuple[str, str, dict]:
    """Check a FanDuel URL copied from the browser. Returns (netloc, path, query)."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https" or not host.endswith("fanduel.com") or "/api/" not in parts.path:
        raise FetchError(f"{flag} must be a https://sbapi.<state>.sportsbook.fanduel.com/api/... URL "
                         "copied from your browser's Network tab")
    query = dict(parse_qsl(parts.query))
    if need_ak and not query.get("_ak"):
        raise FetchError(f"{flag} has no _ak parameter. Copy the full request URL")
    return parts.netloc, parts.path, query


def fanduel_target(opts: Options) -> tuple[str, str, str, list[tuple[str, dict]]]:
    """(api base, _ak key, timezone, [(landing page url, params), ...])."""
    url = opts.fd_url or os.environ.get("FANDUEL_URL", "")
    if url:
        netloc, path, query = fd_url_parts(url, "--fd-url", need_ak=True)
        return f"https://{netloc}/api", query["_ak"], query.get("timezone") or FANDUEL_TZ, \
            [(f"https://{netloc}{path}", query)]
    base = f"https://sbapi.{opts.fd_state}.sportsbook.fanduel.com/api"
    candidates = [(f"{base}/content-managed-page", {**page, "_ak": FANDUEL_AK, "timezone": FANDUEL_TZ})
                  for page in FANDUEL_PAGES]
    return base, FANDUEL_AK, FANDUEL_TZ, candidates


def fanduel_event_variants(opts: Options, base: str) -> list[tuple[str, dict]]:
    """[(event-page url, extra params), ...] to try, in order."""
    url = opts.fd_event_url or os.environ.get("FANDUEL_EVENT_URL", "")
    if url:
        netloc, path, query = fd_url_parts(url, "--fd-event-url", need_ak=False)
        query.pop("eventId", None)
        return [(f"https://{netloc}{path}", query)]
    return [(f"{base}/event-page", extra) for extra in FANDUEL_EVENT_VARIANTS]


def scrape_fanduel(pulled: str, opts: Options) -> list[dict]:
    """Read the motorsport landing page, then each event page for the full market list."""
    http = Http("https://sportsbook.fanduel.com/motorsport")
    http.s.headers["Origin"] = "https://sportsbook.fanduel.com"
    base, ak, tz, candidates = fanduel_target(opts)

    data, tried = None, []
    for url, params in candidates:
        label = " ".join(f"{k}={params[k]}" for k in ("page", "customPageId", "eventTypeId")
                         if k in params) or url
        try:
            reply = http.get(url, params)
        except Blocked:
            raise
        except FetchError as e:
            tried.append(f"{label}: {e}")
            continue
        if (as_dict(reply, "FanDuel page").get("attachments") or {}).get("events"):
            data = reply
            log.info("FanDuel: using page %s", label)
            break
        tried.append(f"{label}: reply had no events")
    if data is None:
        raise FetchError("FanDuel: no motorsport page worked (" + "; ".join(tried) + "). Try "
                         "--fd-state with your state code, or pass the 'content-managed-page' "
                         "request URL from your browser's Network tab as --fd-url")
    if opts.save_raw:
        dump("fanduel_page.json", data)
    events = data["attachments"]["events"]
    rows = parse_fanduel(data, pulled)
    log.info("FanDuel: %d events, %d rows on the motorsport page", len(events), len(rows))

    variants = fanduel_event_variants(opts, base)
    working, misses = None, 0
    for eid in sorted(events, key=lambda i: events[i].get("openDate", ""))[:opts.limit]:
        name = events[eid].get("name", eid)
        raw, last = None, None
        for n, (url, extra) in enumerate([working] if working else variants):
            time.sleep(random.uniform(2, 4) if n == 0 else 1)
            try:
                raw = http.get(url, {**extra, "_ak": ak, "eventId": eid, "timezone": tz})
            except Blocked as e:
                e.partial = rows
                raise
            except FetchError as e:
                last = e
                continue
            working = (url, extra)
            break
        if raw is None:
            if working is None:
                misses += 1
                if misses >= EVENT_PAGE_GIVE_UP:
                    log.warning("FanDuel event pages are not answering (%s). Keeping landing page "
                                "markets only. See README: FanDuel event pages", last)
                    break
            log.warning("Skipped %s: %s", name, last)
            continue
        if opts.save_raw:
            dump(f"fanduel_event_{eid}.json", raw)
        got = parse_fanduel(raw, pulled)
        log.info("  %-45s %4d rows", str(name)[:45], len(got))
        rows.extend(got)
    return rows


SCRAPERS = {"bovada": scrape_bovada, "fanduel": scrape_fanduel}


# ---------- matching books up ----------

def event_profile(row: dict) -> tuple[str, str]:
    """(series tag, kind) for an event, e.g. ("cup", "race") or ("f1", "futures_constructor")."""
    text = norm_name(" ".join((row["event"], row["path"], row["series"])))
    tag = next((t for t, rx in SERIES_TAGS if rx.search(text)), "")
    if FUTURES.search(text):
        kind = "futures_constructor" if "constructor" in text else "futures"
    else:
        kind = "race"
    return tag, kind


def link_events(rows: list[dict]) -> None:
    """Give one event_key to events the books name differently.

    Bovada says "Xfinity 500", FanDuel says "NASCAR - Cup Series - Race". When every book
    has exactly one event for the same series and kind, those events are the same race.
    """
    groups: dict[tuple, dict[str, set]] = {}
    for r in rows:
        profile = event_profile(r)
        if profile[0]:
            groups.setdefault(profile, {}).setdefault(r["book"], set()).add(r["event_key"])
    rename = {}
    for by_book in groups.values():
        if len(by_book) < 2 or any(len(keys) != 1 for keys in by_book.values()):
            continue
        keys = [next(iter(by_book[b])) for b in sorted(by_book)]
        for key in keys[1:]:
            if key != keys[0]:
                rename[key] = keys[0]
    for r in rows:
        r["event_key"] = rename.get(r["event_key"], r["event_key"])
    for old, new in sorted(rename.items()):
        log.info("Linked events: %s = %s", old, new)


def dedupe(rows: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in rows:
        key = (r["book"], r["event_id"], r["market"], r["selection"], r["line"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def log_unmatched(rows: list[dict]) -> None:
    """List events only one book has. Usually a naming gap that needs an EVENT_ALIASES entry."""
    seen: dict[str, set] = {}
    for r in rows:
        seen.setdefault(r["event_key"], set()).add(r["book"])
    if len({b for books in seen.values() for b in books}) < 2:
        return
    lonely = sorted(f"{key} ({next(iter(books))})" for key, books in seen.items() if len(books) == 1)
    if lonely:
        shown = ", ".join(lonely[:15]) + (f", and {len(lonely) - 15} more" if len(lonely) > 15 else "")
        log.info("Events only one book has: %s", shown)


# ---------- saving ----------

def write_latest(rows: list[dict], now: datetime) -> Path:
    """Write a temp file then swap it in, so a crash can't leave half a CSV."""
    tmp = LATEST.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    try:
        os.replace(tmp, LATEST)
        return LATEST
    except PermissionError:
        alt = LATEST.with_name(f"racing_odds_{now:%Y%m%d_%H%M}.csv")
        os.replace(tmp, alt)
        log.warning("%s is open in another program, so this run was saved as %s.", LATEST.name, alt.name)
        return alt


def csv_header(path: Path) -> list[str]:
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return next(csv.reader(f), [])
    except (UnicodeDecodeError, csv.Error):
        return ["<unreadable>"]


def append_history(rows: list[dict], now: datetime) -> None:
    """Append this run. A history file with old columns is moved aside, not mixed."""
    try:
        header = csv_header(HISTORY) if HISTORY.exists() else []
        if header and header != FIELDS:
            aside = HISTORY.with_name(f"racing_odds_history_old_{now:%Y%m%d_%H%M}.csv")
            os.replace(HISTORY, aside)
            log.info("The history columns changed, so the old file was kept as %s", aside.name)
            header = []
        with open(HISTORY, "a" if header else "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            if not header:
                writer.writeheader()
            writer.writerows(rows)
    except PermissionError:
        log.warning("%s is open in another program, so history was skipped this run.", HISTORY.name)


# ---------- running it ----------

def setup_logging() -> None:
    if log.handlers:
        return
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    for handler in (RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"),
                    logging.StreamHandler()):
        handler.setFormatter(fmt)
        log.addHandler(handler)


def take_lock() -> bool:
    """One run at a time. Goes by lock age, not PID, so it behaves the same on Windows."""
    for _ in range(2):
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - LOCK_FILE.stat().st_mtime
            except FileNotFoundError:
                continue
            if age < LOCK_MAX_AGE_S:
                return False
            LOCK_FILE.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return True
    return False


def collect(books: list[str], opts: Options, pulled: str) -> tuple[list[dict], dict, bool]:
    """Run each scraper. One book failing never stops the others."""
    rows, results, blocked = [], {}, False
    for i, book in enumerate(books):
        if i:
            time.sleep(random.uniform(1, 2))
        try:
            got = SCRAPERS[book](pulled, opts)
        except Blocked as e:
            blocked = True
            rows.extend(e.partial)
            results[book] = f"blocked ({len(e.partial)} rows kept)"
            log.error("%s blocked: %s. Give it a few hours before trying again.", book, e)
        except FetchError as e:
            results[book] = "failed"
            log.error("%s failed: %s", book, e)
        except Exception:
            results[book] = "error"
            log.exception("%s crashed", book)
        else:
            results[book] = str(len(got))
            rows.extend(got)
            log.info("%s: %d rows", book, len(got))
    return rows, results, blocked


def run(args: argparse.Namespace) -> int:
    books = list(SCRAPERS) if args.book == "all" else [args.book]
    opts = Options(limit=args.limit, save_raw=args.dump, fd_state=args.fd_state,
                   fd_url=args.fd_url or "", fd_event_url=args.fd_event_url or "")
    pulled = datetime.now(timezone.utc).isoformat(timespec="seconds")

    rows, results, blocked = collect(books, opts, pulled)

    if args.filter:
        needle = args.filter.lower()
        rows = [r for r in rows if needle in " ".join((r["series"], r["path"], r["event"])).lower()]
    rows = dedupe(rows)

    if not rows:
        log.error("No odds found. The previous file was left alone.")
        return 2 if blocked else 1

    link_events(rows)
    rows.sort(key=lambda r: (r["book"], r["start_utc"], r["event"], r["market"],
                             -r["implied_prob"], r["selection"]))
    now = datetime.now()
    saved = write_latest(rows, now)
    append_history(rows, now)

    by_event: dict[tuple, Counter] = {}
    for r in rows:
        by_event.setdefault((r["book"], r["event"]), Counter())[r["market_key"]] += 1
    for (book, event), counts in by_event.items():
        log.info("  %-8s %-34s %s", book, event[:34],
                 ", ".join(f"{k}={n}" for k, n in counts.most_common()))
    log_unmatched(rows)
    log.info("Done: %d rows (%s) saved to %s", len(rows),
             ", ".join(f"{k}={v}" for k, v in results.items()), saved.name)
    incomplete = [b for b, v in results.items() if not v.isdigit()]
    if incomplete:
        log.warning("Incomplete run: no full data from %s", ", ".join(incomplete))
    return 0


def positive_int(text: str) -> int:
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def state_code(text: str) -> str:
    code = text.strip().lower()
    if not re.fullmatch(r"[a-z]{2}", code):
        raise argparse.ArgumentTypeError("must be a two-letter state code such as nj or pa")
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="F1 and NASCAR odds from Bovada and FanDuel, saved as CSV")
    ap.add_argument("--book", choices=["all", *SCRAPERS], default="all")
    ap.add_argument("--filter", help="keep rows whose series, path or event contains this text")
    ap.add_argument("--dump", action="store_true", help="also save every raw reply to debug/")
    ap.add_argument("--limit", type=positive_int, default=30, help="most events to read per book (default 30)")
    ap.add_argument("--fd-state", type=state_code, default="nj",
                    help="FanDuel state code used when --fd-url isn't given (default nj)")
    ap.add_argument("--fd-url", help="FanDuel 'content-managed-page' request URL copied from your "
                                     "browser (or env FANDUEL_URL)")
    ap.add_argument("--fd-event-url", help="FanDuel 'event-page' request URL copied from your "
                                           "browser (or env FANDUEL_EVENT_URL)")
    args = ap.parse_args(argv)

    setup_logging()
    if requests is None:
        log.error("The 'requests' package is not installed. Run: pip install -r requirements.txt")
        return 1
    if not take_lock():
        log.error("Another run is going (or one crashed less than %d minutes ago). Exiting.",
                  LOCK_MAX_AGE_S // 60)
        return 1
    try:
        return run(args)
    except KeyboardInterrupt:
        log.info("Stopped.")
        return 130
    except Exception:
        log.exception("The run failed")
        return 1
    finally:
        LOCK_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
