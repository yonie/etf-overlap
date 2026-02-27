# ETF Overlap Analyzer - Project Notes

## Deployment

**Previously**: Ran under PM2 (removed with `pm2 delete etf-overlap`)

**Now**: Runs in Docker container at `0.0.0.0:3003`

```bash
docker compose up -d     # Start
docker compose down      # Stop
docker compose logs -f   # Logs
docker compose up -d --build  # Rebuild after code changes
```

## Security Hardening (Feb 2026)

- Removed password authentication (was `AUTH_PASSWORD` in `.env`)
- Added security headers: CSP, HSTS, X-Frame-Options, X-Content-Type-Options
- Rate limiting: 200/day, 50/hour default; 10/min for analyze endpoint
- Input limits: Max 10 ISINs per request, 10KB body size
- Subprocess timeout: 60 seconds
- Request logging with timestamps
- Container security: non-root user, dropped capabilities

## ISIN Normalization (Feb 2026)

### Problem
Stocks traded on multiple exchanges have different ISINs, causing overlap analysis to under-report:
- TSMC: `TW0002330008` (Taiwan) vs `US8740391003` (ADR)
- Novartis: `CH0388253459` (Swiss) vs `US66987V1098` (ADR)
- Shell: `GB00BP6MXH84` (UK) vs `US8552441094` (ADR)

### Solution
`isin_normalizer.py` resolves dual-listed stocks using:

1. **OpenFIGI API** - Maps ISINs to Bloomberg's Financial Instrument Global Identifier (FIGI)
2. **Company name matching** - Falls back when `shareClassFIGI` differs (ADR vs underlying)
3. **SQLite cache** - Avoids repeated API calls (`data/isin_mapping.db`)

### Usage
```python
from isin_normalizer import ISINNormalizer

normalizer = ISINNormalizer()
canonical_id = normalizer.get_canonical_id('TW0002330008')  # Returns same ID as US8740391003
```

### CLI Options
```bash
# With normalization (default)
python etf_overlap.py --multi IE00B4L5Y983,IE00B3RBWM25

# Disable normalization (for comparison)
python etf_overlap.py --multi IE00B4L5Y983,IE00B3RBWM25 --no-normalize
```

### Environment Variables
- `OPENFIGI_API_KEY` - Optional, higher rate limits (25 per 6 sec vs 25/min)
- `OPENFIGI_CACHE_DB` - Custom cache path (default: `data/isin_mapping.db`)
- `ISIN_CACHE_EXPIRY_DAYS` - Cache TTL (default: 30 days)

### Output Changes
- Holdings now include `canonical_id` field
- Common holdings show `merged_isins` when multiple ISINs were combined
- Stocks with merged ISINs display with `*` indicator in reports

### Rate Limits (OpenFIGI)
| | Without API Key | With API Key |
|---|---|---|
| Requests | 25/minute | 25 per 6 seconds |
| Batch size | 5 ISINs | 100 ISINs |

Get free API key at: https://www.openfigi.com/api

## Configuration

Environment variables in `etf_web/.env`:
- `FLASK_ENV` - development/production
- `SECRET_KEY` - Flask secret key
- `MAX_ISINS_PER_REQUEST` - Default: 10
- `RATELIMIT_DEFAULT` - Default: "200 per day;50 per hour"
- `ETF_DATABASE_PATH` - SQLite cache location