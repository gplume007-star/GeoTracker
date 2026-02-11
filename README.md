# GeoTracker Document Downloader

A Python tool that bulk-downloads environmental site documents from the [California State Water Resources Control Board GeoTracker](https://geotracker.waterboards.ca.gov/) database. Given a geographic coordinate and search radius, it identifies all tracked sites in the area and downloads their associated documents into per-site ZIP archives.

## Table of Contents

- [Overview](#overview)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Data Setup](#data-setup)
- [Usage](#usage)
- [Command-Line Arguments](#command-line-arguments)
- [How It Works](#how-it-works)
- [Output](#output)
- [Resume Support](#resume-support)
- [Troubleshooting](#troubleshooting)
- [Project Structure](#project-structure)

## Overview

GeoTracker is a California state database that tracks sites with environmental contamination cases, including Leaking Underground Storage Tank (LUST) cleanup sites and land disposal sites. Each site may have associated regulatory documents (reports, correspondence, lab results, etc.) available for download.

This tool automates the process of:

1. Filtering the full GeoTracker sites database to a geographic area of interest
2. Navigating to each site's document page in a browser
3. Selecting and downloading all available documents
4. Packaging each site's documents into a labeled ZIP file
5. Producing a JSON summary log of the entire run

## Prerequisites

- **Python 3.8+**
- **Google Chrome** browser installed (the script uses Chrome version 144 by default)
- **GeoTracker sites data export** (see [Data Setup](#data-setup))

## Installation

1. Clone or download this repository.

2. Install the required Python packages:

   ```bash
   pip install -r requirements.txt
   ```

   This installs:
   - `undetected-chromedriver>=3.5.5` -- Selenium wrapper that bypasses Cloudflare and bot detection
   - `selenium>=4.15.0` -- Browser automation framework

## Data Setup

The downloader requires a local copy of the GeoTracker sites database to determine which sites fall within your search radius.

1. Download the full GeoTracker data export from the [GeoTracker website](https://geotracker.waterboards.ca.gov/).
2. Extract the export. It contains several TAB-delimited text files:
   - `sites.txt` -- Site locations, names, case types, and statuses
   - `regulatory_activities.txt` -- Regulatory actions for each site
   - `status_history.txt` -- Historical status changes
   - `contacts.txt` -- Regulatory contact information
3. Place these files in the `GeoTrackerDownload/` subdirectory (or specify a custom path with `--sites-file`).

The script reads `sites.txt` and uses the `GLOBAL_ID`, `BUSINESS_NAME`, `LATITUDE`, and `LONGITUDE` columns to locate sites. All files are linked by the `GLOBAL_ID` field.

## Usage

### Basic Usage

Download documents for all sites within 0.1 miles of a location (GeoTracker has many sites on it, use a small radius):

```bash
python geotracker_downloader.py --lat 37.701 --lon -122.471 --radius .1
```

### Resume an Interrupted Run

If a download session is interrupted, re-run with `--resume` to skip sites that already have a ZIP file in the output directory:

```bash
python geotracker_downloader.py --lat 37.701 --lon -122.471 --radius .1 --resume
```

### Limit the Number of Sites

Process only the 10 nearest sites:

```bash
python geotracker_downloader.py --lat 37.7749 --lon -122.4194 --radius 10 --max-sites 10
```

### Custom Output Directory and Delay

```bash
python geotracker_downloader.py --lat 34.0522 --lon -118.2437 --radius 2 --output-dir my_downloads --delay 8
```

## Command-Line Arguments

| Argument | Required | Default | Description |
|---|---|---|---|
| `--lat` | Yes | -- | Center latitude in decimal degrees |
| `--lon` | Yes | -- | Center longitude in decimal degrees |
| `--radius` | Yes | -- | Search radius in miles |
| `--sites-file` | No | `GeoTrackerDownload/sites.txt` | Path to the TAB-delimited sites data file |
| `--output-dir` | No | `downloads/` | Directory where ZIP files and logs are saved |
| `--delay` | No | `5.0` | Delay in seconds between processing each site (rate limiting) |
| `--max-sites` | No | All | Maximum number of sites to process (nearest first) |
| `--timeout` | No | `30.0` | Page load timeout in seconds |
| `--headless` | No | Off | Run Chrome in headless mode (may not bypass Cloudflare) |
| `--resume` | No | Off | Skip sites that already have a ZIP in the output directory |

## How It Works

### 1. Site Filtering

The script parses `sites.txt` and uses the [Haversine formula](https://en.wikipedia.org/wiki/Haversine_formula) to calculate the great-circle distance between each site and the specified center point. Only sites within the given radius are kept, sorted by distance (nearest first).

### 2. Browser Initialization

An `undetected-chromedriver` Chrome instance is launched. This specialized driver helps bypass Cloudflare bot detection that protects the GeoTracker website. The browser is configured with a temporary download directory and optimized settings.

### 3. Cloudflare Challenge

Before downloading, the script navigates to the GeoTracker homepage and waits up to 30 seconds for any Cloudflare verification challenge to resolve. This step is critical for the subsequent downloads to succeed.

### 4. Document Download (Per Site)

For each site, the script:

1. Navigates directly to the site's document download page using the URL pattern:
   `https://geotracker.waterboards.ca.gov/profile_report?global_id={ID}&mytab=sitedocuments&zipdownload=True`
2. Clicks the "SELECT ALL DOCUMENTS" link
3. Clicks the "Download Selected Files" button
4. Waits for the download to complete (monitors for `.crdownload` temporary files)

If the bulk download UI is not found, a **fallback method** discovers individual document links on the page and downloads them one by one.

### 5. ZIP Packaging

All downloaded files for a site are compressed into a ZIP archive named `{GLOBAL_ID}.zip` in the output directory. Temporary download files are cleaned up after zipping.

### 6. Error Recovery

If the browser crashes during processing, the script attempts automatic recovery by restarting Chrome and re-authenticating through Cloudflare.

## Output

### ZIP Archives

Each successfully processed site produces a ZIP file named with the site's Global ID:

```
downloads/
  T0608100125.zip
  T0608100152.zip
  T0608100243.zip
  T0608135649.zip
```

### Summary Log

Each run produces a timestamped JSON log file with complete details:

```
downloads/download_log_20260210_205457.json
```

The log contains:

- **Run parameters** -- coordinates, radius, delay, headless mode
- **Aggregate totals** -- sites processed, documents downloaded, failures, skips
- **Per-site results** -- status, document count, ZIP file path, any errors

Example summary totals:

```json
{
  "totals": {
    "sites_in_radius": 4,
    "sites_processed": 4,
    "sites_with_documents": 4,
    "sites_no_documents": 0,
    "sites_failed": 0,
    "sites_skipped": 0,
    "total_documents_downloaded": 4
  }
}
```

### Console Output

Real-time progress is logged to the console, including site-by-site status, download counts, and a running tally of completed/empty/failed sites.

## Resume Support

The `--resume` flag enables incremental downloading. When enabled, the script checks the output directory for existing `{GLOBAL_ID}.zip` files and skips any site that already has one. This is useful for:

- Recovering from interrupted sessions (network issues, browser crashes, manual cancellation)
- Expanding a previous search to a larger radius without re-downloading existing sites
- Retrying after fixing issues, without repeating successful downloads

Sites skipped via resume are logged with a `skipped_existing` status in the summary log.

## Troubleshooting

### Cloudflare Blocks the Browser

- **Avoid `--headless` mode.** Headless Chrome is more easily detected by Cloudflare. Run in normal (visible) mode for reliable access.
- If Cloudflare still blocks you, try increasing the `--delay` between sites.

### Chrome Version Mismatch

The script pins `version_main=144` in the Chrome driver initialization. If your installed Chrome version is different, you may see a version mismatch error. Update the value on line 160 of `geotracker_downloader.py` to match your Chrome version, or remove the parameter to auto-detect.

### No Documents Found for a Site

Not all GeoTracker sites have uploaded documents. Sites with no available documents are recorded with a `no_documents` status -- this is expected behavior.

### Download Timeouts

For sites with large document sets, downloads may exceed the default timeout. Increase the `--timeout` value if you encounter frequent timeout errors.

### Permission or Path Errors

Ensure the output directory is writable. On Windows, avoid paths with restricted permissions. The script creates the output directory automatically if it does not exist.

## Project Structure

```
GeoTracker/
  geotracker_downloader.py    # Main script
  requirements.txt             # Python dependencies
  README.md                    # This file
  GeoTrackerDownload/          # GeoTracker data export
    sites.txt                  #   Site locations and metadata
    regulatory_activities.txt  #   Regulatory actions
    status_history.txt         #   Status change history
    contacts.txt               #   Regulatory contacts
    readme.txt                 #   Data export documentation
  downloads/                   # Output directory (created at runtime)
    {GLOBAL_ID}.zip            #   Per-site document archives
    download_log_*.json        #   Run summary logs
```
