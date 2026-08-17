# Signal Radar

An independent GitHub Pages site for the overseas edition of FuncDance's technology radar.

## What it does

- Pulls from official overseas AI and developer RSS/Atom feeds.
- Keeps only links, headlines, timestamps and short feed summaries.
- Filters political, violence and sexual-content keywords before publication.
- Updates at 08:00 Asia/Shanghai every Monday, Wednesday and Friday, with a manual GitHub Actions trigger available.
- Keeps a dated archive and excludes links already published in earlier editions.

## Publish to GitHub Pages

1. Create an empty GitHub repository, then push this directory as that repository's root.
2. In the repository, open **Settings -> Pages** and set **Source** to **GitHub Actions**.
3. Open **Actions -> Update and deploy Signal Radar** and run it once with **Run workflow**.

The workflow fetches the sources, commits `data/radar.json`, and deploys the static site. GitHub Actions scheduled jobs can be delayed during high load; the manual trigger remains available for an immediate refresh.
