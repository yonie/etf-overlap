# ETF Overlap Analyzer

**Analyze overlap between ETFs to identify concentration risks and improve portfolio diversification.**

![Screenshot of ETF Overlap Analysis](screenshot.png)

## Security Features

This tool includes comprehensive security hardening for production deployment:

- **Input Validation**: Strict ISIN format validation with regex pattern
- **Rate Limiting**: Configurable limits to prevent abuse
- **Security Headers**: CSP, HSTS, X-Frame-Options, X-Content-Type-Options
- **Request Limits**: Max ISINs per request and request size limits
- **Timeout Protection**: Subprocess and HTTP request timeouts
- **Audit Logging**: Structured logging with timestamps
- **Configurable Secrets**: Environment-based configuration

## Quick Start

### Console Tool

```bash
# Install dependencies
pip install requests beautifulsoup4

# Analyze two ETFs
python etf_overlap.py --isin1 IE00B4L5Y983 --isin2 IE00B3RBWM25

# Analyze multiple ETFs
python etf_overlap.py --multi IE00B4L5Y983,IE00B3RBWM25,IE00BK5BQT80

# Get JSON output for integration
python etf_overlap.py --multi IE00B4L5Y983,IE00B3RBWM25 --json

# Expire cache and fetch fresh data
python etf_overlap.py --multi IE00B4L5Y983,IE00B3RBWM25 --expire-cache

# Enable verbose logging
python etf_overlap.py --multi IE00B4L5Y983,IE00B3RBWM25 --verbose
```

### Web Interface

```bash
cd etf_web
pip install -r requirements.txt

# Copy .env.example to .env and configure
cp .env.example .env

# Run with Flask development server
python app.py

# Or run with Gunicorn for production
gunicorn --bind 127.0.0.1:3003 --workers 2 app:app
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `FLASK_ENV` | Environment (development/production/testing) | development |
| `SECRET_KEY` | Flask secret key for sessions | random |
| `HOST` | Server bind address | 127.0.0.1 |
| `PORT` | Server port | 3003 |
| `HTTPS_ONLY` | Enable HTTPS-only headers | true |
| `MAX_ISINS_PER_REQUEST` | Maximum ISINs per analysis request | 10 |
| `MAX_REQUEST_SIZE_BYTES` | Maximum request body size | 10240 |
| `SUBPROCESS_TIMEOUT_SECONDS` | Analysis timeout | 60 |
| `RATELIMIT_DEFAULT` | Default rate limit | 200/day, 50/hour |
| `RATELIMIT_ANALYZE` | Analysis endpoint rate limit | 10/minute |
| `ETF_DATABASE_PATH` | Database file path | ./data/etf_cache.db |
| `ETF_CACHE_EXPIRY_HOURS` | Cache validity period | 24 |
| `LOG_LEVEL` | Logging level | INFO |
| `LOG_FILE` | Log file path (optional) | - |

## Production Deployment

### Security Checklist

- [ ] Set `FLASK_ENV=production`
- [ ] Generate a strong `SECRET_KEY`
- [ ] Set `HTTPS_ONLY=true` (requires reverse proxy with HTTPS)
- [ ] Configure rate limiting for your use case
- [ ] Set up log file rotation
- [ ] Use a process manager (systemd, supervisord)
- [ ] Configure firewall to restrict access
- [ ] Set restrictive file permissions on database directory

### Gunicorn + Nginx Example

**gunicorn.conf.py:**
```python
bind = "127.0.0.1:3003"
workers = 2
threads = 4
timeout = 120
accesslog = "/var/log/etf/access.log"
errorlog = "/var/log/etf/error.log"
loglevel = "info"
```

**Nginx config:**
```nginx
server {
    listen 443 ssl http2;
    server_name your-domain.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://127.0.0.1:3003;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Rate limiting
        limit_req zone=etf_limit burst=20 nodelay;
    }
}

# Rate limiting zone
limit_req_zone $binary_remote_addr zone=etf_limit:10m rate=10r/m;
```

### Systemd Service

```ini
[Unit]
Description=ETF Overlap Analyzer
After=network.target

[Service]
User=etf
Group=etf
WorkingDirectory=/opt/etf-overlap/etf_web
Environment="PATH=/opt/etf-overlap/venv/bin"
EnvironmentFile=/opt/etf-overlap/etf_web/.env
ExecStart=/opt/etf-overlap/venv/bin/gunicorn -c gunicorn.conf.py app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### File Permissions

```bash
# Create dedicated user
sudo useradd -r -s /bin/false etf

# Set ownership
sudo chown -R etf:etf /opt/etf-overlap

# Restrict database directory
chmod 750 /opt/etf-overlap/data
chmod 640 /opt/etf-overlap/data/etf_cache.db

# Protect environment file
chmod 640 /opt/etf-overlap/etf_web/.env
```

## API Documentation

### POST /api/analyze

Analyze ETF overlap for multiple ETFs.

**Request:**
```json
{
  "isins": ["IE00B4L5Y983", "IE00B3RBWM25"]
}
```

**Response:**
```json
{
  "data": {
    "etfs": [...],
    "summary": {
      "total_etfs": 2,
      "average_overlap_percentage": 15.32,
      "total_unique_stocks": 842
    },
    "stock_overlap_analysis": [...]
  }
}
```

### GET /health

Health check endpoint for load balancers.

**Response:**
```json
{
  "status": "healthy",
  "timestamp": "2026-02-27T12:00:00.000000"
}
```

## Security Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        Internet                              │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Reverse Proxy (Nginx)                                       │
│  - HTTPS Termination                                         │
│  - Rate Limiting                                             │
│  - Security Headers                                          │
│  - Access Control (network-level)                            │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Gunicorn (WSGI Server)                                      │
│  - Process Management                                        │
│  - Request Handling                                          │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Flask Application                                           │
│  - Security Headers Middleware                               │
│  - Rate Limiting (Flask-Limiter)                            │
│  - Input Validation                                          │
│  - Request Size Limits                                       │
│  - Audit Logging                                             │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  etf_overlap.py (Subprocess)                                │
│  - ISIN Validation                                           │
│  - Database Caching                                          │
│  - External API Access (justetf.com)                        │
└─────────────────────────────────────────────────────────────┘
```

## Dependencies

### Core
- Python 3.7+
- requests==2.31.0
- beautifulsoup4==4.12.2

### Web Interface
- Flask==2.3.2
- Flask-Limiter==3.5.0
- python-dotenv==1.0.0
- gunicorn==21.2.0

### Development
- pytest==7.4.2
- pytest-cov==4.1.0
- flake8==6.1.0
- bandit==1.7.5
- safety==2.3.5

## Running Security Audits

```bash
# Install dev dependencies
pip install -r requirements-dev.txt

# Run bandit security linter
bandit -r etf_overlap.py etf_web/

# Check for vulnerable dependencies
safety check

# Run flake8
flake8 etf_overlap.py etf_web/
```

## Disclaimers

**NO FINANCIAL ADVICE**: This tool provides data analysis only. It does not provide financial advice or recommendations. Consult a qualified financial advisor before making investment decisions.

**SCRAPING RESPONSIBILITY**: This tool scrapes data from justetf.com. Users are solely responsible for complying with justetf.com's terms of service and applicable laws. Use at your own risk.

**DATA ACCURACY**: Results depend on data availability from justetf.com. Some ETFs may not have holdings information available.

## License

MIT License - See [LICENSE](LICENSE) for details.

---

## Support

If you find this tool useful, consider buying me a coffee! Your support helps keep this project maintained and improving.

[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-ffdd00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/yonie)