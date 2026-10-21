#!/usr/bin/env python3
"""
rmp.py - look up each instructor in byui.db on RateMyProfessors and store the
ratings in a `professors` table (then re-export sections.json).

Uses RMP's unofficial GraphQL endpoint (the one its own website calls), so it
can change or break without notice. Be gentle: results are cached and only
instructors that are missing or older than --max-age days are looked up.

Usage:
    python rmp.py
    python rmp.py --refresh        # ignore the cache
    python rmp.py --db byui.db --json sections.json
Manual fixes: edit the `professors` row (set rmp_id etc.) and set match='manual';
manual rows are never overwritten.
"""
import argparse
import base64
import datetime as dt
import re
import sqlite3
import sys
import time
import unicodedata

import requests

import byui_scraper as scraper

URL = "https://www.ratemyprofessors.com/graphql"
SCHOOL_IDS = {                                    # base64 of 'School-<legacyId>'
    "U2Nob29sLTE3NTQ=": "Brigham Young University - Idaho",
    "U2Nob29sLTE4ODYx": "BYUI",
}
QUERY = """query($q: TeacherSearchQuery!) { newSearch { teachers(query: $q, first: 20) {
  edges { node { id legacyId firstName lastName department avgRating avgDifficulty
                 wouldTakeAgainPercent numRatings } } } } }"""
TEACHER_QUERY = """query($id: ID!) { node(id: $id) { ... on Teacher { id legacyId firstName
  lastName department avgRating avgDifficulty wouldTakeAgainPercent numRatings
  school { id name } } } }"""
SCHEMA = """
CREATE TABLE IF NOT EXISTS professors (
  name TEXT PRIMARY KEY,              -- exactly as shown in sections.instructor
  rmp_id INTEGER,                     -- legacy id; page is /professor/<id>
  rmp_name TEXT, department TEXT,
  avg_rating REAL, avg_difficulty REAL, would_take_again REAL, num_ratings INTEGER,
  match TEXT,                         -- exact | first-name | initial | none | manual
  fetched_at TEXT
);
"""


def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z]", "", s)


def split_name(name):
    """'Dunlop, Robin D McWhorter' -> ('Dunlop', ['Robin', 'D', 'McWhorter'])"""
    last, _, rest = name.partition(",")
    return last.strip(), rest.split()


def surname_matches(surname, rmp_last):
    want = norm(surname)
    tokens = {norm(t) for t in re.split(r"[\s\-]+", rmp_last)} | {norm(rmp_last)}
    return len(want) >= 2 and want in tokens


def first_score(want, got):
    want, got = norm(want), norm(got)
    if not want or not got:
        return 0
    if want == got:
        return 3
    if min(len(want), len(got)) >= 3 and (want.startswith(got) or got.startswith(want)):
        return 2            # Rob/Robert, Tate/Tatum
    if len(want) == 1 and want == got[0]:
        return 1            # source only gave an initial
    return 0


def pick(name, candidates):
    """Best candidate for an instructor name, or (None, 'none')."""
    last, firsts = split_name(name)
    # A trailing middle/maiden name may be the surname RMP uses, so try both.
    surnames = [last] + ([firsts[-1]] if len(firsts) > 1 and len(norm(firsts[-1])) >= 3 else [])
    best = None
    for surname in surnames:
        pool = [c for c in candidates if surname_matches(surname, c["lastName"])]
        scored = [(first_score(firsts[0] if firsts else "", c["firstName"]), c["numRatings"], c)
                  for c in pool]
        scored = [t for t in scored if t[0] > 0]
        if not scored:
            continue
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        score, _, cand = scored[0]
        if score == 1 and len(scored) > 1:
            continue        # only an initial to go on and several people share it
        kind = {3: "exact", 2: "first-name", 1: "initial"}[score]
        if best is None or score > best[2]:
            best = (cand, kind, score)
    return (best[0], best[1]) if best else (None, "none")


