# KYC Portal

A self-hosted identity (KYC) and business (KYB) verification portal for Yativo. Customers fill in a guided, step-by-step form styled like Sumsub. Their data is encrypted at rest and submitted to the Yativo KYC API as soon as it passes validation. Admins review everything in a separate console.

```
/verify?customer_type=individual&customer_id=<id>     # individual KYC
/verify?customer_type=business&customer_id=<id>       # business KYB
/admin                                                # compliance console
```

`type=` and `customerId=` are accepted as aliases. `kyc`/`kyb` and `personal`/`company` also work as types.

## Quick start

```bash
cd kyc-portal
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m app.cli generate-secret   # -> SECRET_KEY
.venv/bin/python -m app.cli generate-key      # -> KYC_ENCRYPTION_KEY  (back it up!)
# set ADMIN_EMAIL / ADMIN_PASSWORD in .env for the first admin
.venv/bin/python run.py                       # http://localhost:8000
```

Production: `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2 --proxy-headers` behind HTTPS. Then set `APP_ENV=production`, `SESSION_HTTPS_ONLY=true`, `ALLOWED_HOSTS`, `YATIVO_KYC_BASE_URL=https://kyc.yativo.com`, and point `DATABASE_URL` at Postgres if you like.

## What it does

| Area | Details |
|---|---|
| Validation | `app/validation.py` mirrors every rule in the Yativo guide (§2–§8). It covers banned name words (a permanent block on Yativo), ages of 18–120, BVN/NIN for Nigeria, strict state and postal-code checks against Yativo's lookups, enum checks, photo-ID front/back rules, ownership ≤ 100%, the required `business_registration` document, the EURDE closure and more. The browser validates each step through the same code. Any Yativo 422 is mapped back to the exact field. |
| Lookups | Every dropdown comes from Yativo's lookup endpoints (countries, subdivisions, ID types, postal formats, occupations, industries, purposes, source of funds, currencies). They are cached for 6 hours, and the last good copy is kept on disk. |
| Files | Any size and any common format is accepted: JPG, PNG, HEIC, WebP, GIF, BMP, TIFF or PDF. `app/files.py` turns each upload into a JPEG or PDF of **120 KB–3 MB**. Big photos are recompressed or downscaled, small ones are upscaled, oversized PDFs are compressed and then re-rendered, and tiny PDFs are re-rendered at a higher DPI. EXIF and GPS metadata is stripped. |
| Encryption | AES-256-GCM. Form data, the Yativo response and every file (and its metadata) are encrypted, each bound to its record. Search by email or name uses keyed blind indexes. Key rotation works via `KYC_ENCRYPTION_OLD_KEYS`. |
| Submission | On final submit the data is re-validated, saved, and immediately sent to Yativo as multipart in Laravel bracket notation. The outcome is mapped: 201 submitted, 422 shown back to the customer, 409 duplicate, 403 blocked, 404 business not provisioned. Network or 5xx failures are retried automatically every 10 minutes, up to 6 attempts. |
| Address autocomplete | Every address block has search-as-you-type that fills street, city, state (matched to Yativo's subdivision code) and postal code, restricted to the selected country. Provider priority: **1** Google Places (New) when `GOOGLE_MAPS_API_KEY` is set (key never reaches the browser); **2** Bing Maps AutoSuggest when `USE_BING_AUTOCOMPLETE=true`; **3** free OpenStreetMap/Photon geocoder as the final fallback. All providers are proxied server-side. |
| Drafts | Progress is saved encrypted on the server. A draft is only restored to the browser that created it, so knowing a customer ID alone reveals nothing. |
| Admin | Submissions list with status tiles, search and filters. Decrypted review screen with a document gallery, Yativo warnings/errors and raw payloads. Actions: resend, reopen for the customer, and permanent delete. Branding page with colour presets plus per-colour pickers, radius, font, logo, copy, live preview, and which accounts are offered. Also a customer-link generator and an audit log of every view and download. |
| Security | CSP, scrypt admin passwords, CSRF on admin forms, login rate limiting, session timeout, rate-limited customer APIs, and optional HMAC-signed customer links (`LINK_SIGNING_SECRET`). |

## CLI

```bash
python -m app.cli create-admin you@company.com
python -m app.cli sign-link individual <customer_id>
python -m app.cli retry-failed
```

## Tests

```bash
.venv/bin/python -m pytest -q
```

## Notes

- Business customers must already exist on Yativo's side (guide §2.7), or Yativo returns 404. The portal keeps the data, shows "account not ready" to the customer, and you can **Resend** from the admin once Yativo has provisioned the customer.
- A 201 means *submitted, pending review*, not approved (§2.12). The customer sees exactly that.
- Customer-facing text never names downstream providers.
