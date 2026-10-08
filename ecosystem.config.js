// pm2 start ecosystem.config.js            -> all 3 workers
// pm2 start ecosystem.config.js --only land-scrape-1,land-scrape-2
// pm2 logs land-scrape-0   |   pm2 stop all
// Each worker gets a third of the accounts and a third of the villages (see --worker in scrape.py).
const path = require("path");
const WORKERS = 3;
const worker = (k) => ({
  name: `land-scrape-${k}`,
  script: "scrape.py",
  interpreter: path.join(__dirname, ".venv/bin/python"), // virtualenv with playwright + openpyxl
  interpreter_args: "-u",                                   // unbuffered so pm2 logs show progress live
  cwd: __dirname,
  args: `--worker ${k}/${WORKERS}`,                         // add e.g. " --district Ariyalur" to restrict
  autorestart: true,
  stop_exit_codes: [0],                                     // finished normally -> don't restart
  restart_delay: 30000,
  max_restarts: 1000,                                       // the script resumes itself; pm2 is only the safety net
  min_uptime: "30s",
  env: {
    // Captcha reader: the Qwen3-VL API fleet (~/Documents/vlm-api, pm2 qwen-vlm-api-*). Comma-separated base URLs;
    // worker k starts at URL k and rotates on each attempt, so the 3 scrapers spread over the 3 API workers.
    CAPTCHA_API_URLS: process.env.CAPTCHA_API_URLS || "http://192.168.52.115:8091,http://192.168.52.115:8093,http://192.168.52.115:8094",
    CAPTCHA_API_TOKEN_FILE: "/home/randomwalk/Documents/vlm-api/.env", // bearer token read at start-up (VLM_API_TOKENS=...); or set CAPTCHA_API_TOKEN
    CAPTCHA_LEN: "6",                                       // answers of another length are discarded and the captcha refreshed
    // Fallback only when CAPTCHA_API_URLS is empty: local Ollama vision model(s)
    OLLAMA_URL: "http://localhost:11434",
    CAPTCHA_MODEL: "qwen2.5vl:7b",
    NO_MANUAL_CAPTCHA: "1",                                 // never block on a terminal prompt under pm2
  },
  out_file: `logs/out-${k}.log`,
  error_file: `logs/err-${k}.log`,
  time: true,
});
module.exports = { apps: Array.from({ length: WORKERS }, (_, k) => worker(k)) };
