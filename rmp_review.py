#!/usr/bin/env python3
"""
rmp_review.py - interactively resolve suspicious RateMyProfessors matches.

Finds instructors in the `professors` table (filled by rmp.py) that share one
RMP profile - usually a wrong match, e.g. two different BYU-I teachers pointed
at the same RMP person - and walks you through fixing them. For each group it
shows the RMP profile (with a link) next to what each BYU-I instructor teaches,
so you can compare departments.

Per instructor you can:
  k  keep      the match is right (same person, e.g. name written two ways)
  n  none      this instructor has no RMP profile
  u  url       paste the correct RMP profile URL (or just its number)
  f  find      search RMP by name and pick from the results
  s  skip      decide later

Everything you decide is saved with match='manual', so rmp.py never overwrites
it and it won't be shown again.

Usage:
    python rmp_review.py                       # review duplicates
    python rmp_review.py --list                # just print them, change nothing
    python rmp_review.py --include-approximate # also review first-name/initial-only matches
    python rmp_review.py --unmatched           # also go through instructors RMP search missed
    python rmp_review.py --fix Lines           # fix one instructor (any part of the name)
"""
import argparse
import re
import sqlite3
import sys

import byui_scraper as scraper
import rmp

PROFILE = "https://www.ratemyprofessors.com/professor/{}"


def duplicate_groups(db):
    """{rmp_id: [professor names]} for ids shared by 2+ rows, at least one not yet reviewed."""
    rows = db.execute("""
        SELECT rmp_id FROM professors WHERE rmp_id IS NOT NULL
        GROUP BY rmp_id HAVING COUNT(*) > 1 AND SUM(match <> 'manual') > 0
        ORDER BY rmp_id""").fetchall()
    return {rid: [n for (n,) in db.execute(
        "SELECT name FROM professors WHERE rmp_id=? ORDER BY name", (rid,))] for (rid,) in rows}


def approximate_names(db):
    return [n for (n,) in db.execute(
        "SELECT name FROM professors WHERE rmp_id IS NOT NULL AND match IN ('first-name','initial') "
        "ORDER BY name")]


def teaching(db, name):
    """Short 'ACCTG 100, ACCTG 210, ...' summary of what an instructor teaches."""
    courses = sorted({f"{s} {n}" for s, n in db.execute(
        "SELECT subject, number FROM sections WHERE instr('; ' || instructor || ';', ?) > 0",
        (f"; {name};",))})
    if not courses:
        return "(no sections found)"
    shown = ", ".join(courses[:8])
    return shown + (f" â€¦ (+{len(courses) - 8} more)" if len(courses) > 8 else "")


def describe(p):
    """One line for a professors row dict."""
    if not p["rmp_id"]:
        return "no RMP profile"
    return (f"{p['rmp_name']} - {p['department']} - {p['avg_rating']}/5, "
            f"{p['num_ratings']} ratings [{p['match']}]\n      {PROFILE.format(p['rmp_id'])}")


def get_row(db, name):
    db.row_factory = sqlite3.Row
    try:
        r = db.execute("SELECT * FROM professors WHERE name=?", (name,)).fetchone()
    finally:
        db.row_factory = None
    return dict(r) if r else None


def read(prompt):
    try:
        return input(prompt)
    except EOFError:
        return ""


def ask(prompt, choices):
    while True:
        try:
            answer = input(prompt).strip()
        except EOFError:
            return "q"
        if not answer:
            continue
        if answer[0].lower() in choices:
            return answer[0].lower()
        print(f"  choose one of: {', '.join(choices)}")


def profile_id(text):
    m = re.search(r"professor/(\d+)", text) or re.fullmatch(r"\s*(\d+)\s*", text)
    return int(m.group(1)) if m else None


def choose_by_url(client, db, name):
    text = read("  RMP profile URL or id (blank = cancel): ").strip()
    if not text:
        return False
    pid = profile_id(text)
    if pid is None:
        print("  couldn't find a profile number in that")
        return False
    cand = client.get_teacher(pid)
    if cand is None:
        print(f"  no RMP teacher with id {pid}")
        return False
    print(f"  -> {cand['firstName']} {cand['lastName']} ({cand['department']}), "
          f"{cand['avgRating']}/5, {cand['numRatings']} ratings")
    school = cand.get("school") or {}
    if school.get("id") not in rmp.SCHOOL_IDS:
        print(f"  WARNING: this profile is at {school.get('name', 'another school')}, not BYU-Idaho")
    if ask("  use this? [y/n] ", "yn") == "y":
        rmp.save_professor(db, rmp.professor_row(name, cand, "manual"))
        return True
    return False


