"""
ETF Overlap Analyzer - Web Interface

SECURITY FEATURES:
- Security headers (CSP, HSTS, X-Frame-Options, etc.)
- Rate limiting to prevent abuse
- Input validation with strict limits
- Subprocess execution with timeout and validated inputs
- Structured audit logging
- Request size limits
- No authentication required - relies on network-level security

DEPLOYMENT NOTES:
- Use behind a reverse proxy (Nginx/Apache) for production
- Ensure HTTPS is enabled at the reverse proxy level
- Configure firewall to restrict access as needed
- Use Gunicorn/uWSGI instead of Flask dev server for production
"""

import logging
import logging.handlers
import os
import re
import subprocess
import json
import time
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory, Response, g
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

app.config['DEBUG'] = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
app.config['TESTING'] = os.getenv('FLASK_TESTING', 'false').lower() == 'true'
app.config['MAX_CONTENT_LENGTH'] = int(os.getenv('MAX_REQUEST_SIZE_BYTES', 10240))
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', os.urandom(32).hex())

from config import get_config
config = get_config()
config.init_app(app)

logging.basicConfig(
    level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO')),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('etf_analyzer')

log_file_path = os.getenv('LOG_FILE')
if log_file_path:
    file_handler = logging.handlers.RotatingFileHandler(
        log_file_path,
        maxBytes=10485760,
        backupCount=10
    )
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    ))
    logger.addHandler(file_handler)

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=[os.getenv('RATELIMIT_DEFAULT', '200 per day;50 per hour')],
    storage_uri=os.getenv('RATELIMIT_STORAGE_URL', 'memory://')
)

ISIN_PATTERN = re.compile(r'^[A-Z]{2}[A-Z0-9]{9}[0-9]$')
MAX_ISINS_PER_REQUEST = int(os.getenv('MAX_ISINS_PER_REQUEST', 10))
SUBPROCESS_TIMEOUT = int(os.getenv('SUBPROCESS_TIMEOUT_SECONDS', 60))


@app.before_request
def log_request():
    """Log incoming requests for audit purposes"""
    g.request_start_time = time.time()
    logger.info(
        "Request started",
        extra={
            'method': request.method,
            'path': request.path,
            'remote_addr': request.remote_addr,
            'user_agent': request.headers.get('User-Agent', 'Unknown')[:100]
        }
    )


@app.after_request
def add_security_headers(response):
    """Add security headers to all responses"""
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
    
    if os.getenv('HTTPS_ONLY', 'true').lower() == 'true':
        response.headers['Strict-Transport-Security'] = f'max-age={os.getenv("HSTS_MAX_AGE", 31536000)}; includeSubDomains'
    
    csp = os.getenv(
        'CONTENT_SECURITY_POLICY',
        "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; frame-ancestors 'none';"
    )
    response.headers['Content-Security-Policy'] = csp
    
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, private'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    
    return response


@app.after_request
def log_response(response):
    """Log response for audit purposes"""
    duration = time.time() - g.get('request_start_time', time.time())
    logger.info(
        "Request completed",
        extra={
            'method': request.method,
            'path': request.path,
            'remote_addr': request.remote_addr,
            'status_code': response.status_code,
            'duration_ms': round(duration * 1000, 2)
        }
    )
    return response


@app.errorhandler(413)
def request_entity_too_large(error):
    """Handle request too large error"""
    logger.warning(f"Request entity too large from {request.remote_addr}")
    return jsonify({'error': 'Request too large. Maximum size is 10KB.'}), 413


@app.errorhandler(429)
def ratelimit_handler(error):
    """Handle rate limit exceeded"""
    logger.warning(f"Rate limit exceeded for {request.remote_addr}")
    return jsonify({'error': 'Rate limit exceeded. Please try again later.'}), 429


@app.errorhandler(500)
def internal_error(error):
    """Handle internal server errors"""
    logger.error(f"Internal server error: {error}")
    return jsonify({'error': 'Internal server error'}), 500


@app.errorhandler(Exception)
def handle_exception(error):
    """Handle unhandled exceptions"""
    logger.exception(f"Unhandled exception: {error}")
    return jsonify({'error': 'An unexpected error occurred'}), 500


def validate_isin(isin: str) -> bool:
    """
    Validate ISIN format to prevent injection attacks.
    ISIN format: 2 letter country code + 9 alphanumeric + 1 check digit
    Example: IE00B4L5Y983
    """
    if not isinstance(isin, str):
        return False
    
    isin = isin.strip().upper()
    
    if not ISIN_PATTERN.match(isin):
        return False
    
    return True


