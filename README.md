# scraper-glv

Scrapes Rural District > Taluk > Village > Survey Number > Sub-divisions from the
Tamil Nilam GI Viewer (tngis.tn.gov.in) into one Excel workbook per village.

Output: `out/Village_map_of_<District>_<Taluk>_<Village>_<lat>_<lon>.xlsx`
with the columns `surveynumber | sub-divisions`. `-` means the server reported
no sub-divisions. lat/lon is the centre of the village boundary as the site's
map shows it.

## Setup

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium        # or /usr/bin/google-chrome is used if present
cp accounts.example.json accounts.json       # fill in real site accounts (never committed)
```

## Run

```bash
.venv/bin/python scrape.py                                  # everything
.venv/bin/python scrape.py --district Tiruvallur            # one district
.venv/bin/python scrape.py --district X --taluk Y --village Z
.venv/bin/python scrape.py --worker 1/3                     # one of 3 parallel workers
```

Under pm2 (3 workers, logs in `logs/`):

```bash
pm2 start ecosystem.config.js
pm2 logs land-scrape-0
```

The run is resumable: every fetched row is saved to the workbook immediately,
so restarting continues from the first blank row. The only other state is
`out/.progress/account_idx[.k]` (which account each worker is on).

## Captcha

Login captchas are read by a Qwen3-VL HTTP API fleet (the `vlm-api` service).

| env | meaning |
| --- | --- |
| `CAPTCHA_API_URLS` | comma-separated base URLs, e.g. `http://host:8091,http://host:8093`. `/v1/describe/base64` is appended. |
| `CAPTCHA_API_TOKEN` | bearer token, or |
| `CAPTCHA_API_TOKEN_FILE` | file holding `VLM_API_TOKENS=...` (the API's own `.env`) |
| `CAPTCHA_LEN` | expected length, default 6; other lengths are discarded and the captcha refreshed |
| `CAPTCHA_PROMPT` | prompt sent with the image |

Worker k starts at URL k and rotates per attempt; if one API is down the others
are tried. When `CAPTCHA_API_URLS` is empty a local Ollama vision model is used
(`OLLAMA_URL`, `CAPTCHA_MODEL`).

## Server limits handled

* 50 check-areg calls per account per hour: the script switches account and
  remembers when each one frees up.
* Burst detector (HTTP 429 "unusual request pattern"): waits 60 s and slows down.
* Per-network login lockout ("Too many failed login attempts from this
  network. Try again in 4hr 12min."): parsed and slept out.

See the docstring at the top of `scrape.py` for the full behaviour.