def choose_by_search(client, db, name):
    last, firsts = rmp.split_name(name)
    default = f"{firsts[0]} {last}" if firsts else last
    text = read(f"  search text [{default}]: ").strip() or default
    results = client.search(text)
    if not results:
        print("  no results")
        return False
    for i, c in enumerate(results, 1):
        print(f"  {i:2}. {c['firstName']} {c['lastName']} - {c['department']} - "
              f"{c['avgRating']}/5, {c['numRatings']} ratings\n      {PROFILE.format(c['legacyId'])}")
    pick = read("  number to use (blank = cancel): ").strip()
    if not pick.isdigit() or not 1 <= int(pick) <= len(results):
        return False
    rmp.save_professor(db, rmp.professor_row(name, results[int(pick) - 1], "manual"))
    return True


def resolve(db, client, name):
    """Prompt for one instructor. Returns False if the user wants to quit."""
    print(f"\n  {name}\n    teaches: {teaching(db, name)}\n    current: {describe(get_row(db, name))}")
    while True:
        a = ask("    [k]eep  [n]one  [u]rl  [f]ind  [s]kip  [q]uit > ", "knufsq")
        try:
            if a == "k":
                with db:
                    db.execute("UPDATE professors SET match='manual' WHERE name=?", (name,))
                return True
            if a == "n":
                rmp.save_professor(db, rmp.professor_row(name, None, "manual"))
                return True
            if a == "u" and choose_by_url(client, db, name):
                return True
            if a == "f" and choose_by_search(client, db, name):
                return True
        except RuntimeError as e:
            print(f"  RMP error: {e}")
        if a == "s":
            return True
        if a == "q":
            return False


def review_group(db, client, header, names, force=False):
    print(f"\n{'=' * 72}\n{header}")
    for n in names:
        print(f"  - {n}: teaches {teaching(db, n)}")
    todo = [n for n in names if force or get_row(db, n)["match"] != "manual"]
    a = ask("[a]ccept all as the same person  [r]eview one by one  [s]kip  [q]uit > ", "arsq")
    if a == "q":
        return False
    if a == "a":
        with db:
            db.executemany("UPDATE professors SET match='manual' WHERE name=?", [(n,) for n in todo])
    elif a == "r":
        for n in todo:
            if not resolve(db, client, n):
                return False
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="byui.db")
    ap.add_argument("--json", default="sections.json", help="re-export here when done; '' to skip")
    ap.add_argument("--list", action="store_true", help="print the problems and exit")
    ap.add_argument("--include-approximate", action="store_true",
                    help="also review matches made on first-name prefix or initial only")
    ap.add_argument("--unmatched", action="store_true",
                    help="review instructors with no RMP match (not just duplicates)")
    ap.add_argument("--fix", metavar="NAME", action="append",
                    help="fix specific instructor(s): any part of the name, e.g. --fix Lines "
                         "(repeatable); skips the duplicate scan")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between RMP requests")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.executescript(rmp.SCHEMA)
    if args.fix:
        groups = []
        for text in args.fix:
            found = [n for (n,) in db.execute(
                "SELECT name FROM professors WHERE name LIKE ? ORDER BY name", (f"%{text}%",))]
            if not found:
                print(f"No instructor matching {text!r} in the professors table "
                      "(run rmp.py first, or check the spelling).")
            groups += [(f"Fixing {n}:", [n]) for n in found]
    else:
        groups = [(f"RMP profile {PROFILE.format(rid)} is shared by {len(names)} instructors:", names)
                  for rid, names in duplicate_groups(db).items()]
        if args.include_approximate:
            seen = {n for _, names in groups for n in names}
            groups += [(f"Approximate match for {n}:", [n])
                       for n in approximate_names(db) if n not in seen]
        if args.unmatched:
            groups += [(f"No RMP match for {n}:", [n]) for (n,) in db.execute(
                "SELECT name FROM professors WHERE match='none' ORDER BY name")]
    if not groups:
        print("Nothing to review.")
        return

    if args.list:
        for header, names in groups:
            print(f"\n{header}")
            for n in names:
                print(f"  - {n}: teaches {teaching(db, n)}")
        print(f"\n{len(groups)} group(s)")
        return

    client = rmp.Rmp(args.delay)
    print(f"{len(groups)} group(s) to review.")
    for i, (header, names) in enumerate(groups, 1):
        if not review_group(db, client, f"[{i}/{len(groups)}] {header}", names, force=bool(args.fix)):
            break
    if args.json:
        scraper.export_json(db, args.json)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\ninterrupted (decisions so far are saved)")
