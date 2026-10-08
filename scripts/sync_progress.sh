#!/usr/bin/env bash
# Commit and push the scraping progress (out/) so any other clone resumes from here.
# pm2 runs this every 10 min (land-progress-sync); it is safe to run by hand too.
set -u
cd "$(dirname "$0")/.."
git add -A out
if ! git diff --cached --quiet; then
  git commit -q -m "progress: $(ls out/*.xlsx 2>/dev/null | wc -l) village workbooks ($(hostname) $(date -u +%FT%TZ))" || exit 1
fi
# -X theirs: if the same workbook changed on both sides keep this machine's copy (the machine scraping is authoritative)
if ! git pull -q --rebase -X theirs origin main; then
  echo "pull failed; aborting rebase"; git rebase --abort 2>/dev/null; exit 1
fi
git push -q origin main && echo "synced $(date -u +%FT%TZ)"
