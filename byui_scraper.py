#!/usr/bin/env python3
"""
byui_scraper.py - pull every section for a BYU-Idaho term from the PUBLIC
Class Schedule page (https://student.byui.edu/ics/class_schedule/) into
SQLite and a JSON file.

How it works (per term):
  1. GET the search page, read the term and delivery-method dropdowns.
  2. For each delivery method (P, O, B, X, M, V...): GET a fresh copy of the
     form, POST the search, then POST the "Show all" postback so pagination
     goes away.
  3. Parse the course table, then replace that term's rows in SQLite in one
     transaction and export sections.json.

Requests are spaced at least 10 s apart (the Crawl-delay in byui.edu's
robots.txt). A full term is ~19 requests, so about 3-4 minutes.

Setup:
    pip install requests beautifulsoup4

Usage (quote the term; the semicolon is special in most shells):
    python byui_scraper.py --contact you@example.com
    python byui_scraper.py --contact you@example.com --term "2027;WI" --term "2027;SP"
    python byui_scraper.py --contact you@example.com --list-terms
    python byui_scraper.py --offline          # re-parse saved pages, no requests

Instructor, seats and status only appear when logged in. Either:
  * --cookie-file cookies.txt  (a cookies.txt export from your browser while logged in to
    student.byui.edu, or a file with the raw Cookie header; it expires, so re-export),
    or
  * save the logged-in results page from your browser ("Save as", single file/.mhtml)
    as <cache>/2027_WI_P.mhtml (name = term + delivery code) and use --offline.
Then run rmp.py to add RateMyProfessors ratings.
"""
import argparse
import datetime as dt
import email
import email.policy
import json
import pathlib
import re
import sqlite3
import sys
import time
from urllib.parse import urlsplit
import requests
from bs4 import BeautifulSoup

URL = "https://student.byui.edu/ics/class_schedule/"
P = "pg0$V$tabSearch$"            # prefix of the search-form field names
SEARCH_BUTTON = (P + "btnSearch", "Search")
DEFAULT_TERM = "2027;WI"
MIN_DELAY = 10                    # seconds; byui.edu robots.txt Crawl-delay


class ScrapeError(Exception):
    pass


def warn(msg):
    print(f"  WARNING: {msg}", file=sys.stderr)


def load_cookies(path):
    """(name, value) pairs from either a raw 'a=1; b=2' Cookie header string or a
    Netscape cookies.txt export (what browser cookie-export extensions produce).
    Only cookies that apply to student.byui.edu and haven't expired are kept."""
    text = pathlib.Path(path).read_text(encoding="utf-8").strip()
    if "\t" not in text:
        raw = re.sub(r"^cookie:\s*", "", text, flags=re.I).replace("\n", " ")
        pairs = [tuple(p.strip().split("=", 1)) for p in raw.split(";") if "=" in p]
    else:
        host = urlsplit(URL).hostname
        now = time.time()
        pairs = []
        for line in text.splitlines():
            line = re.sub(r"^#HttpOnly_", "", line)
            if not line.strip() or line.startswith("#"):
                continue
            f = line.split("\t")
            if len(f) < 7:
                continue
            domain, _, _, _, expires, name, value = f[:7]
            domain = domain.lstrip(".").lower()
            if host != domain and not host.endswith("." + domain):
                continue
            if expires.isdigit() and 0 < int(expires) < now:
                continue                  # 0 = session cookie, which is fine
            pairs.append((name, value.strip()))
    if not any(n == ".ASPXAUTH" for n, _ in pairs):
        warn("no .ASPXAUTH login cookie found in the cookie file; you may not be logged in")
    return pairs


