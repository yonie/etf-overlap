#!/usr/bin/env python3
"""
ISIN Normalizer Module

Normalizes stock ISINs to canonical identifiers using OpenFIGI API.
This resolves the issue where the same company has different ISINs
across different listings (e.g., TSMC: TW0002330008 vs US8740391003).

The canonical ID is determined by:
1. shareClassFIGI from OpenFIGI (works for same-class dual listings)
2. Normalized company name matching (works for ADRs vs underlying shares)

USAGE:
    from isin_normalizer import ISINNormalizer
    
    normalizer = ISINNormalizer()
    canonical_id = normalizer.get_canonical_id('TW0002330008')
    canonical_id = normalizer.get_canonical_id('US8740391003')
    # Both return the same canonical ID for TSMC

ENVIRONMENT VARIABLES:
    OPENFIGI_API_KEY - Optional API key for higher rate limits
    OPENFIGI_CACHE_DB - Custom cache database path (default: data/isin_mapping.db)

RATE LIMITS (OpenFIGI):
    Without API key: 25 requests/min, max 5 ISINs per batch
    With API key: 25 requests per 6 seconds, max 100 ISINs per batch

For more info: https://www.openfigi.com/api
"""

import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from difflib import SequenceMatcher

import requests

logging.basicConfig(
    level=getattr(logging, os.getenv('ETF_LOG_LEVEL', 'INFO').upper()),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('isin_normalizer')

DEFAULT_CACHE_DB = str(Path(__file__).parent / 'data' / 'isin_mapping.db')
OPENFIGI_API_URL = 'https://api.openfigi.com/v3/mapping'
CACHE_EXPIRY_DAYS = int(os.getenv('ISIN_CACHE_EXPIRY_DAYS', '30'))

# Known company name suffixes to strip for matching
COMPANY_SUFFIXES = [
    r'\s*-\s*SP\s*ADR$', r'\s*SPON\s*ADR$', r'\s*SPONSORED\s*ADR$', r'\s*ADR$',
    r'\s*GDR$', r'\s*CDR$', r'\s*ADS$',
    r'\s*PLC$', r'\s*LTD$', r'\s*LIMITED$', r'\s*INC$', r'\s*CORP$', r'\s*CORPORATION$',
    r'\s*CO$', r'\s*SA$', r'\s*AG$', r'\s*NV$', r'\s*B\.V\.$',
    r'\s*S\.A\.$', r'\s*S\.R\.L\.$', r'\s*PTE\.$', r'\s*PTY\.$',
]

# Precompiled pattern for suffix stripping
SUFFIX_PATTERN = re.compile('|'.join(COMPANY_SUFFIXES), re.IGNORECASE)


def normalize_company_name(name: str) -> str:
    """
    Normalize company name for matching.
    
    Strips common suffixes and standardizes format.
    """
    if not name:
        return ""
    
    # Convert to uppercase and strip whitespace
    normalized = name.upper().strip()
    
    # Remove common suffixes
    normalized = SUFFIX_PATTERN.sub('', normalized)
    
    # Remove trailing whitespace and punctuation
    normalized = re.sub(r'[\s\.\,\-]+$', '', normalized)
    normalized = re.sub(r'^[\s\.\,\-]+', '', normalized)
    
    # Standardize whitespace
    normalized = ' '.join(normalized.split())
    
    return normalized


def companies_match(name1: str, name2: str, threshold: float = 0.85) -> bool:
    """
    Check if two company names refer to the same company.
    
    Uses both exact matching after normalization and fuzzy matching.
    """
    n1 = normalize_company_name(name1)
    n2 = normalize_company_name(name2)
    
    if not n1 or not n2:
        return False
    
    # Exact match after normalization
    if n1 == n2:
        return True
    
    # Check if one name contains the other (handles abbreviations)
    if n1 in n2 or n2 in n1:
        return True
    
    # Fuzzy match for minor variations
    ratio = SequenceMatcher(None, n1, n2).ratio()
    return ratio >= threshold


class ISINNormalizer:
    """
    Normalizes ISINs to canonical identifiers using OpenFIGI API.
    
    Uses shareClassFIGI as the canonical identifier because it uniquely 
    identifies a share class regardless of listing exchange.
    
    Example:
        >>> normalizer = ISINNormalizer()
        >>> normalizer.get_canonical_id('TW0002330008')  # TSMC Taiwan
        'BBG001S6Q004'
        >>> normalizer.get_canonical_id('US8740391003')  # TSMC ADR
        'BBG001S6Q004'  # Same canonical ID!
    """
    
    def __init__(self, api_key: str = None, cache_db: str = None):
        """
        Initialize the ISIN normalizer.
        
        Args:
            api_key: Optional OpenFIGI API key for higher rate limits
            cache_db: Path to SQLite cache database
        """
        self.api_key = api_key or os.getenv('OPENFIGI_API_KEY')
        self.cache_db = cache_db or os.getenv('OPENFIGI_CACHE_DB', DEFAULT_CACHE_DB)
        self.session = requests.Session()
        self._init_cache()
        self._last_request_time = 0
        self._min_request_interval = 2.4 if self.api_key else 2.4  # ~25 per minute
        
    def _init_cache(self):
        """Initialize the SQLite cache database."""
        db_path = Path(self.cache_db)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        
        self.conn = sqlite3.connect(self.cache_db)
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS isin_mapping (
                isin TEXT PRIMARY KEY,
                share_class_figi TEXT,
                company_name TEXT,
                ticker TEXT,
                exchange_code TEXT,
                security_type TEXT,
                cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_share_class_figi 
            ON isin_mapping(share_class_figi)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_company_name 
            ON isin_mapping(company_name)
        ''')
        self.conn.commit()
        logger.debug(f"ISIN mapping cache initialized at {self.cache_db}")
    
    def get_canonical_id(self, isin: str) -> str:
        """
        Get the canonical identifier for an ISIN.
        
        Uses shareClassFIGI when available, falls back to normalized company name
        for ADRs and depositary receipts that represent the same underlying company.
        
        Args:
            isin: The ISIN to normalize (e.g., 'TW0002330008')
            
        Returns:
            The canonical ID for grouping, or original ISIN if unavailable
        """
        if not isin or not isinstance(isin, str):
            return isin
            
        isin = isin.strip().upper()
        
        # Check cache first
        cached = self._get_cached_mapping(isin)
        if cached and cached.get('share_class_figi'):
            # Check if there's a company-wide canonical ID for this shareClassFIGI
            company_canonical = self._get_company_canonical(cached['share_class_figi'], cached.get('company_name'))
            if company_canonical:
                return company_canonical
            return cached['share_class_figi']
        elif cached and cached.get('company_name'):
            # Have company name but no shareClassFIGI - try name-based matching
            company_canonical = self._find_canonical_by_company_name(cached['company_name'])
            if company_canonical:
                return company_canonical
            # No previous company match, create one from normalized name
            normalized_name = normalize_company_name(cached['company_name'])
            if normalized_name:
                return f"NAME:{normalized_name[:30]}"
            return isin
        
        # Fetch from OpenFIGI
        mapping = self._fetch_single_from_openfigi(isin)
        if mapping:
            share_class_figi = mapping.get('share_class_figi')
            company_name = mapping.get('company_name')
            
            if share_class_figi:
                # Check if there's a company-wide canonical ID
                company_canonical = self._get_company_canonical(share_class_figi, company_name)
                if company_canonical:
                    return company_canonical
                return share_class_figi
            elif company_name:
                # No shareClassFIGI but have company name
                company_canonical = self._find_canonical_by_company_name(company_name)
                if company_canonical:
                    return company_canonical
                normalized_name = normalize_company_name(company_name)
                if normalized_name:
                    return f"NAME:{normalized_name[:30]}"
        
        # Cache the fact that this ISIN has no mapping
        self._cache_mapping(isin, None)
        return isin
    
    def _get_company_canonical(self, share_class_figi: str, company_name: str = None) -> Optional[str]:
        """
        Check if there's already a canonical ID established for this company.
        
        This handles the ADR case: if we've seen another ISIN with a matching
        company name but different shareClassFIGI, we group them together.
        """
        if not company_name:
            return None
        
        # Look for other ISINs with the same company name
        existing_canonical = self._find_canonical_by_company_name(company_name)
        if existing_canonical:
            return existing_canonical
        
        # No existing canonical found, but we have shareClassFIGI
        # Check if there's an alias for this shareClassFIGI
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT canonical_id FROM figi_aliases 
            WHERE figi = ?
        ''', (share_class_figi,))
        row = cursor.fetchone()
        if row:
            return row[0]
        
        return None
    
    def _find_canonical_by_company_name(self, company_name: str) -> Optional[str]:
        """
        Find canonical ID by matching normalized company names.
        
        This allows ADRs and underlying shares to be grouped together.
        """
        if not company_name:
            return None
            
        normalized = normalize_company_name(company_name)
        if not normalized:
            return None
        
        cursor = self.conn.cursor()
        
        # First check the company_canonicals table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS company_canonicals (
                normalized_name TEXT PRIMARY KEY,
                canonical_id TEXT
            )
        ''')
        self.conn.commit()
        
        cursor.execute('''
            SELECT canonical_id FROM company_canonicals 
            WHERE normalized_name = ?
        ''', (normalized,))
        row = cursor.fetchone()
        if row:
            return row[0]
        
        # Look through all cached ISINs for matching company names
        cursor.execute('SELECT isin, share_class_figi, company_name FROM isin_mapping WHERE company_name IS NOT NULL')
        rows = cursor.fetchall()
        
        for row in rows:
            cached_name = row[2]
            if cached_name and companies_match(company_name, cached_name):
                other_figi = row[1]
                if other_figi:
                    # Store this mapping for future lookups
                    self._store_company_canonical(normalized, other_figi)
                    return other_figi
        
        return None
    
    def _store_company_canonical(self, normalized_name: str, canonical_id: str):
        """Store a company name to canonical ID mapping."""
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS company_canonicals (
                normalized_name TEXT PRIMARY KEY,
                canonical_id TEXT
            )
        ''')
        cursor.execute('''
            INSERT OR REPLACE INTO company_canonicals (normalized_name, canonical_id)
            VALUES (?, ?)
        ''', (normalized_name, canonical_id))
        self.conn.commit()
    
    def _fetch_single_from_openfigi(self, isin: str) -> Optional[Dict]:
        """Fetch a single ISIN mapping from OpenFIGI."""
        mappings = self._fetch_from_openfigi([isin])
        return mappings.get(isin)
    
    def get_canonical_ids_batch(self, isins: List[str]) -> Dict[str, str]:
        """
        Get canonical identifiers for multiple ISINs efficiently.
        
        OpenFIGI supports batch requests (up to 5 without API key, 
        100 with API key), making this more efficient than individual calls.
        
        Args:
            isins: List of ISINs to normalize
            
        Returns:
            Dict mapping original ISIN to canonical ID
        """
        result = {}
        to_fetch = []
        
        # Check cache for all ISINs first
        for isin in isins:
            if not isin or not isinstance(isin, str):
                result[isin] = isin
                continue
                
            isin_clean = isin.strip().upper()
            cached = self._get_cached_mapping(isin_clean)
            if cached:
                result[isin_clean] = cached['share_class_figi'] or isin_clean
            else:
                to_fetch.append(isin_clean)
        
        # Batch fetch from OpenFIGI
        if to_fetch:
            batch_size = 100 if self.api_key else 5
            
            for i in range(0, len(to_fetch), batch_size):
                batch = to_fetch[i:i + batch_size]
                mappings = self._fetch_from_openfigi(batch)
                
                for isin in batch:
                    if isin in mappings:
                        result[isin] = mappings[isin]['share_class_figi']
                    else:
                        # Cache miss - store original ISIN as fallback
                        self._cache_mapping(isin, None)
                        result[isin] = isin
        
        return result
    
    def _get_cached_mapping(self, isin: str) -> Optional[Dict]:
        """Get cached mapping if not expired."""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT share_class_figi, company_name, ticker, exchange_code, security_type, cached_at
            FROM isin_mapping WHERE isin = ?
        ''', (isin,))
        row = cursor.fetchone()
        
        if not row:
            return None
        
        # Check if expired
        cached_at = datetime.fromisoformat(row[5])
        if datetime.now() - cached_at > timedelta(days=CACHE_EXPIRY_DAYS):
            logger.debug(f"Cache expired for ISIN: {isin}")
            return None
        
        return {
            'share_class_figi': row[0],
            'company_name': row[1],
            'ticker': row[2],
            'exchange_code': row[3],
            'security_type': row[4]
        }
    
    def _cache_mapping(self, isin: str, mapping: Optional[Dict]):
        """Cache an ISIN mapping."""
        cursor = self.conn.cursor()
        if mapping:
            cursor.execute('''
                INSERT OR REPLACE INTO isin_mapping 
                (isin, share_class_figi, company_name, ticker, exchange_code, security_type, cached_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                isin,
                mapping.get('share_class_figi'),
                mapping.get('company_name'),
                mapping.get('ticker'),
                mapping.get('exchange_code'),
                mapping.get('security_type'),
                datetime.now().isoformat()
            ))
        else:
            # Store a record with NULL figi to indicate "no mapping available"
            cursor.execute('''
                INSERT OR REPLACE INTO isin_mapping (isin, share_class_figi, cached_at)
                VALUES (?, NULL, ?)
            ''', (isin, datetime.now().isoformat()))
        
        self.conn.commit()
    
    def _fetch_from_openfigi(self, isins: List[str]) -> Dict[str, Dict]:
        """
        Fetch mappings from OpenFIGI API.
        
        Args:
            isins: List of ISINs to look up (max 5 without API key, 100 with)
            
        Returns:
            Dict mapping ISIN to mapping info
        """
        if not isins:
            return {}
        
        # Rate limiting
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_request_interval:
            time.sleep(self._min_request_interval - elapsed)
        
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['X-OPENFIGI-APIKEY'] = self.api_key
        
        # Build request body
        jobs = [{"idType": "ID_ISIN", "idValue": isin} for isin in isins]
        
        try:
            logger.debug(f"Fetching {len(isins)} ISINs from OpenFIGI")
            response = self.session.post(
                OPENFIGI_API_URL,
                headers=headers,
                json=jobs,
                timeout=15
            )
            self._last_request_time = time.time()
            
            if response.status_code == 429:
                logger.warning("OpenFIGI rate limit exceeded")
                return {}
            
            if response.status_code != 200:
                logger.warning(f"OpenFIGI returned status {response.status_code}")
                return {}
            
            results = response.json()
            mappings = {}
            
            for i, result in enumerate(results):
                isin = isins[i]
                if 'data' in result and result['data']:
                    # Get the first matching security
                    security = result['data'][0]
                    mapping = {
                        'share_class_figi': security.get('shareClassFIGI'),
                        'company_name': security.get('name'),
                        'ticker': security.get('ticker'),
                        'exchange_code': security.get('exchCode'),
                        'security_type': security.get('securityType')
                    }
                    if mapping['share_class_figi']:
                        mappings[isin] = mapping
                        self._cache_mapping(isin, mapping)
                        logger.debug(f"Mapped {isin} -> {mapping['share_class_figi']}")
                else:
                    logger.debug(f"No mapping found for {isin}")
            
            return mappings
            
        except requests.Timeout:
            logger.warning("OpenFIGI request timed out")
            return {}
        except requests.RequestException as e:
            logger.warning(f"OpenFIGI request failed: {e}")
            return {}
        except Exception as e:
            logger.error(f"Unexpected error fetching from OpenFIGI: {e}")
            return {}
    
    def get_isins_for_canonical(self, canonical_id: str) -> List[str]:
        """
        Get all ISINs that map to the same canonical ID.
        
        Useful for showing users why certain stocks were merged.
        
        Args:
            canonical_id: The shareClassFIGI to look up
            
        Returns:
            List of ISINs with the same canonical ID
        """
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT isin FROM isin_mapping 
            WHERE share_class_figi = ?
        ''', (canonical_id,))
        return [row[0] for row in cursor.fetchall()]
    
    def get_mapping_info(self, isin: str) -> Optional[Dict]:
        """
        Get full mapping information for an ISIN.
        
        Args:
            isin: The ISIN to look up
            
        Returns:
            Dict with mapping info or None
        """
        return self._get_cached_mapping(isin)
    
    def close(self):
        """Close the database connection."""
        if self.conn:
            self.conn.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
    
    def clear_expired_cache(self):
        """Remove expired entries from cache."""
        cursor = self.conn.cursor()
        cutoff = datetime.now() - timedelta(days=CACHE_EXPIRY_DAYS)
        cursor.execute('''
            DELETE FROM isin_mapping 
            WHERE cached_at < ?
        ''', (cutoff.isoformat(),))
        deleted = cursor.rowcount
        self.conn.commit()
        logger.info(f"Cleared {deleted} expired cache entries")
        return deleted


def normalize_holdings(holdings: List[Dict], normalizer: ISINNormalizer = None) -> List[Dict]:
    """
    Add canonical_id to holdings list.
    
    Args:
        holdings: List of holding dicts with 'isin' key
        normalizer: ISINNormalizer instance (creates new one if None)
        
    Returns:
        Holdings with 'canonical_id' field added
    """
    if normalizer is None:
        normalizer = ISINNormalizer()
    
    # Get all ISINs
    isins = [h.get('isin') for h in holdings if h.get('isin')]
    
    # Batch lookup
    canonical_map = normalizer.get_canonical_ids_batch(isins)
    
    # Add canonical_id to each holding
    for holding in holdings:
        isin = holding.get('isin')
        if isin:
            holding['canonical_id'] = canonical_map.get(isin, isin)
    
    return holdings


if __name__ == '__main__':
    # Demo/test
    import argparse
    
    parser = argparse.ArgumentParser(description='ISIN Normalizer')
    parser.add_argument('isins', nargs='+', help='ISINs to normalize')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose output')
    parser.add_argument('--clear-cache', action='store_true', help='Clear expired cache')
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger('isin_normalizer').setLevel(logging.DEBUG)
    
    with ISINNormalizer() as normalizer:
        if args.clear_cache:
            normalizer.clear_expired_cache()
        
        print(f"\nNormalizing {len(args.isins)} ISINs...\n")
        
        for isin in args.isins:
            canonical = normalizer.get_canonical_id(isin)
            info = normalizer.get_mapping_info(isin)
            
            print(f"ISIN: {isin}")
            print(f"  Canonical ID: {canonical}")
            if info:
                print(f"  Company: {info.get('company_name', 'N/A')}")
                print(f"  Ticker: {info.get('ticker', 'N/A')}")
                print(f"  Exchange: {info.get('exchange_code', 'N/A')}")
            print()