@app.route('/')
@limiter.limit("60 per minute")
def index():
    """Serve the main application page"""
    return send_from_directory('templates', 'index.html')


@app.route('/api/analyze', methods=['POST'])
@limiter.limit(os.getenv('RATELIMIT_ANALYZE', '10 per minute'))
def analyze():
    """Analyze ETF overlap"""
    try:
        if not request.is_json:
            logger.warning(f"Non-JSON request from {request.remote_addr}")
            return jsonify({'error': 'Content-Type must be application/json'}), 400
        
        data = request.get_json()
        if data is None:
            return jsonify({'error': 'Invalid JSON payload'}), 400
        
        if not data or 'isins' not in data:
            return jsonify({'error': 'Invalid request - missing isins parameter'}), 400

        isins = data.get('isins', [])
        
        if not isinstance(isins, list):
            return jsonify({'error': 'isins must be an array'}), 400
        
        if len(isins) < 2:
            return jsonify({'error': 'At least 2 ETF ISINs required'}), 400
        
        if len(isins) > MAX_ISINS_PER_REQUEST:
            return jsonify({
                'error': f'Too many ISINs. Maximum is {MAX_ISINS_PER_REQUEST}'
            }), 400
        
        invalid_isins = []
        validated_isins = []
        
        for isin in isins:
            if not isinstance(isin, str):
                invalid_isins.append(str(isin))
                continue
            
            isin_cleaned = isin.strip().upper()
            if validate_isin(isin_cleaned):
                validated_isins.append(isin_cleaned)
            else:
                invalid_isins.append(isin)
        
        if invalid_isins:
            logger.warning(
                f"Invalid ISINs provided by {request.remote_addr}: {invalid_isins}"
            )
            return jsonify({
                'error': f'Invalid ISIN format detected. ISINs must be exactly 12 characters '
                         f'(2 letters + 9 alphanumeric + 1 digit). Invalid: {", ".join(invalid_isins[:3])}'
                         + ('...' if len(invalid_isins) > 3 else '')
            }), 400
        
        if len(validated_isins) < 2:
            return jsonify({'error': 'At least 2 valid ETF ISINs required'}), 400

        logger.info(
            f"Processing analysis request from {request.remote_addr} for "
            f"{len(validated_isins)} ISINs"
        )

        script_dir = Path(__file__).parent.parent
        script_path = script_dir / 'etf_overlap.py'
        
        if not script_path.exists():
            logger.error(f"Script not found: {script_path}")
            return jsonify({'error': 'Analysis script not found'}), 500

        result = subprocess.run(
            ['python', str(script_path), '--multi', ','.join(validated_isins)],
            capture_output=True,
            text=True,
            cwd=str(script_dir),
            timeout=SUBPROCESS_TIMEOUT
        )

        try:
            json_data = json.loads(result.stdout)
            response = {'data': json_data}

            if result.stderr.strip():
                logger.warning(f"Analysis warnings: {result.stderr.strip()[:500]}")
                response['warnings'] = result.stderr.strip().split('\n')[:3]

            logger.info(
                f"Analysis completed successfully for {request.remote_addr}"
            )
            return jsonify(response)
        except json.JSONDecodeError:
            if result.returncode != 0:
                error_msg = result.stderr or 'Analysis failed'
                logger.error(f"Analysis failed: {error_msg[:500]}")
                return jsonify({'error': 'Analysis failed', 'details': error_msg[:200]}), 500
            else:
                logger.error("Invalid JSON output from analysis tool")
                return jsonify({'error': 'Invalid JSON output from analysis tool'}), 500

    except subprocess.TimeoutExpired:
        logger.error(f"Analysis timeout for {request.remote_addr}")
        return jsonify({'error': 'Analysis timed out. Please try with fewer ETFs.'}), 504
    except Exception as e:
        logger.exception(f"Unexpected error in analyze: {e}")
        return jsonify({'error': 'An unexpected error occurred'}), 500


@app.route('/health')
def health_check():
    """Health check endpoint for load balancers"""
    return jsonify({'status': 'healthy', 'timestamp': datetime.utcnow().isoformat()})


if __name__ == '__main__':
    host = os.getenv('HOST', '127.0.0.1')
    port = int(os.getenv('PORT', 3003))
    debug = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
    
    logger.info(f"Starting ETF Overlap Analyzer on {host}:{port}")
    logger.info(f"Debug mode: {debug}")
    logger.info(f"Max ISINs per request: {MAX_ISINS_PER_REQUEST}")
    
    app.run(
        host=host,
        port=port,
        debug=debug,
        threaded=True
    )