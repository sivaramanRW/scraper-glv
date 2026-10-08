#!/usr/bin/env bash
# One-time setup after `git clone`: venv, browser, config files, latest progress.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
if [ ! -x /usr/bin/google-chrome ]; then
  .venv/bin/playwright install chromium   # add --with-deps (needs sudo) if Chromium fails to start
fi
git pull -q --rebase origin main || echo "!! git pull failed (no network / auth?) - continuing with the local progress"
if [ ! -f accounts.json ]; then
  cp accounts.example.json accounts.json
  echo "!! accounts.json created from the example: put the real site accounts in it"
fi
if [ ! -f .env ]; then
  cp .env.example .env
  echo "!! .env created from the example: set CAPTCHA_API_URLS and CAPTCHA_API_TOKEN"
fi
command -v pm2 >/dev/null || echo "!! pm2 not found: npm install -g pm2"
echo "setup done. start with:  pm2 start ecosystem.config.js && pm2 save"