class Rmp:
    def __init__(self, delay):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": "Basic dGVzdDp0ZXN0",
                               "User-Agent": "Mozilla/5.0 (byui-class-scheduler personal project)"})
        self.delay, self._last = delay, 0.0

    def _post(self, query, variables):
        gap = time.monotonic() - self._last
        if gap < self.delay:
            time.sleep(self.delay - gap)
        for attempt in range(3):
            try:
                r = self.s.post(URL, json={"query": query, "variables": variables}, timeout=30)
                r.raise_for_status()
                data = r.json()
                break
            except (requests.RequestException, ValueError) as e:
                if attempt == 2:
                    raise RuntimeError(f"RMP request failed: {e}") from e
                time.sleep(5 * (attempt + 1))
        self._last = time.monotonic()
        if data.get("errors"):
            raise RuntimeError(f"RMP error: {data['errors'][0].get('message')}")
        return data["data"]

    def search(self, text):
        out = []
        for school in SCHOOL_IDS:
            data = self._post(QUERY, {"q": {"text": text, "schoolID": school}})
            out += [e["node"] for e in data["newSearch"]["teachers"]["edges"]]
        return out

    def get_teacher(self, legacy_id):
        """Teacher record for an RMP profile id (the number in /professor/<id>), or None."""
        gid = base64.b64encode(f"Teacher-{legacy_id}".encode()).decode()
        data = self._post(TEACHER_QUERY, {"id": gid})
        node = data.get("node")
        return node if node and node.get("legacyId") else None

    def lookup(self, name):
        last, firsts = split_name(name)
        queries = [f"{firsts[0]} {last}" if firsts else last, last]
        if len(firsts) > 1:
            queries.append(f"{firsts[0]} {firsts[-1]}")
        for q in queries:
            cand, kind = pick(name, self.search(q))
            if cand:
                return cand, kind
        return None, "none"


def instructor_names(db):
    names = set()
    for (v,) in db.execute("SELECT DISTINCT instructor FROM sections WHERE instructor IS NOT NULL"):
        names.update(n.strip() for n in v.split(";") if n.strip())
    return sorted(names)


def professor_row(name, cand, kind):
    """Row for the professors table; cand=None records 'no RMP profile'."""
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    if not cand:
        return (name, None, None, None, None, None, None, None, kind, now)
    wta = cand["wouldTakeAgainPercent"]
    return (name, cand["legacyId"], f"{cand['firstName']} {cand['lastName']}",
            cand["department"], cand["avgRating"], cand["avgDifficulty"],
            wta if (wta if wta is not None else -1) >= 0 else None,
            cand["numRatings"], kind, now)


def save_professor(db, row):
    with db:
        db.execute("INSERT OR REPLACE INTO professors VALUES (?,?,?,?,?,?,?,?,?,?)", row)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="byui.db")
    ap.add_argument("--json", default="sections.json", help="re-export here; '' to skip")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between requests")
    ap.add_argument("--max-age", type=int, default=30, help="re-look-up after this many days")
    ap.add_argument("--refresh", action="store_true", help="ignore cached results")
    ap.add_argument("--limit", type=int, help="look up at most this many instructors")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.executescript(SCHEMA)
    names = instructor_names(db)
    if not names:
        sys.exit("No instructors in the database; scrape while logged in first.")
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.max_age)).isoformat()
    cached = dict(db.execute("SELECT name, fetched_at FROM professors"))
    manual = {n for (n,) in db.execute("SELECT name FROM professors WHERE match='manual'")}
    todo = [n for n in names if n not in manual
            and (args.refresh or n not in cached or (cached[n] or "") < cutoff)]
    print(f"{len(names)} instructors, {len(todo)} to look up")
    todo = todo[:args.limit]

    rmp = Rmp(args.delay)
    for i, name in enumerate(todo, 1):
        try:
            cand, kind = rmp.lookup(name)
        except RuntimeError as e:
            print(f"stopping: {e}", file=sys.stderr)
            break
        row = professor_row(name, cand, kind)
        save_professor(db, row)
        print(f"[{i}/{len(todo)}] {name}: "
              + (f"{row[2]} {row[4]}/5 ({row[7]} ratings, {kind})" if cand else "no match"))

    if args.json:
        scraper.export_json(db, args.json)


if __name__ == "__main__":
    main()