# --------------------------------------------------------------------------
# Networking
# --------------------------------------------------------------------------
class Fetcher:
    """requests.Session wrapper: enforces a delay between requests, retries."""

    def __init__(self, contact, delay, cookies=None):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = (
            f"byui-class-scheduler/0.1 (personal project; contact: {contact})"
        )
        # In the jar (not a raw Cookie header) so they survive redirects.
        host = urlsplit(URL).hostname
        for name, value in cookies or []:
            self.session.cookies.set(name, value, domain=host, path="/")
        self.delay = delay
        self._last = 0.0

    def _wait(self):
        gap = time.monotonic() - self._last
        if gap < self.delay:
            time.sleep(self.delay - gap)

    def _request(self, method, **kwargs):
        last_error = None
        for attempt in range(3):
            self._wait()
            try:
                resp = self.session.request(method, URL, timeout=60, **kwargs)
            except requests.RequestException as e:
                last_error = e
            else:
                if resp.status_code < 500:
                    resp.raise_for_status()
                    if "charset" not in resp.headers.get("content-type", "").lower():
                        resp.encoding = "utf-8"
                    self._last = time.monotonic()
                    return resp.text
                last_error = requests.HTTPError(f"HTTP {resp.status_code}")
            self._last = time.monotonic()
            if attempt < 2:
                print(f"  request failed ({last_error}); retrying", file=sys.stderr)
                time.sleep(self.delay * 3)
        raise ScrapeError(f"{method} {URL} failed after 3 attempts: {last_error}")

    def get(self):
        return self._request("GET")

    def post(self, fields):
        return self._request("POST", data=fields)


# --------------------------------------------------------------------------
# WebForms helpers
# --------------------------------------------------------------------------
def form_fields(soup):
    """Everything a browser would submit from the page's form, in order,
    with default values. Returned as a list of (name, value) so duplicate
    names (the site-search 'q' fields) are preserved."""
    viewstate = soup.find("input", {"name": "__VIEWSTATE"})
    form = viewstate.find_parent("form") if viewstate else None
    if form is None:
        raise ScrapeError(
            "No __VIEWSTATE found; the page layout changed or the request was blocked."
        )
    fields = []
    for el in form.find_all(["input", "select", "textarea"]):
        name = el.get("name")
        if not name:
            continue
        if el.name == "input":
            kind = (el.get("type") or "text").lower()
            if kind in {"submit", "button", "image", "file", "reset"}:
                continue          # only the clicked button is sent (added by caller)
            if kind in {"checkbox", "radio"} and not el.has_attr("checked"):
                continue
            fields.append((name, el.get("value", "")))
        elif el.name == "select":
            opt = el.find("option", selected=True) or el.find("option")
            value = (opt.get("value", opt.get_text(strip=True)) if opt else "")
            fields.append((name, value))
        else:
            fields.append((name, el.get_text()))
    return fields


def with_overrides(fields, overrides):
    """Drop any existing fields named in `overrides`, then append them."""
    kept = [(k, v) for k, v in fields if k not in overrides]
    return kept + list(overrides.items())


def select_options(soup, name):
    sel = soup.find("select", {"name": name})
    if sel is None:
        raise ScrapeError(f"Dropdown {name!r} not found on the page.")
    return [(o.get("value", ""), o.get_text(strip=True)) for o in sel.find_all("option")]


def count_rows(soup):
    return len(soup.select("#tableCourses tbody tr"))


# --------------------------------------------------------------------------
# Scraping one (term, delivery method) search
# --------------------------------------------------------------------------
def scrape_method(fetcher, term, method):
    """Fresh GET -> search POST -> 'Show all' POST. Returns the results HTML."""
    soup = BeautifulSoup(fetcher.get(), "html.parser")
    overrides = {
        "pg0$V$DefaultPageTabs": "2",
        P + "ddlTermSearch": term,
        P + "ddlDeliveryMethod": method,
        SEARCH_BUTTON[0]: SEARCH_BUTTON[1],
    }
    html = fetcher.post(with_overrides(form_fields(soup), overrides))

    results = BeautifulSoup(html, "html.parser")
    if results.select_one("#tableCourses") is None:
        return html                               # nothing to show-all; caller warns

    before = count_rows(results)
    link = results.find("a", href=re.compile(r"lnkShowAll"))
    if link is not None:
        m = re.search(r"__doPostBack\('([^']*)'\s*,\s*'([^']*)'\)", link["href"])
        target, arg = (m.group(1), m.group(2)) if m else ("pg0$V$lnkShowAll", "")
        html = fetcher.post(with_overrides(
            form_fields(results), {"__EVENTTARGET": target, "__EVENTARGUMENT": arg}
        ))
        after = count_rows(BeautifulSoup(html, "html.parser"))
        if after < before:
            warn(f"{method}: 'Show all' returned fewer rows ({after}) than page 1 ({before}).")
    elif results.find("a", href=re.compile(r"ltrNavBottom")):
        warn(f"{method}: results are paginated but no 'Show all' link was found; "
             "this method's data is incomplete.")
    return html


