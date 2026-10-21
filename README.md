# BYU-Idaho Class Scheduler

Scrapes the BYU-Idaho class schedule, adds RateMyProfessors ratings, and serves a local web page for browsing sections and building a schedule.

## Setup

```
pip install requests beautifulsoup4
```

## Usage

1. Scrape a term into `byui.db` and `sections.json`:

   ```
   python byui_scraper.py --contact you@example.com --term "2027;WI"
   ```

   Requests are spaced 10 seconds apart to respect the site's robots.txt crawl delay.

   Instructor, seats and status only appear when logged in. Pass `--cookie-file cookies.txt` (exported from your browser while logged in to student.byui.edu), or save the logged-in pages and run with `--offline`. See the docstring in `byui_scraper.py` for details.

2. Add RateMyProfessors ratings (uses an unofficial endpoint, which may break):

   ```
   python rmp.py
   ```

3. Optionally fix wrong professor matches interactively:

   ```
   python rmp_review.py
   ```

4. Start the frontend at http://localhost:8000:

   ```
   python serve.py
   ```

## Files

| File | Purpose |
| --- | --- |
| `byui_scraper.py` | Scrapes sections into SQLite and `sections.json` |
| `rmp.py` | Looks up instructor ratings on RateMyProfessors |
| `rmp_review.py` | Interactive review of suspicious RMP matches |
| `serve.py` | Local web server for `index.html` |
| `index.html` | Scheduler frontend; loads `sections.json` |

## Not committed

`byui.db`, `sections.json`, the page caches and cookie files are git-ignored. Generate them with the steps above. Never commit your login cookies.
