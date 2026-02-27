#!/usr/bin/env python3
"""
ETF Overlap Analysis Tool
Analyzes overlap between ETFs using justetf.com data

SECURITY FEATURES:
- Configurable database path with secure defaults
- Input validation with strict ISIN format checking
- Logging for audit and debugging
- No secrets or sensitive data stored in code
- Users responsible for compliance with justetf.com terms of service

USAGE:
    python etf_overlap.py --isin1 IE00B4L5Y983 --isin2 IE00B3RBWM25
    python etf_overlap.py --multi IE00B4L5Y983,IE00B3RBWM25,IE00BK5BQT80
"""
import sys
import os
import json
import time
import argparse
import logging
import stat
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import re

import requests
from bs4 import BeautifulSoup
import sqlite3

from isin_normalizer import ISINNormalizer, normalize_holdings

DATABASE_FILE = os.getenv(
    'ETF_DATABASE_PATH',
    str(Path(__file__).parent / 'data' / 'etf_cache.db')
)
CACHE_EXPIRY_HOURS = int(os.getenv('ETF_CACHE_EXPIRY_HOURS', '24'))
JUSTETF_URL = 'https://www.justetf.com/en/etf-profile.html?isin={}&tab=analyses'
REQUEST_TIMEOUT = int(os.getenv('ETF_REQUEST_TIMEOUT', '30'))
USER_AGENT = os.getenv(
    'ETF_USER_AGENT',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
)

WEIGHT_CLEAN_PATTERN = re.compile(r'[^\d.]')
ISIN_PATTERN = re.compile(r'^[A-Z]{2}[A-Z0-9]{9}[0-9]$')