def load_saved(path):
    """Read a saved page. Browser 'Save as single file' (.mhtml) captures are
    MIME bundles; plain .html files are returned as is."""
    raw = path.read_bytes()
    if not raw.lstrip().startswith((b"From:", b"MIME-Version:", b"Subject:", b"Date:")):
        return raw.decode("utf-8", errors="replace")
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    for part in msg.walk():      # the page may also bundle iframes; want the results frame
        if part.get_content_type() == "text/html":
            html = part.get_payload(decode=True).decode("utf-8", errors="replace")
            if "tableCourses" in html:
                return html
    raise ScrapeError(f"{path}: no course results table found in saved page")


def pages_for_term(fetcher, term, cache_dir, offline, only_methods=None):
    """Yield (delivery_method, results_html) for every delivery method."""
    key = term.replace(";", "_")
    if offline:
        files = sorted(p for p in cache_dir.glob(f"{key}_*")
                       if p.suffix.lower() in {".html", ".htm", ".mhtml"})
        if not files:
            raise ScrapeError(f"No cached pages for {term!r} in {cache_dir}/")
        for path in files:
            # '2027_WI_P.html' and '2027_WI_P_single.html' are both method P
            method = path.stem[len(key) + 1:].split("_")[0]
            yield method, load_saved(path)
        return

    soup = BeautifulSoup(fetcher.get(), "html.parser")
    terms = select_options(soup, P + "ddlTermSearch")
    if term not in [v for v, _ in terms]:
        listing = ", ".join(f"{v!r} ({label})" for v, label in terms if v)
        raise ScrapeError(f"Term {term!r} is not offered. Available: {listing}")
    methods = [v for v, _ in select_options(soup, P + "ddlDeliveryMethod") if v]
    if only_methods:
        unknown = set(only_methods) - set(methods)
        if unknown:
            raise ScrapeError(f"Unknown delivery method(s) {sorted(unknown)}; available: {methods}")
        methods = [m for m in methods if m in only_methods]
    print(f"  delivery methods: {', '.join(methods)}")

    cache_dir.mkdir(parents=True, exist_ok=True)
    for method in methods:
        html = scrape_method(fetcher, term, method)
        (cache_dir / f"{key}_{method}.html").write_text(html, encoding="utf-8")
        yield method, html


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
WANTED_COLUMNS = {            # key -> header text prefix (lower case)
    "code": "course-section",
    "title": "title",
    "credits": "credits",
    "schedule": "schedule",
    "class_type": "class type",
    "delivery_label": "delivery method",
}
OPTIONAL_COLUMNS = {          # only present when logged in
    "instructor": "instructor",
    "seats": "seats open",
    "status": "status",
}


def clean(text):
    return re.sub(r"\s+", " ", text).strip()


def column_map(table):
    """Find column positions from the header row so a column reorder doesn't
    silently corrupt data."""
    for tr in table.select("thead tr"):
        heads = [clean(th.get_text(" ", strip=True)).lower() for th in tr.find_all("th")]
        if not any(h.startswith("course-section") for h in heads):
            continue
        cols = {}
        for key, prefix in {**WANTED_COLUMNS, **OPTIONAL_COLUMNS}.items():
            for i, h in enumerate(heads):
                if h.startswith(prefix):
                    cols[key] = i
                    break
        if all(k in cols for k in WANTED_COLUMNS):
            return cols
    raise ScrapeError("Could not locate the expected table columns; page layout changed.")


