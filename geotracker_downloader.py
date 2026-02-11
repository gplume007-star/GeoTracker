"""
GeoTracker Document Downloader

Downloads all available documents from GeoTracker sites within a specified
geographic radius of a target location.

Dependencies:
pip install undetected-chromedriver selenium

Usage:
    python geotracker_downloader.py --lat 37.701 --lon -122.471 --radius .1
    python geotracker_downloader.py --lat 37.7749 --lon -122.4194 --radius .1 --resume
"""

import argparse
import csv
import glob
import json
import logging
import math
import os
import shutil
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

EARTH_RADIUS_MILES = 3958.8

logger = logging.getLogger('geotracker')


def setup_logging():
    """Configure console logging."""
    logger.setLevel(logging.DEBUG)

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console_fmt = logging.Formatter(
        '[%(asctime)s] %(levelname)-8s %(message)s',
        datefmt='%H:%M:%S'
    )
    console.setFormatter(console_fmt)
    logger.addHandler(console)


def parse_sites_file(filepath):
    """
    Parse the TAB-delimited sites.txt file.

    Returns a list of dicts with keys: global_id, business_name, latitude, longitude.
    Rows with empty or unparseable coordinates are skipped.
    """
    sites = []
    skipped = 0
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        reader = csv.DictReader(f, delimiter='\t')
        for row in reader:
            try:
                lat_str = row.get('LATITUDE', '').strip()
                lon_str = row.get('LONGITUDE', '').strip()
                if not lat_str or not lon_str:
                    skipped += 1
                    continue
                lat = float(lat_str)
                lon = float(lon_str)
            except (ValueError, TypeError, KeyError):
                skipped += 1
                continue
            sites.append({
                'global_id': row['GLOBAL_ID'].strip(),
                'business_name': row.get('BUSINESS_NAME', '').strip(),
                'latitude': lat,
                'longitude': lon,
            })
    logger.info(f"Parsed {len(sites)} sites with valid coordinates ({skipped} skipped)")
    return sites


