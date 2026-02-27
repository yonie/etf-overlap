"""
ETF Overlap Analyzer - Security Configuration

This module provides centralized security configuration for the application.
All security-sensitive settings are loaded from environment variables with
secure defaults for production deployment.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

class Config:
    """Base configuration with secure defaults"""
    
    SECRET_KEY = os.getenv('SECRET_KEY', os.urandom(32).hex())
    
    DEBUG = False
    TESTING = False
    
    HOST = os.getenv('HOST', '127.0.0.1')
    PORT = int(os.getenv('PORT', 3003))
    
    MAX_ISINS_PER_REQUEST = int(os.getenv('MAX_ISINS_PER_REQUEST', 10))
    MAX_REQUEST_SIZE_BYTES = int(os.getenv('MAX_REQUEST_SIZE_BYTES', 10240))
    
    SUBPROCESS_TIMEOUT_SECONDS = int(os.getenv('SUBPROCESS_TIMEOUT_SECONDS', 60))
    
    DATABASE_PATH = os.getenv(
        'DATABASE_PATH',
        str(Path(__file__).parent.parent / 'data' / 'etf_cache.db')
    )
    
    SESSION_TIMEOUT_MINUTES = int(os.getenv('SESSION_TIMEOUT_MINUTES', 30))
    MAX_FAILED_AUTH_ATTEMPTS = int(os.getenv('MAX_FAILED_AUTH_ATTEMPTS', 5))
    AUTH_LOCKOUT_MINUTES = int(os.getenv('AUTH_LOCKOUT_MINUTES', 15))
    
    RATELIMIT_STORAGE_URL = os.getenv('RATELIMIT_STORAGE_URL', 'memory://')
    RATELIMIT_DEFAULT = os.getenv('RATELIMIT_DEFAULT', '200 per day;50 per hour')
    RATELIMIT_ANALYZE = os.getenv('RATELIMIT_ANALYZE', '10 per minute')
    
    HTTPS_ONLY = os.getenv('HTTPS_ONLY', 'true').lower() == 'true'
    HSTS_MAX_AGE = int(os.getenv('HSTS_MAX_AGE', 31536000))
    
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    LOG_FILE = os.getenv('LOG_FILE', None)
    
    CONTENT_SECURITY_POLICY = os.getenv(
        'CONTENT_SECURITY_POLICY',
        "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; frame-ancestors 'none';"
    )
    
    @classmethod
    def init_app(cls, app):
        """Initialize application with this configuration"""
        pass


class DevelopmentConfig(Config):
    """Development configuration - more permissive but still secure"""
    
    DEBUG = True
    HTTPS_ONLY = False
    
    DATABASE_PATH = os.getenv(
        'DATABASE_PATH',
        str(Path(__file__).parent.parent / 'etf_cache.db')
    )
    
    @classmethod
    def init_app(cls, app):
        Config.init_app(app)
        import logging
        logging.getLogger('werkzeug').setLevel(logging.DEBUG)


class ProductionConfig(Config):
    """Production configuration - strictest security settings"""
    
    HTTPS_ONLY = True
    
    @classmethod
    def init_app(cls, app):
        Config.init_app(app)
        
        import logging
        from logging.handlers import RotatingFileHandler
        
        if cls.LOG_FILE:
            file_handler = RotatingFileHandler(
                cls.LOG_FILE,
                maxBytes=10485760,
                backupCount=10
            )
            file_handler.setFormatter(logging.Formatter(
                '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
            ))
            file_handler.setLevel(getattr(logging, cls.LOG_LEVEL))
            app.logger.addHandler(file_handler)
        
        app.logger.setLevel(getattr(logging, cls.LOG_LEVEL))


class TestingConfig(Config):
    """Testing configuration"""
    
    TESTING = True
    DATABASE_PATH = ':memory:'
    RATELIMIT_STORAGE_URL = 'memory://'
    HTTPS_ONLY = False


config_by_name = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}


def get_config():
    """Get configuration based on FLASK_ENV environment variable"""
    env = os.getenv('FLASK_ENV', 'development')
    return config_by_name.get(env, DevelopmentConfig)