# TN GIS APIs used by the scraper

Request and response shapes observed while building `tngis_scrape.py`. Anything I did not see directly is marked.

## Public APIs (no login)

Base: `https://tngis.tn.gov.in/apps/generic_api/v2`. Plain GET calls with the header `X-APP-NAME: demo`.

### 1. Districts

```
GET /admin_master_district?request_type=district
→ {"success":1,"message":"District Details Found","count":38,
   "data":[{"district_code":"13","district_lgd_code":"…","district_english_name":"Dindigul","district_tamil_name":"…"}]}
```

### 2. Taluks

```
GET /admin_master_taluk?district_code=14&request_type=taluk
→ {"success":1,"message":"Taluk Details Found","count":4,
   "data":[{"taluk_code":"01","taluk_lgd_code":"5787","taluk_english_name":"Karur","taluk_tamil_name":"…"}]}
```

### 3. Villages

```
GET /admin_master_village?district_code=14&taluk_code=01&request_type=revenue_village
→ {"success":1,"message":"Revenue Village Details Found","count":21,
   "data":[{"village_code":"45B","village_lgd_code":"…","village_english_name":"Veliannai (North)","village_tamil_name":"…"}]}
```

### 4. Survey numbers

```
GET /admin_master_survey_number?district_code=14&taluk_code=01&revenue_village_code=45B
    &area_type=rural&data_type=cadastral&request_type=survey_number
→ {"success":1,"message":"Survey Number Details Found","count":1113,
   "data":[{"survey_number":"1"},{"survey_number":"10"}, …]}
```

## Secure APIs (login needed)

Base: `https://tngis.tn.gov.in/apps/gi_viewer_api/gi_mvc/api/v1`

### 5. Session key

```
GET /session-key        (cookie: access_token)
→ {"sessionKey":"…","sessionId":"…","csrfToken":"…","expiresIn":600}
```

### The encrypted envelope

Used by every POST below except refresh.

- Request body: `{"payload": "<base64>"}`. The base64 decodes to
  `{"ciphertext":"<base64>","iv":"<hex>","salt":"<hex>","iterations":1500}`.
- Key: PBKDF2-HMAC-SHA512 over `sessionKey`, using the `salt`, 1500 iterations, 32 bytes.
  Cipher: AES-256-CBC with PKCS7 padding.
- Headers: `X-Secure-Request: true`, `X-Session-ID`, `X-CSRF-Token`, `Origin`, `Referer`,
  plus the `access_token` cookie.
- The response uses the same `{"payload": …}` envelope, decrypted with the same `sessionKey`.
  The JSON shown below is the decrypted content.

### 6. Captcha: `POST /auth/captcha`

```
{"timestamp": 1791670000000}
→ {"captchaId":"54a906bc-…","image":"data:image/png;base64,iVBOR…"}
```

### 7. Login: `POST /auth/login`

```
{"identifier":"<email or mobile>","password":"…","captchaId":"<id>",
 "captchaHash":"<sha256 hex of captchaText + captchaId>","loginType":"public","timestamp":…}
→ {"status":1, …}                                   success; cookies access_token + refresh_token are set
→ {"status":2,"mfaRequired":true,"mfaToken":"…"}    OTP needed
→ {"message":"…"} or {"error":"…"}                   failure
```

Not confirmed: every field of a success body. Only `status` and the cookies were seen.

### 8. OTP step: `POST /auth/login/verify-otp`

```
{"mfaToken":"…","otp":"123456","timestamp":…}  →  {"status":1, …}
```

### 9. Refresh token: `POST /auth/refresh`

```
body: {}   (plain JSON, not encrypted; cookie: refresh_token)
→ HTTP 200 + Set-Cookie: access_token (and possibly a rotated refresh_token)
→ HTTP 401 if the refresh token is expired or revoked
```

Not confirmed: the response body. Only the status code and cookies were checked.

### 10. Sub-divisions: `POST /land/check-areg`

The main call of the scraper.

```
{"district_code":"14","taluk_code":"01","village_code":"45B","survey_number":"104",
 "sub_division_number":"jjj","area_type":"rural"}
→ {"success":2,"message":"Sub division Details Found","count":9,
   "data":[{"subdiv_no":"1"},{"subdiv_no":"2"}, …],
   "rate_limit_status":{"limit":50,"count":10,"remaining":40,"window_hours":1}}
```

`sub_division_number: "jjj"` is a dummy value; the server then lists the real sub-divisions.

Failure replies:

```
{"success":0,"message":"Unusual request pattern detected across land data services. Please slow down and try again shortly."}
{"success":0,"rate_limited":true,"message":"Request limit exhausted. Try again after 2h 23m",
 "rate_limit_status":{"limit":50,"count":50,"remaining":0,"window_hours":1,"retry_after_seconds":8595}}
{"error":"Invalid or expired token"}
```

Not confirmed: the raw body of the rate-limited reply. The message and `rate_limit_status` came from the script's log, and the script checks the `rate_limited` flag.

## Tested but not used by the scraper

### 11. Geometry: `POST /land/get-geom`

`case` can be `district`, `taluk`, `revenue_village`, `survey_number` or `sub_division_number`.

```
{"case":"survey_number","code_type":"revenue","district_code":"17","taluk_code":"08",
 "village_code":"271","survey_number":"1"}
→ {"success":1,"message":"Geometry Data Retrieved Successfully",
   "data":{"type":"FeatureCollection","features":[{"type":"Feature",
     "geometry":{"type":"Polygon","coordinates":[[[79.289,11.236],…]]},
     "properties":{"district_code":17,"taluk_code":8,"village_code":"271","survey_number":"1",
                   "centroid_longitude":79.289,"centroid_latitude":11.236}}]},
   "rate_limit_status":{"limit":500,"count":210,"remaining":290,"window_hours":24}}
```

## Limits seen

| API | Limit |
|---|---|
| `check-areg` | 50 calls per rolling hour. Reaching 50 locked the account for about 2h 23m. |
| `get-geom` | 500 calls per 24 hours |
| public lists | none found |

The `check-areg` window is rolling: each call frees its slot exactly 60 minutes after it was made.