logging.basicConfig(
    level=getattr(logging, os.getenv('ETF_LOG_LEVEL', 'INFO').upper()),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('etf_overlap')

def validate_isin(isin: str) -> bool:
    """
    Validate ISIN format to prevent injection attacks.
    ISIN format: 2 letter country code + 9 alphanumeric + 1 check digit
    Example: IE00B4L5Y983
    """
    if not isinstance(isin, str):
        return False
    
    # Remove whitespace and convert to uppercase
    isin = isin.strip().upper()
    
    # Check format
    if not ISIN_PATTERN.match(isin):
        return False
    
    return True

class ETFData:
    """Data structure for ETF holdings"""
    def __init__(self, isin: str, name: str, holdings: List[Dict]):
        self.isin = isin
        self.name = name
        self.holdings = holdings

class OverlapCalculator:
    """Calculates overlap between ETFs"""

    @staticmethod
    def calculate_overlap(etf1: ETFData, etf2: ETFData) -> Dict:
        """Calculate overlap between two ETFs using canonical IDs"""
        common_holdings = []
        total_overlap = 0.0

        # Create canonical ID to holding maps
        # Group holdings by canonical_id to handle dual-listed stocks
        etf1_map = {}
        etf2_map = {}
        
        for h in etf1.holdings:
            key = h.get('canonical_id') or h['isin']
            if key not in etf1_map:
                etf1_map[key] = []
            etf1_map[key].append(h)
        
        for h in etf2.holdings:
            key = h.get('canonical_id') or h['isin']
            if key not in etf2_map:
                etf2_map[key] = []
            etf2_map[key].append(h)

        # Find common holdings by canonical ID
        for canonical_id, holdings1_list in etf1_map.items():
            if canonical_id in etf2_map:
                holdings2_list = etf2_map[canonical_id]
                
                # Get the first holding from each (they represent the same stock)
                holding1 = holdings1_list[0]
                holding2 = holdings2_list[0]
                
                # Sum weights if same stock appears multiple times in same ETF
                weight1 = sum(h['weight'] for h in holdings1_list)
                weight2 = sum(h['weight'] for h in holdings2_list)
                
                min_weight = min(weight1, weight2)
                
                # Collect all ISINs that map to this canonical ID
                all_isins = [h['isin'] for h in holdings1_list + holdings2_list]
                unique_isins = list(set(all_isins))
                
                common_holdings.append({
                    'isin': holding1['isin'],
                    'canonical_id': canonical_id,
                    'name': holding1['name'],
                    'weight': min_weight,
                    'etf1_weight': weight1 / len(holdings1_list) if len(holdings1_list) > 1 else holding1['weight'],
                    'etf2_weight': weight2 / len(holdings2_list) if len(holdings2_list) > 1 else holding2['weight'],
                    'merged_isins': unique_isins if len(unique_isins) > 1 else None
                })
                total_overlap += min_weight

        # Calculate diversification score (0-100)
        score = 100 - total_overlap
        if total_overlap > 20:
            score -= (total_overlap - 20) * 2
        if total_overlap > 50:
            score -= (total_overlap - 50) * 3
        score = max(0, min(100, score))

        return {
            'etf1': etf1,
            'etf2': etf2,
            'common_holdings': common_holdings,
            'total_overlap_percentage': total_overlap,
            'diversification_score': score
        }

    @staticmethod
    def calculate_multi_overlap(etfs: List[ETFData]) -> Dict:
        """Calculate overlap between multiple ETFs"""
        matrix = {}
        total_overlap = 0
        pair_count = 0

        # Initialize matrix
        for etf in etfs:
            matrix[etf.isin] = {}

        # Calculate all pairs
        for i, etf1 in enumerate(etfs):
            for j, etf2 in enumerate(etfs[i+1:], i+1):
                result = OverlapCalculator.calculate_overlap(etf1, etf2)
                matrix[etf1.isin][etf2.isin] = {
                    'common_holdings': result['common_holdings'],
                    'overlap_percentage': result['total_overlap_percentage']
                }
                matrix[etf2.isin][etf1.isin] = {
                    'common_holdings': result['common_holdings'],
                    'overlap_percentage': result['total_overlap_percentage']
                }
                total_overlap += result['total_overlap_percentage']
                pair_count += 1

        avg_overlap = total_overlap / pair_count if pair_count > 0 else 0

        return {
            'etfs': etfs,
            'overlap_matrix': matrix,
            'average_overlap': avg_overlap
        }

class DataCache:
    """Manages caching of ETF data"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DATABASE_FILE
        self._ensure_secure_db_path()
        self._initialize_db()

    def _ensure_secure_db_path(self):
        """Ensure database directory exists with secure permissions"""
        db_path = Path(self.db_path)
        db_dir = db_path.parent
        
        db_dir.mkdir(parents=True, exist_ok=True)
        
        mode = os.stat(db_dir).st_mode
        if mode & stat.S_IRWXO or mode & stat.S_IRWXG:
            logger.warning(
                f"Database directory {db_dir} has permissive permissions. "
                f"Consider running: chmod 750 {db_dir}"
            )

    def _initialize_db(self):
        """Initialize database tables"""
        self.conn = sqlite3.connect(self.db_path)
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS etf_cache (
                isin TEXT PRIMARY KEY,
                name TEXT,
                holdings TEXT,
                fetched_at TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_fetched_at 
            ON etf_cache(fetched_at)
        ''')
        self.conn.commit()
        logger.debug(f"Database initialized at {self.db_path}")

    def get_cached_data(self, isin: str) -> Optional[ETFData]:
        """Get cached ETF data if not expired"""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT name, holdings, fetched_at FROM etf_cache
            WHERE isin = ?
        ''', (isin,))
        result = cursor.fetchone()

        if not result:
            return None

        name, holdings_json, fetched_at_str = result
        fetched_at = datetime.fromisoformat(fetched_at_str)

        # Check if expired
        if datetime.now() - fetched_at < timedelta(hours=CACHE_EXPIRY_HOURS):
            holdings = json.loads(holdings_json)
            return ETFData(isin, name, holdings)

        return None

    def cache_data(self, etf: ETFData):
        """Cache ETF data"""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO etf_cache
            VALUES (?, ?, ?, ?)
        ''', (
            etf.isin,
            etf.name,
            json.dumps(etf.holdings),
            datetime.now().isoformat()
        ))
        self.conn.commit()

    def close(self):
        """Close database connection"""
        self.conn.close()

class DataFetcher:
    """Fetches ETF data from justetf.com"""

    def __init__(self, cache: DataCache, normalizer: ISINNormalizer = None):
        self.cache = cache
        self.normalizer = normalizer
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': USER_AGENT,
            'Accept-Language': 'en-US,en;q=0.9'
        })

    def fetch_etf_data(self, isin: str) -> ETFData:
        """Fetch ETF data with caching"""
        cached = self.cache.get_cached_data(isin)
        if cached:
            logger.debug(f"Cache hit for ISIN: {isin}")
            return cached

        logger.info(f"Fetching data for ISIN: {isin}")
        url = JUSTETF_URL.format(isin)
        
        try:
            response = self.session.get(url, timeout=REQUEST_TIMEOUT)
        except requests.Timeout:
            logger.error(f"Timeout fetching {isin} after {REQUEST_TIMEOUT}s")
            raise Exception(f"Request timeout for {isin}")
        except requests.RequestException as e:
            logger.error(f"Network error fetching {isin}: {e}")
            raise Exception(f"Network error for {isin}: {e}")

        if response.status_code != 200:
            logger.warning(f"HTTP {response.status_code} for {isin}")
            raise Exception(f"Failed to fetch {isin}: HTTP {response.status_code}")

        soup = BeautifulSoup(response.text, 'html.parser')

        # Get ETF name
        name_tag = soup.find('h1', class_='etf-profile__name')
        if not name_tag:
            # Try alternative selector
            name_tag = soup.find('h1')
            if not name_tag:
                raise Exception(f"Could not find ETF name for {isin}")
        name = name_tag.get_text(strip=True)

        # Get holdings table - use the correct selector based on our analysis
        table = soup.find('table', {'data-testid': 'etf-holdings_top-holdings_table'})
        if not table:
            raise Exception(f"ETF {isin} does not provide holdings information on justetf.com. This ETF may not have a holdings tab or the data is not available. Please remove this ISIN from your input.")

        holdings = []
        tbody = table.find('tbody')
        if not tbody:
            tbody = table

        for row in tbody.find_all('tr'):
            cols = row.find_all('td')
            if len(cols) >= 2:
                # Extract stock ISIN from profile link in first column
                stock_link = cols[0].find('a', href=True)
                stock_isin = None
                if stock_link and '/stock-profiles/' in stock_link['href']:
                    # Extract ISIN from href like "/en/stock-profiles/US67066G1040"
                    href_parts = stock_link['href'].split('/')
                    if len(href_parts) > 0:
                        stock_isin = href_parts[-1]  # Last part is the ISIN
                
                # Extract stock name from first column
                stock_name_element = cols[0].find('span')
                stock_name = stock_name_element.get_text(strip=True) if stock_name_element else cols[0].get_text(strip=True)

                # Extract percentage from second column
                percentage_element = cols[1].find('span', {'data-testid': 'tl_etf-holdings_top-holdings_value_percentage'})
                percentage_text = percentage_element.get_text(strip=True) if percentage_element else cols[1].get_text(strip=True)

                # Clean percentage and convert to float (using pre-compiled regex for performance)
                percentage_text = WEIGHT_CLEAN_PATTERN.sub('', percentage_text).strip()
                try:
                    weight = float(percentage_text)
                    # Use ISIN as unique identifier, fallback to stock name if ISIN not available
                    holdings.append({
                        'isin': stock_isin if stock_isin else stock_name,  # Unique identifier
                        'name': stock_name,
                        'weight': weight
                    })
                except ValueError:
                    # Skip if we can't parse the percentage
                    continue

        etf_data = ETFData(isin, name, holdings)
        
        # Normalize ISINs to canonical IDs if normalizer is available
        if self.normalizer:
            normalize_holdings(etf_data.holdings, self.normalizer)
        
        self.cache.cache_data(etf_data)
        return etf_data

class ReportGenerator:
    """Generates formatted reports"""

    @staticmethod
    def generate_text_report(result: Dict) -> str:
        """Generate text report for two ETFs"""
        etf1 = result['etf1']
        etf2 = result['etf2']
        common = result['common_holdings']
        overlap = result['total_overlap_percentage']
        score = result['diversification_score']

        report = []
        report.append('=' * 80)
        report.append('ETF OVERLAP ANALYSIS REPORT'.center(80))
        report.append('=' * 80 + '\n')

        report.append(ReportGenerator._format_etf_info(etf1, 'ETF 1'))
        report.append(ReportGenerator._format_etf_info(etf2, 'ETF 2') + '\n')

        report.append('OVERLAP SUMMARY'.center(80))
        report.append('-' * 80)
        report.append(f"Total Overlap Percentage: {overlap:.2f}%")
        report.append(f"Diversification Score: {score:.1f}/100")
        report.append(f"Number of Common Holdings: {len(common)}\n")

        report.append('HOLDINGS COMPARISON'.center(80))
        report.append('-' * 80)
        report.append(f"ETF 1 Total Holdings: {len(etf1.holdings)}")
        report.append(f"ETF 2 Total Holdings: {len(etf2.holdings)}")
        report.append(f"ETF 1 Unique Holdings: {len(etf1.holdings) - len(common)}")
        report.append(f"ETF 2 Unique Holdings: {len(etf2.holdings) - len(common)}\n")

        if common:
            report.append('COMMON HOLDINGS'.center(80))
            report.append('-' * 80)
            report.append(ReportGenerator._format_holdings_table(common))
            report.append('\n')

        report.append('RECOMMENDATIONS'.center(80))
        report.append('-' * 80)
        recommendations = ReportGenerator._generate_recommendations(score)
        # Replace Unicode characters with ASCII equivalents for Windows compatibility
        recommendations = recommendations.replace('✓', 'OK').replace('✗', 'XX').replace('⚠', 'WW')
        report.append(recommendations)
        report.append('\n')

        report.append('=' * 80)
        report.append('End of Report'.center(80))
        report.append('=' * 80)

        return '\n'.join(report)

    @staticmethod
    def generate_multi_report(result: Dict) -> str:
        """Generate report for multiple ETFs"""
        etfs = result['etfs']
        matrix = result['overlap_matrix']
        avg_overlap = result['average_overlap']

        # Calculate total stock overlap across all ETFs
        stock_appearances = {}
        stock_total_weights = {}
        merged_isins_map = {}  # Track which ISINs were merged

        # Count how many ETFs each stock appears in and total weight
        # Use canonical_id to properly group dual-listed stocks
        for etf in etfs:
            for holding in etf.holdings:
                canonical_id = holding.get('canonical_id') or holding['isin']
                if canonical_id not in stock_appearances:
                    stock_appearances[canonical_id] = 0
                    stock_total_weights[canonical_id] = 0
                    merged_isins_map[canonical_id] = []
                stock_appearances[canonical_id] += 1
                stock_total_weights[canonical_id] += holding['weight']
                if holding['isin'] not in merged_isins_map[canonical_id]:
                    merged_isins_map[canonical_id].append(holding['isin'])

        # Create sorted list of stocks by total weight (primary) and appearance count (secondary)
        stocks_by_appearance = sorted(
            stock_appearances.items(),
            key=lambda x: (-stock_total_weights[x[0]], -x[1])
        )

        # Generate clean JSON output
        json_output = {
            "etfs": [],
            "summary": {
                "total_etfs": len(etfs),
                "average_overlap_percentage": avg_overlap,
                "total_unique_stocks": len(stock_appearances)
            },
            "stock_overlap_analysis": [],
            "pairwise_comparisons": []
        }

        # Add ETF information
        for etf in etfs:
            json_output["etfs"].append({
                "isin": etf.isin,
                "name": etf.name,
                "total_holdings": len(etf.holdings),
                "holdings": etf.holdings
            })

        # Add stock overlap analysis (sorted by appearance)
        for canonical_id, appearance_count in stocks_by_appearance:
            total_weight = stock_total_weights[canonical_id]
            # Find the stock name from any ETF that has it
            stock_name = ""
            original_isin = ""
            for etf in etfs:
                for holding in etf.holdings:
                    cid = holding.get('canonical_id') or holding['isin']
                    if cid == canonical_id:
                        stock_name = holding['name']
                        original_isin = holding['isin']
                        break
                if stock_name:
                    break

            merged_isins = merged_isins_map.get(canonical_id, [canonical_id])
            json_output["stock_overlap_analysis"].append({
                "isin": original_isin,
                "canonical_id": canonical_id,
                "name": stock_name,
                "appears_in_etfs": appearance_count,
                "total_weight_across_all_etfs": total_weight,
                "average_weight_per_etf": total_weight / appearance_count,
                "merged_isins": merged_isins if len(merged_isins) > 1 else None
            })

        # Add pairwise comparisons
        for i, etf1 in enumerate(etfs):
            for j, etf2 in enumerate(etfs):
                if i < j:
                    overlap = matrix[etf1.isin][etf2.isin]
                    json_output["pairwise_comparisons"].append({
                        "etf1_isin": etf1.isin,
                        "etf2_isin": etf2.isin,
                        "overlap_percentage": overlap['overlap_percentage'],
                        "common_holdings_count": len(overlap['common_holdings']),
                        "common_holdings": overlap['common_holdings']
                    })

        # Generate clean text output for console
        report = []
        report.append('=' * 80)
        report.append('MULTI-ETF OVERLAP ANALYSIS REPORT'.center(80))
        report.append('=' * 80 + '\n')

        report.append('ETFS IN ANALYSIS'.center(80))
        report.append('-' * 80)
        for i, etf in enumerate(etfs, 1):
            report.append(f"{i}. {etf.name} ({etf.isin})")
            report.append(f"   Holdings: {len(etf.holdings)}\n")
        report.append('\n')

        report.append('STOCK OVERLAP ANALYSIS (ACROSS ALL ETFs)'.center(80))
        report.append('-' * 80)
        report.append(f"Total Unique Stocks: {len(stock_appearances)}")
        report.append(f"Average Overlap: {avg_overlap:.2f}%\n")

        # Show stocks that appear in multiple ETFs (concentration risk)
        report.append("STOCKS WITH HIGHEST CONCENTRATION RISK:")
        report.append("| {:<15} | {:<30} | {:<15} | {:<25} | {:<20} |".format(
            "ISIN", "Name", "ETF Count", "Total Weight", "Avg Weight/ETF"))
        report.append("|" + "-" * 15 + "|" + "-" * 30 + "|" + "-" * 15 + "|" + "-" * 25 + "|" + "-" * 20 + "|")

        for stock in stocks_by_appearance:
            if stock[1] > 1:  # Only show stocks in multiple ETFs
                canonical_id = stock[0]
                total_weight = stock_total_weights[canonical_id]
                avg_weight = total_weight / stock[1]
                stock_name = ""
                original_isin = canonical_id
                merged_isins = merged_isins_map.get(canonical_id, [canonical_id])
                
                for etf in etfs:
                    for holding in etf.holdings:
                        cid = holding.get('canonical_id') or holding['isin']
                        if cid == canonical_id:
                            stock_name = holding['name']
                            original_isin = holding['isin']
                            break
                    if stock_name:
                        break

                # Show merged ISINs if this stock has multiple listings
                isin_display = original_isin[:13]
                if len(merged_isins) > 1:
                    isin_display = f"{original_isin[:10]}*"  # Asterisk indicates merged
                
                report.append("| {:<15} | {:<30} | {:<15} | {:<25.2f}% | {:<20.2f}% |".format(
                    isin_display, stock_name[:28], f"{stock[1]}/{len(etfs)}", total_weight, avg_weight))
                
                # Add note about merged ISINs
                if len(merged_isins) > 1:
                    report.append(f"  └─ Merged ISINs: {', '.join(merged_isins[:3])}{'...' if len(merged_isins) > 3 else ''}")

        report.append('\n')

        # Add JSON output section
        report.append('JSON OUTPUT (FOR PROGRAMMATIC USE)'.center(80))
        report.append('-' * 80)
        report.append(json.dumps(json_output, indent=2))
        report.append('\n')

        report.append('=' * 80)
        report.append('End of Report'.center(80))
        report.append('=' * 80)

        return '\n'.join(report)

    @staticmethod
    def _format_etf_info(etf: ETFData, label: str) -> str:
        """Format ETF information"""
        info = []
        info.append(f"{label}: {etf.name} ({etf.isin})".center(80))
        info.append('-' * 80)
        info.append(f"Holdings: {len(etf.holdings)}")
        info.append("Top 5 Holdings:")
        for h in sorted(etf.holdings, key=lambda x: x['weight'], reverse=True)[:5]:
            info.append(f"  - {h['name']}: {h['weight']:.2f}% (ISIN: {h['isin']})")
        return '\n'.join(info)

    @staticmethod
    def _format_holdings_table(holdings: List[Dict]) -> str:
        """Format holdings as a table"""
        lines = []
        # Header
        lines.append("| {:<15} | {:<30} | {:>8} | {:>8} | {:>8} |".format(
            "ISIN", "Name", "Weight", "ETF1", "ETF2"))
        lines.append("|" + "-" * 15 + "|" + "-" * 30 + "|" + "-" * 8 + "|" + "-" * 8 + "|" + "-" * 8 + "|")

        # Rows
        for h in sorted(holdings, key=lambda x: x['weight'], reverse=True):
            lines.append("| {:<15} | {:<30} | {:>8.2f}% | {:>8.2f}% | {:>8.2f}% |".format(
                h['isin'][:13], h['name'][:28], h['weight'], h['etf1_weight'], h['etf2_weight']))

        return '\n'.join(lines)

    @staticmethod
    def _generate_recommendations(score: float) -> str:
        """Generate recommendations based on score"""
        if score >= 80:
            return """OK Excellent diversification! These ETFs have minimal overlap.
OK Consider holding both for broad market exposure."""
        elif score >= 60:
            return """OK Good diversification with some overlap.
OK Monitor the common holdings for concentration risk."""
        elif score >= 40:
            return """WW Moderate overlap detected.
WW Consider reducing position size in one of these ETFs.
WW Look for alternative ETFs with less overlap."""
        else:
            return """XX High overlap - poor diversification!
XX These ETFs are essentially investing in the same stocks.
XX Strongly consider holding only one of these ETFs.
XX Look for ETFs with different sector/geographic focus."""

    @staticmethod
    def _get_stock_overlap_analysis(etfs: List[ETFData]) -> List[Dict]:
        """Get stock overlap analysis for JSON output using canonical IDs"""
        stock_appearances = {}
        stock_total_weights = {}
        stock_etf_details = {}

        # Count how many ETFs each stock appears in and total weight
        # Use canonical_id to properly group dual-listed stocks
        for etf in etfs:
            for holding in etf.holdings:
                canonical_id = holding.get('canonical_id') or holding['isin']
                if canonical_id not in stock_appearances:
                    stock_appearances[canonical_id] = 0
                    stock_total_weights[canonical_id] = 0
                    stock_etf_details[canonical_id] = []
                stock_appearances[canonical_id] += 1
                stock_total_weights[canonical_id] += holding['weight']
                stock_etf_details[canonical_id].append({
                    "etf_isin": etf.isin,
                    "etf_name": etf.name,
                    "weight": holding['weight'],
                    "original_isin": holding['isin']
                })

        # Create sorted list of stocks by total weight (primary) and appearance count (secondary)
        stocks_by_appearance = sorted(
            stock_appearances.items(),
            key=lambda x: (-stock_total_weights[x[0]], -x[1])
        )

        # Build analysis
        analysis = []
        for canonical_id, appearance_count in stocks_by_appearance:
            total_weight = stock_total_weights[canonical_id]
            # Find the stock name from any ETF that has it
            stock_name = ""
            original_isin = ""
            merged_isins = []
            
            for etf in etfs:
                for holding in etf.holdings:
                    cid = holding.get('canonical_id') or holding['isin']
                    if cid == canonical_id:
                        if not stock_name:
                            stock_name = holding['name']
                            original_isin = holding['isin']
                        if holding['isin'] not in merged_isins:
                            merged_isins.append(holding['isin'])

            analysis.append({
                "isin": original_isin,
                "canonical_id": canonical_id,
                "name": stock_name,
                "appears_in_etfs": appearance_count,
                "total_weight_across_all_etfs": total_weight,
                "average_weight_per_etf": total_weight / appearance_count,
                "etf_breakdown": stock_etf_details[canonical_id],
                "merged_isins": merged_isins if len(merged_isins) > 1 else None
            })

        return analysis

def main():
    """Main application entry point"""
    parser = argparse.ArgumentParser(description='ETF Overlap Analysis Tool')
    parser.add_argument('--isin1', help='First ETF ISIN code')
    parser.add_argument('--isin2', help='Second ETF ISIN code')
    parser.add_argument('--multi', help='Multiple ETF ISIN codes (comma-separated)')
    parser.add_argument('--expire-cache', action='store_true', help='Expire cache and fetch fresh data')
    parser.add_argument('--json', action='store_true', help='Output JSON format for programmatic use')
    parser.add_argument('--verbose', '-v', action='store_true', help='Enable verbose logging')
    parser.add_argument('--no-normalize', action='store_true', help='Disable ISIN normalization (for comparison)')

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger('etf_overlap').setLevel(logging.DEBUG)
        logging.getLogger('isin_normalizer').setLevel(logging.DEBUG)
        logger.info("Verbose logging enabled")

    logger.info(f"Starting ETF analysis - isin1={args.isin1}, isin2={args.isin2}, multi={args.multi}")

    cache = DataCache()
    normalizer = None if args.no_normalize else ISINNormalizer()
    fetcher = DataFetcher(cache, normalizer)
    calculator = OverlapCalculator()
    report_gen = ReportGenerator()

    try:
        if args.expire_cache:
            logger.info("Expiring cache...")
            cursor = cache.conn.cursor()
            cursor.execute('DELETE FROM etf_cache')
            cache.conn.commit()
            logger.info("Cache expired.")

        if args.isin1 and args.isin2:
            if not validate_isin(args.isin1):
                logger.warning(f"Invalid ISIN format: {args.isin1}")
                error_response = {
                    "error": f"Invalid ISIN format: {args.isin1}. ISINs must be exactly 12 characters (2 letters + 9 alphanumeric + 1 digit).",
                    "status": "failed"
                }
                print(json.dumps(error_response, indent=2))
                return 1
            
            if not validate_isin(args.isin2):
                logger.warning(f"Invalid ISIN format: {args.isin2}")
                error_response = {
                    "error": f"Invalid ISIN format: {args.isin2}. ISINs must be exactly 12 characters (2 letters + 9 alphanumeric + 1 digit).",
                    "status": "failed"
                }
                print(json.dumps(error_response, indent=2))
                return 1
            
            try:
                logger.info(f"Analyzing pair: {args.isin1} vs {args.isin2}")
                etf1 = fetcher.fetch_etf_data(args.isin1.strip().upper())
                etf2 = fetcher.fetch_etf_data(args.isin2.strip().upper())
                result = calculator.calculate_overlap(etf1, etf2)

                json_result = {
                    "etf1": {
                        "isin": etf1.isin,
                        "name": etf1.name,
                        "holdings": etf1.holdings
                    },
                    "etf2": {
                        "isin": etf2.isin,
                        "name": etf2.name,
                        "holdings": etf2.holdings
                    },
                    "summary": {
                        "total_overlap_percentage": result['total_overlap_percentage'],
                        "diversification_score": result['diversification_score'],
                        "common_holdings_count": len(result['common_holdings'])
                    },
                    "common_holdings": result['common_holdings']
                }
                logger.info(f"Analysis complete: overlap={result['total_overlap_percentage']:.2f}%, score={result['diversification_score']:.1f}")
                print(json.dumps(json_result, indent=2))

            except Exception as e:
                logger.error(f"Analysis failed: {e}")
                error_response = {
                    "error": str(e),
                    "status": "failed"
                }
                print(json.dumps(error_response, indent=2))
                return 1

        elif args.multi:
            raw_isins = [i.strip() for i in args.multi.split(',')]
            
            invalid_isins = []
            isins = []
            for isin in raw_isins:
                isin_cleaned = isin.strip().upper()
                if validate_isin(isin_cleaned):
                    isins.append(isin_cleaned)
                else:
                    invalid_isins.append(isin)
            
            if invalid_isins:
                logger.warning(f"Invalid ISINs: {invalid_isins}")
                error_response = {
                    "error": f"Invalid ISIN format detected. ISINs must be exactly 12 characters (2 letters + 9 alphanumeric + 1 digit). Invalid: {', '.join(invalid_isins)}",
                    "status": "failed"
                }
                print(json.dumps(error_response, indent=2))
                return 1

            logger.info(f"Analyzing {len(isins)} ETFs: {', '.join(isins)}")
            etfs = []
            failed_isins = []
            for isin in isins:
                try:
                    etf = fetcher.fetch_etf_data(isin)
                    etfs.append(etf)
                except Exception as e:
                    logger.warning(f"Failed to fetch {isin}: {e}")
                    failed_isins.append((isin, str(e)))

            if len(etfs) < 2:
                logger.error(f"Not enough valid ETFs: {len(etfs)} valid, {len(failed_isins)} failed")
                error_response = {
                    "error": "At least 2 valid ETFs are required for analysis",
                    "failed_isins": [{isin: error} for isin, error in failed_isins],
                    "valid_isins_count": len(etfs)
                }
                print(json.dumps(error_response, indent=2))
                return 1

            # Final normalization pass across all ETFs to ensure cross-ETF company name matching
            if normalizer:
                all_isins = set()
                for etf in etfs:
                    for holding in etf.holdings:
                        if 'isin' in holding:
                            all_isins.add(holding['isin'])
                
                if all_isins:
                    canonical_map = normalizer.get_canonical_ids_batch(list(all_isins))
                    for etf in etfs:
                        for holding in etf.holdings:
                            if 'isin' in holding and holding['isin'] in canonical_map:
                                holding['canonical_id'] = canonical_map[holding['isin']]

            result = calculator.calculate_multi_overlap(etfs)

            output = {
                "etfs": [{
                    "isin": etf.isin,
                    "name": etf.name,
                    "holdings": etf.holdings
                } for etf in result['etfs']],
                "summary": {
                    "total_etfs": len(result['etfs']),
                    "average_overlap_percentage": result['average_overlap'],
                    "total_unique_stocks": len(set(stock['isin'] for etf in result['etfs'] for stock in etf.holdings))
                },
                "stock_overlap_analysis": ReportGenerator._get_stock_overlap_analysis(result['etfs'])
            }

            if failed_isins:
                logger.warning(f"Some ETFs failed: {len(failed_isins)}")
                output['warnings'] = {
                    "failed_isins": [{isin: error} for isin, error in failed_isins],
                    "message": f"{len(failed_isins)} ETF(s) could not be analyzed but analysis continued with {len(etfs)} valid ETF(s)"
                }

            logger.info(f"Multi-ETF analysis complete: {len(etfs)} ETFs, {result['average_overlap']:.2f}% avg overlap")
            print(json.dumps(output, indent=2))

        else:
            parser.print_help()
            return 1

    finally:
        cache.close()
        if normalizer:
            normalizer.close()

    return 0

if __name__ == '__main__':
    exit(main())