def parse_seats(text):
    """'30 ∕ 30' (U+2215 slash) -> (30, 30). Open can be negative (overfilled)."""
    m = re.search(r"(-?\d+)\s*[∕/]\s*(\d+)", text)
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def parse_instructors(td):
    names = [clean(li.get_text(" ", strip=True)) for li in td.find_all("li")]
    names = [n for n in names if n and n.lower() not in {"staff", "tba"}]
    return "; ".join(names) or None


def to_minutes(hhmm, ampm):
    h, m = map(int, hhmm.split(":"))
    return (h % 12 + (12 if ampm == "PM" else 0)) * 60 + m


def parse_times(start, start_ampm, end, ampm):
    """AM/PM follows the end time and, on some rows, the start time too. When
    the start's is missing, infer it by taking whichever reading gives the
    shorter positive duration. 00:00-00:00 (online placeholder) -> None."""
    e = to_minutes(end, ampm)
    options = []
    for suffix in ((start_ampm,) if start_ampm else ("AM", "PM")):
        s = to_minutes(start, suffix)
        if e - s > 0:
            options.append((e - s, s))
    if not options:
        return None
    return min(options)[1], e


def iso_date(text):
    try:
        return dt.datetime.strptime(text, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def parse_meetings(td, code):
    meetings = []
    for li in td.select("ul.schedules li"):
        head = clean(li.find(string=True, recursive=False) or "").upper()
        dates, room = [], ""
        for div in li.find_all("div"):
            text = clean(div.get_text(strip=True))
            found = re.findall(r"\d\d/\d\d/\d{4}", text)
            if found:
                dates = found
            elif text:
                room = text
        m = re.match(
            r"([MTWRFSU]*)\s*(\d{1,2}:\d{2})\s*([AP]M)?\s*-\s*(\d{1,2}:\d{2})\s*([AP]M)", head)
        if m:
            times = parse_times(m.group(2), m.group(3), m.group(4), m.group(5))
        else:
            times = None
            warn(f"{code}: unrecognized schedule text {head!r}")
        meetings.append({
            "days": m.group(1) if m else "",
            "start_min": times[0] if times else None,
            "end_min": times[1] if times else None,
            "start_date": iso_date(dates[0]) if dates else None,
            "end_date": iso_date(dates[1]) if len(dates) > 1 else None,
            "room": room,
        })
    return meetings


def parse_page(html, delivery):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("#tableCourses")
    if table is None:
        return []
    cols = column_map(table)
    need = max(cols.values()) + 1
    if "instructor" not in cols:
        warn("no Instructor/Seats/Status columns: not logged in (cookie expired?) "
             "or this is a public-page save")
    rows = []
    for tr in table.select("tbody tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < need:
            warn(f"skipping short row with {len(tds)} cells")
            continue
        code = clean(tds[cols["code"]].get_text(" ", strip=True))
        # Subject may be 2-5 letters, and 5-letter ones have no space ('ACCTG100-01').
        m = re.match(r"^(.+?)\s*(\d\w*)-(\w+)$", code)
        if not m:
            warn(f"unparsed course code {code!r}")
            continue
        credit_nums = re.findall(r"\d+(?:\.\d+)?", tds[cols["credits"]].get_text())
        seats_open, seats_total = (parse_seats(tds[cols["seats"]].get_text(" ", strip=True))
                                   if "seats" in cols else (None, None))
        rows.append({
            "instructor": parse_instructors(tds[cols["instructor"]]) if "instructor" in cols else None,
            "seats_open": seats_open,
            "seats_total": seats_total,
            "status": clean(tds[cols["status"]].get_text(" ", strip=True)) if "status" in cols else None,
            "code": code,
            "subject": m.group(1),
            "number": m.group(2),
            "section": m.group(3),                       # '01', 'A1', 'B2', ...
            "title": clean(tds[cols["title"]].get_text(" ", strip=True)),
            "credits": float(credit_nums[0]) if credit_nums else None,
            "credits_max": float(credit_nums[-1]) if credit_nums else None,
            "class_type": clean(tds[cols["class_type"]].get_text(" ", strip=True)),
            "delivery": delivery,                        # filter code that returned it
            "delivery_label": clean(tds[cols["delivery_label"]].get_text(" ", strip=True)),
            "meetings": parse_meetings(tds[cols["schedule"]], code),
        })
    return rows


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS sections (
  term TEXT NOT NULL,
  code TEXT NOT NULL,                 -- 'CSE 310-01', 'CSE 310-A1'
  subject TEXT, number TEXT, section TEXT,
  title TEXT,
  credits REAL, credits_max REAL,
  class_type TEXT,                    -- DAY, ONLN, ...
  delivery TEXT,                      -- P O B X M V (search filter that returned it)
  delivery_label TEXT,                -- 'In-Person', 'Online', ...
  instructor TEXT,                    -- 'Last, First M; ...' (logged-in scrapes only)
  seats_open INTEGER, seats_total INTEGER,  -- seats_open < 0 = overfilled
  status TEXT,                        -- Open, Closed, Reopened, Waitlist Only
  scraped_at TEXT,
  PRIMARY KEY (term, code)
);
CREATE INDEX IF NOT EXISTS sections_by_course ON sections(term, subject, number);
CREATE TABLE IF NOT EXISTS meetings (
  term TEXT NOT NULL,
  code TEXT NOT NULL,
  days TEXT,                          -- 'MWF', 'TR'; '' when none
  start_min INTEGER, end_min INTEGER, -- minutes after midnight; NULL if no set time
  start_date TEXT, end_date TEXT,     -- ISO dates
  room TEXT,
  FOREIGN KEY (term, code) REFERENCES sections(term, code)
);
CREATE INDEX IF NOT EXISTS meetings_by_section ON meetings(term, code);
"""


def migrate(db):
    have = {r[1] for r in db.execute("PRAGMA table_info(sections)")}
    for col, decl in (("instructor", "TEXT"), ("seats_open", "INTEGER"),
                      ("seats_total", "INTEGER"), ("status", "TEXT")):
        if col not in have:
            db.execute(f"ALTER TABLE sections ADD COLUMN {col} {decl}")


def save(db, term, rows, force=False):
    previous = db.execute("SELECT COUNT(*) FROM sections WHERE term=?", (term,)).fetchone()[0]
    # A public (logged-out) scrape has no instructors; keep ones learned earlier.
    known = dict(db.execute(
        "SELECT code, instructor FROM sections WHERE term=? AND instructor IS NOT NULL", (term,)))
    for r in rows:
        if r["instructor"] is None:
            r["instructor"] = known.get(r["code"])
    if previous and len(rows) < 0.8 * previous and not force:
        raise ScrapeError(
            f"Only {len(rows)} sections scraped vs {previous} already stored for {term}; "
            "refusing to overwrite (use --force if that's expected)."
        )
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with db:                                         # one transaction per term
        db.execute("DELETE FROM meetings WHERE term=?", (term,))
        db.execute("DELETE FROM sections WHERE term=?", (term,))
        for r in rows:
            db.execute(
                """INSERT INTO sections (term, code, subject, number, section, title,
                       credits, credits_max, class_type, delivery, delivery_label,
                       instructor, seats_open, seats_total, status, scraped_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (term, r["code"], r["subject"], r["number"], r["section"], r["title"],
                 r["credits"], r["credits_max"], r["class_type"], r["delivery"],
                 r["delivery_label"], r["instructor"], r["seats_open"], r["seats_total"],
                 r["status"], now),
            )
            db.executemany(
                """INSERT INTO meetings (term, code, days, start_min, end_min,
                       start_date, end_date, room) VALUES (?,?,?,?,?,?,?,?)""",
                [(term, r["code"], x["days"], x["start_min"], x["end_min"],
                  x["start_date"], x["end_date"], x["room"]) for x in r["meetings"]],
            )


def export_json(db, path):
    db.row_factory = sqlite3.Row
    try:
        sections = {}
        for r in db.execute("SELECT * FROM sections ORDER BY term, subject, number, section"):
            d = dict(r)
            d["meetings"] = []
            sections[(r["term"], r["code"])] = d
        for m in db.execute("SELECT * FROM meetings ORDER BY term, code, rowid"):
            sections[(m["term"], m["code"])]["meetings"].append(
                {k: m[k] for k in ("days", "start_min", "end_min",
                                   "start_date", "end_date", "room")}
            )
    finally:
        db.row_factory = None
    professors = {}
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='professors'").fetchone():
        for name, rid, rname, dept, rating, diff, wta, n, match in db.execute(
                """SELECT name, rmp_id, rmp_name, department, avg_rating, avg_difficulty,
                          would_take_again, num_ratings, match
                   FROM professors WHERE rmp_id IS NOT NULL"""):
            professors[name] = {"rmp_id": rid, "rmp_name": rname, "department": dept,
                                "rating": rating, "difficulty": diff,
                                "would_take_again": wta, "num_ratings": n, "match": match}
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "sections": list(sections.values()),
        "professors": professors,
    }
    pathlib.Path(path).write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"wrote {len(sections)} sections to {path}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def parse_args():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--term", action="append",
                    help=f'term value, quoted, e.g. "{DEFAULT_TERM}" (repeatable)')
    ap.add_argument("--contact", help="your email; goes in the User-Agent (required online)")
    ap.add_argument("--delay", type=float, default=MIN_DELAY,
                    help=f"seconds between requests (minimum {MIN_DELAY})")
    ap.add_argument("--cookie-file",
                    help="your logged-in cookies: a browser cookies.txt export (Netscape format) "
                         "or a file holding the raw Cookie header value; adds instructor, "
                         "seats and status. Cookies expire, so re-export when it stops working")
    ap.add_argument("--methods", nargs="+", metavar="CODE",
                    help="only scrape these delivery methods, e.g. --methods P O "
                         "(a partial run is refused by the 80%% safeguard unless --force "
                         "or --db points at a fresh file)")
    ap.add_argument("--db", default="byui.db")
    ap.add_argument("--json", default="sections.json", help="output path; '' to skip")
    ap.add_argument("--cache", default="cache", help="where raw result pages are saved")
    ap.add_argument("--offline", action="store_true",
                    help="re-parse pages already in --cache; makes no requests")
    ap.add_argument("--list-terms", action="store_true", help="print available terms and exit")
    ap.add_argument("--force", action="store_true",
                    help="overwrite a term even if far fewer sections came back")
    args = ap.parse_args()
    if not args.offline and not args.contact:
        ap.error("--contact is required (it identifies you to the site in the User-Agent)")
    return args