def haversine_distance(lat1, lon1, lat2, lon2):
    """
    Calculate the great-circle distance between two points on Earth
    using the Haversine formula.

    Parameters are in decimal degrees. Returns distance in miles.
    """
    lat1_r, lon1_r = math.radians(lat1), math.radians(lon1)
    lat2_r, lon2_r = math.radians(lat2), math.radians(lon2)

    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r

    a = (math.sin(dlat / 2) ** 2 +
         math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return EARTH_RADIUS_MILES * c


def filter_sites_by_radius(sites, center_lat, center_lon, radius_miles):
    """
    Filter sites to those within radius_miles of (center_lat, center_lon).
    Returns sites sorted by distance (nearest first), each with a distance_miles key.
    """
    nearby = []
    for site in sites:
        dist = haversine_distance(center_lat, center_lon,
                                  site['latitude'], site['longitude'])
        if dist <= radius_miles:
            site_copy = site.copy()
            site_copy['distance_miles'] = round(dist, 3)
            nearby.append(site_copy)

    nearby.sort(key=lambda s: s['distance_miles'])
    logger.info(f"Found {len(nearby)} sites within {radius_miles} miles")
    return nearby


class GeoTrackerDownloader:
    """Manages Selenium browser and document download workflow."""

    DOWNLOAD_URL = "https://geotracker.waterboards.ca.gov/profile_report?global_id={}&mytab=sitedocuments&zipdownload=True"
    HOME_URL = "https://geotracker.waterboards.ca.gov/"

    def __init__(self, output_dir, delay, timeout, headless, resume):
        self.output_dir = Path(output_dir)
        self.delay = delay
        self.timeout = timeout
        self.headless = headless
        self.resume = resume
        self.driver = None
        self.base_temp_dir = None
        self.results = []
        self.center_lat = None
        self.center_lon = None
        self.radius_miles = None
        self.total_sites = 0

    def _init_driver(self):
        """Initialize undetected_chromedriver with download preferences."""
        import undetected_chromedriver as uc

        options = uc.ChromeOptions()

        if self.headless:
            options.add_argument('--headless=new')

        self.base_temp_dir = tempfile.mkdtemp(prefix='geotracker_')
        prefs = {
            'download.default_directory': self.base_temp_dir,
            'download.prompt_for_download': False,
            'download.directory_upgrade': True,
            'plugins.always_open_pdf_externally': True,
        }
        options.add_experimental_option('prefs', prefs)
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--window-size=1920,1080')

        self.driver = uc.Chrome(options=options, version_main=144)
        self.driver.set_page_load_timeout(self.timeout)
        self.driver.implicitly_wait(10)
        logger.info("Browser initialized")

    def _wait_for_cloudflare(self):
        """
        Navigate to homepage and wait for Cloudflare challenge to resolve.
        Returns True if resolved successfully.
        """
        logger.info("Navigating to GeoTracker homepage to handle Cloudflare challenge...")
        self.driver.get(self.HOME_URL)

        max_wait = 30
        poll_interval = 2
        elapsed = 0

        while elapsed < max_wait:
            page_source = self.driver.page_source.lower()
            cf_markers = [
                'checking your browser',
                'just a moment',
                'cf-browser-verification',
                'challenge-platform',
            ]
            if any(marker in page_source for marker in cf_markers):
                logger.debug(f"Cloudflare challenge still active ({elapsed}s)...")
                time.sleep(poll_interval)
                elapsed += poll_interval
            else:
                logger.info(f"Cloudflare challenge resolved after {elapsed}s")
                return True

        logger.warning("Cloudflare challenge may not have resolved within timeout")
        return False

    def _download_all_documents(self, global_id):
        """
        Navigate directly to the Site Maps / Documents download page,
        click 'SELECT ALL DOCUMENTS', then 'Download Selected Files'.

        The URL pattern profile_report?global_id={ID}&mytab=sitedocuments&zipdownload=True
        goes directly to the multi-select download view, skipping the need to
        click the tab or the 'mark multiple' link.

        Returns (success, temp_dir, doc_count).
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        site_temp_dir = tempfile.mkdtemp(prefix=f'gt_{global_id}_')

        # Set download directory for this site via CDP
        self.driver.execute_cdp_cmd('Page.setDownloadBehavior', {
            'behavior': 'allow',
            'downloadPath': site_temp_dir,
        })

        # Navigate directly to the download page
        url = self.DOWNLOAD_URL.format(global_id)
        logger.info(f"Navigating to {url}")

        try:
            self.driver.get(url)
            time.sleep(3)
        except Exception as e:
            logger.error(f"Failed to load page for {global_id}: {e}")
            return False, site_temp_dir, 0

        # Check if the page has documents (look for "SELECT ALL DOCUMENTS" link)
        # Step 1: Click "SELECT ALL DOCUMENTS"
        select_clicked = False
        try:
            element = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.LINK_TEXT, "SELECT ALL DOCUMENTS"))
            )
            element.click()
            time.sleep(2)
            select_clicked = True
            logger.info("Clicked 'SELECT ALL DOCUMENTS'")
        except Exception:
            pass

        if not select_clicked:
            # Try partial/alternate selectors
            alt_selectors = [
                (By.PARTIAL_LINK_TEXT, "SELECT ALL DOCUMENTS"),
                (By.XPATH, "//a[contains(text(), 'SELECT ALL DOCUMENTS')]"),
                (By.XPATH, "//*[contains(text(), 'SELECT ALL DOCUMENTS')]"),
            ]
            for by, selector in alt_selectors:
                try:
                    element = WebDriverWait(self.driver, 5).until(
                        EC.element_to_be_clickable((by, selector))
                    )
                    element.click()
                    time.sleep(2)
                    select_clicked = True
                    logger.info("Clicked 'SELECT ALL DOCUMENTS' (alt selector)")
                    break
                except Exception:
                    continue

        if not select_clicked:
            logger.warning(f"No 'SELECT ALL DOCUMENTS' link found for {global_id} - site may have no documents")
            return False, site_temp_dir, 0

        # Step 2: Click "Download Selected Files" button
        download_clicked = False
        try:
            element = WebDriverWait(self.driver, 5).until(
                EC.element_to_be_clickable(
                    (By.XPATH, "//input[@type='button' and @value='Download Selected Files']"))
            )
            element.click()
            time.sleep(2)
            download_clicked = True
            logger.info("Clicked 'Download Selected Files'")
        except Exception:
            pass

        if not download_clicked:
            alt_selectors = [
                (By.XPATH, "//input[contains(@value, 'Download Selected')]"),
                (By.XPATH, "//button[contains(text(), 'Download Selected')]"),
                (By.XPATH, "//*[contains(text(), 'Download Selected Files')]"),
            ]
            for by, selector in alt_selectors:
                try:
                    element = WebDriverWait(self.driver, 5).until(
                        EC.element_to_be_clickable((by, selector))
                    )
                    element.click()
                    time.sleep(2)
                    download_clicked = True
                    logger.info("Clicked 'Download Selected Files' (alt selector)")
                    break
                except Exception:
                    continue

        if not download_clicked:
            logger.warning(f"Could not find 'Download Selected Files' button for {global_id}")
            return self._fallback_download_documents(global_id, site_temp_dir)

        # Wait for download to complete
        success = self._wait_for_download(site_temp_dir, timeout=120)
        files = [f for f in os.listdir(site_temp_dir) if not f.endswith('.crdownload')]
        doc_count = len(files)

        if success and doc_count > 0:
            logger.info(f"Downloaded {doc_count} file(s) for {global_id}")
            return True, site_temp_dir, doc_count
        else:
            logger.warning(f"Download may have failed for {global_id} ({doc_count} files found)")
            return doc_count > 0, site_temp_dir, doc_count

    def _fallback_download_documents(self, global_id, site_temp_dir):
        """
        Fallback: discover individual document links and download them one by one.
        Used when the bulk download UI cannot be found.
        """
        from selenium.webdriver.common.by import By

        logger.info(f"Using fallback document discovery for {global_id}")

        doc_links = []
        seen_urls = set()

        # Find links to the documents subdomain
        links = self.driver.find_elements(
            By.CSS_SELECTOR, 'a[href*="documents.geotracker.waterboards.ca.gov"]')
        for link in links:
            href = link.get_attribute('href')
            if href and href not in seen_urls:
                seen_urls.add(href)
                doc_links.append(href)

        # Find PDF links
        pdf_links = self.driver.find_elements(
            By.CSS_SELECTOR, 'a[href$=".pdf"], a[href$=".PDF"]')
        for link in pdf_links:
            href = link.get_attribute('href')
            if href and href not in seen_urls:
                seen_urls.add(href)
                doc_links.append(href)

        # JS sweep
        try:
            js_docs = self.driver.execute_script("""
                var docs = [];
                var allLinks = document.querySelectorAll('a[href]');
                for (var i = 0; i < allLinks.length; i++) {
                    var href = allLinks[i].href;
                    if (href && (
                        href.includes('documents.geotracker') ||
                        href.toLowerCase().endsWith('.pdf') ||
                        href.includes('/esi/uploads/') ||
                        href.includes('geo_report')
                    )) {
                        docs.push(href);
                    }
                }
                return docs;
            """)
            for url in (js_docs or []):
                if url not in seen_urls:
                    seen_urls.add(url)
                    doc_links.append(url)
        except Exception:
            pass

        if not doc_links:
            logger.info(f"No document links found for {global_id}")
            return False, site_temp_dir, 0

        logger.info(f"Found {len(doc_links)} document links via fallback")

        downloaded = 0
        for i, url in enumerate(doc_links):
            filename = url.split('/')[-1].split('?')[0] or f'document_{i}.pdf'
            logger.info(f"  Downloading [{i+1}/{len(doc_links)}]: {filename}")
            try:
                self.driver.get(url)
                if self._wait_for_download(site_temp_dir, timeout=60):
                    downloaded += 1
                time.sleep(max(1, self.delay / 2))
            except Exception as e:
                logger.warning(f"  Failed: {e}")

        return downloaded > 0, site_temp_dir, downloaded

    def _wait_for_download(self, download_dir, timeout=60):
        """
        Wait for a download to complete by monitoring for .crdownload files.
        Returns True if a download completed within the timeout.
        """
        start = time.time()
        # Give the download a moment to start
        time.sleep(2)

        while time.time() - start < timeout:
            in_progress = glob.glob(os.path.join(download_dir, '*.crdownload'))
            files = [f for f in os.listdir(download_dir) if not f.endswith('.crdownload')]

            if files and not in_progress:
                return True

            if not files and not in_progress and (time.time() - start) > 10:
                # No files appeared and nothing in progress after 10s
                return False

            time.sleep(1)

        return False

    def _create_zip(self, global_id, site_temp_dir):
        """
        Package all downloaded files into {GLOBAL_ID}.zip.
        Returns the zip path or None if no files.
        """
        downloaded_files = []
        for f in os.listdir(site_temp_dir):
            fpath = os.path.join(site_temp_dir, f)
            if os.path.isfile(fpath) and not f.endswith('.crdownload'):
                downloaded_files.append(fpath)

        if not downloaded_files:
            logger.warning(f"No files to zip for {global_id}")
            return None

        zip_path = self.output_dir / f"{global_id}.zip"

        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for filepath in downloaded_files:
                arcname = os.path.basename(filepath)
                zf.write(filepath, arcname)

        # Clean up temp directory
        shutil.rmtree(site_temp_dir, ignore_errors=True)

        zip_size_mb = os.path.getsize(zip_path) / (1024 * 1024)
        logger.info(f"Created {zip_path.name} ({len(downloaded_files)} files, {zip_size_mb:.1f} MB)")
        return str(zip_path)

    def _recover_driver(self):
        """Attempt to restart the browser after a crash."""
        logger.warning("Attempting browser recovery...")
        try:
            if self.driver:
                self.driver.quit()
                self.driver = None
        except Exception:
            self.driver = None

        time.sleep(5)
        self._init_driver()
        self._wait_for_cloudflare()
        logger.info("Browser recovery successful")

    def process_site(self, site):
        """
        Process a single site: navigate to profile, find docs tab,
        download documents, create zip.
        """
        global_id = site['global_id']
        result = {
            'global_id': global_id,
            'business_name': site['business_name'],
            'distance_miles': site['distance_miles'],
            'status': 'pending',
            'documents_found': 0,
            'documents_downloaded': 0,
            'zip_file': None,
            'errors': [],
        }

        # Resume: skip if zip already exists
        if self.resume:
            existing_zip = self.output_dir / f"{global_id}.zip"
            if existing_zip.exists():
                logger.info(f"Skipping {global_id} (zip already exists)")
                result['status'] = 'skipped_existing'
                return result

        # Download all documents (navigates directly to download page)
        try:
            success, site_temp_dir, doc_count = self._download_all_documents(global_id)
        except Exception as e:
            logger.error(f"Error downloading documents for {global_id}: {e}")
            result['status'] = 'download_error'
            result['errors'].append(str(e))
            return result

        result['documents_downloaded'] = doc_count

        if not success or doc_count == 0:
            result['status'] = 'no_documents'
            if os.path.exists(site_temp_dir):
                shutil.rmtree(site_temp_dir, ignore_errors=True)
            return result

        # Step 3: Create zip
        zip_path = self._create_zip(global_id, site_temp_dir)
        if zip_path:
            result['zip_file'] = zip_path
            result['status'] = 'completed'
        else:
            result['status'] = 'zip_failed'
            result['errors'].append('Failed to create zip file')

        return result

    def _write_summary_log(self):
        """Write a JSON summary of the entire run."""
        summary = {
            'run_timestamp': datetime.now().isoformat(),
            'parameters': {
                'center_lat': self.center_lat,
                'center_lon': self.center_lon,
                'radius_miles': self.radius_miles,
                'delay_seconds': self.delay,
                'headless': self.headless,
            },
            'totals': {
                'sites_in_radius': self.total_sites,
                'sites_processed': len(self.results),
                'sites_with_documents': sum(
                    1 for r in self.results if r['status'] == 'completed'),
                'sites_no_documents': sum(
                    1 for r in self.results if r['status'] == 'no_documents'),
                'sites_failed': sum(
                    1 for r in self.results if 'failed' in r['status'] or 'error' in r['status']),
                'sites_skipped': sum(
                    1 for r in self.results if r['status'] == 'skipped_existing'),
                'total_documents_downloaded': sum(
                    r['documents_downloaded'] for r in self.results),
            },
            'sites': self.results,
        }

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_path = self.output_dir / f"download_log_{timestamp}.json"
        with open(log_path, 'w') as f:
            json.dump(summary, f, indent=2)

        logger.info(f"Summary log written to {log_path}")

    def run(self, sites):
        """Process all filtered sites."""
        self.output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Starting download for {len(sites)} sites")
        logger.info(f"Output directory: {self.output_dir.resolve()}")

        self._init_driver()

        try:
            if not self._wait_for_cloudflare():
                logger.error("Failed to bypass Cloudflare. Try running without --headless.")
                return

            for i, site in enumerate(sites):
                logger.info("")
                logger.info("=" * 60)
                logger.info(
                    f"Processing site {i+1}/{len(sites)}: "
                    f"{site['global_id']} - {site['business_name']}")
                logger.info(f"Distance: {site['distance_miles']} miles")
                logger.info("=" * 60)

                try:
                    result = self.process_site(site)
                except Exception as e:
                    logger.error(f"Unexpected error processing {site['global_id']}: {e}")
                    result = {
                        'global_id': site['global_id'],
                        'business_name': site['business_name'],
                        'distance_miles': site['distance_miles'],
                        'status': 'unexpected_error',
                        'documents_found': 0,
                        'documents_downloaded': 0,
                        'zip_file': None,
                        'errors': [str(e)],
                    }
                    # Try to recover the browser
                    try:
                        self._recover_driver()
                    except Exception as re:
                        logger.error(f"Browser recovery failed: {re}")
                        self.results.append(result)
                        break

                self.results.append(result)

                # Progress summary
                completed = sum(1 for r in self.results if r['status'] == 'completed')
                no_docs = sum(1 for r in self.results if r['status'] == 'no_documents')
                failed = sum(1 for r in self.results
                             if 'failed' in r['status'] or 'error' in r['status'])
                logger.info(
                    f"Progress: {i+1}/{len(sites)} processed | "
                    f"{completed} downloaded | {no_docs} empty | {failed} failed")

                # Rate limiting between sites
                if i < len(sites) - 1:
                    logger.info(f"Waiting {self.delay}s before next site...")
                    time.sleep(self.delay)

        except KeyboardInterrupt:
            logger.warning("\nInterrupted by user. Saving partial results...")
        finally:
            if self.driver:
                try:
                    self.driver.quit()
                    self.driver = None
                except Exception:
                    self.driver = None
            self._write_summary_log()
            # Clean up base temp dir
            if self.base_temp_dir and os.path.exists(self.base_temp_dir):
                shutil.rmtree(self.base_temp_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(
        description='Download documents from GeoTracker sites within a geographic radius.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --lat 34.0522 --lon -118.2437 --radius 5
  %(prog)s --lat 37.7749 --lon -122.4194 --radius 10 --delay 8 --resume
  %(prog)s --lat 34.0522 --lon -118.2437 --radius 2 --max-sites 5
        """
    )

    parser.add_argument('--lat', type=float, required=True,
                        help='Center latitude (decimal degrees)')
    parser.add_argument('--lon', type=float, required=True,
                        help='Center longitude (decimal degrees)')
    parser.add_argument('--radius', type=float, required=True,
                        help='Search radius in miles')
    parser.add_argument('--sites-file', type=str,
                        default='GeoTrackerDownload/sites.txt',
                        help='Path to sites.txt (default: GeoTrackerDownload/sites.txt)')
    parser.add_argument('--output-dir', type=str, default='downloads',
                        help='Output directory for zip files (default: downloads/)')
    parser.add_argument('--delay', type=float, default=5.0,
                        help='Delay between site requests in seconds (default: 5)')
    parser.add_argument('--max-sites', type=int, default=None,
                        help='Maximum number of sites to process')
    parser.add_argument('--timeout', type=float, default=30.0,
                        help='Page load timeout in seconds (default: 30)')
    parser.add_argument('--headless', action='store_true',
                        help='Run browser in headless mode (may not bypass Cloudflare)')
    parser.add_argument('--resume', action='store_true',
                        help='Skip sites that already have a zip in output-dir')

    args = parser.parse_args()

    # Validate inputs
    if not (-90 <= args.lat <= 90):
        parser.error("Latitude must be between -90 and 90")
    if not (-180 <= args.lon <= 180):
        parser.error("Longitude must be between -180 and 180")
    if args.radius <= 0:
        parser.error("Radius must be positive")

    setup_logging()

    # Step 1: Parse sites
    logger.info(f"Reading sites from {args.sites_file}...")
    sites = parse_sites_file(args.sites_file)

    # Step 2: Filter by radius
    logger.info(f"Filtering sites within {args.radius} miles of ({args.lat}, {args.lon})...")
    nearby_sites = filter_sites_by_radius(sites, args.lat, args.lon, args.radius)

    if not nearby_sites:
        logger.info("No sites found within the specified radius. Exiting.")
        return

    # Step 3: Apply max-sites limit
    if args.max_sites:
        nearby_sites = nearby_sites[:args.max_sites]
        logger.info(f"Limited to first {args.max_sites} sites")

    # Step 4: Display summary and confirm
    print(f"\nFound {len(nearby_sites)} sites within {args.radius} miles:")
    for i, s in enumerate(nearby_sites[:10]):
        print(f"  {i+1}. {s['global_id']} - {s['business_name']} ({s['distance_miles']} mi)")
    if len(nearby_sites) > 10:
        print(f"  ... and {len(nearby_sites) - 10} more")

    response = input(f"\nProceed to download documents for {len(nearby_sites)} sites? [y/N] ")
    if response.lower() != 'y':
        print("Aborted.")
        return

    # Step 5: Run downloader
    downloader = GeoTrackerDownloader(
        output_dir=args.output_dir,
        delay=args.delay,
        timeout=args.timeout,
        headless=args.headless,
        resume=args.resume,
    )
    downloader.center_lat = args.lat
    downloader.center_lon = args.lon
    downloader.radius_miles = args.radius
    downloader.total_sites = len(nearby_sites)

    downloader.run(nearby_sites)


if __name__ == '__main__':
    # Suppress the harmless OSError from undetected-chromedriver's __del__ on Windows
    import atexit
    atexit.register(lambda: os._exit(0))
    main()