def main():
    args = parse_args()
    delay = args.delay
    if delay < MIN_DELAY:
        print(f"note: raising delay to {MIN_DELAY}s (byui.edu robots.txt Crawl-delay)")
        delay = MIN_DELAY
    cache = pathlib.Path(args.cache)
    cookies = load_cookies(args.cookie_file) if args.cookie_file else None
    fetcher = None if args.offline else Fetcher(args.contact, delay, cookies)

    if args.list_terms:
        if fetcher is None:
            sys.exit("--list-terms needs network access (drop --offline)")
        soup = BeautifulSoup(fetcher.get(), "html.parser")
        for value, label in select_options(soup, P + "ddlTermSearch"):
            if value:
                print(f"{value!r:14} {label}")
        return

    db = sqlite3.connect(args.db)
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    migrate(db)

    for term in args.term or [DEFAULT_TERM]:
        print(f"== {term}")
        rows = {}
        for method, html in pages_for_term(fetcher, term, cache, args.offline, args.methods):
            parsed = parse_page(html, method)
            print(f"  {method}: {len(parsed)} sections")
            if not parsed:
                warn(f"{method}: no sections found")
            for r in parsed:
                if r["code"] in rows:
                    warn(f"{r['code']} appears under {rows[r['code']]['delivery']} and "
                         f"{method}; keeping the first")
                else:
                    rows[r["code"]] = r
        save(db, term, list(rows.values()), args.force)
        print(f"  saved {len(rows)} sections")

    if args.json:
        export_json(db, args.json)


if __name__ == "__main__":
    try:
        main()
    except ScrapeError as e:
        sys.exit(f"error: {e}")