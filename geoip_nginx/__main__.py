"""Main module for GeoIP Nginx allowlist generation."""

import argparse
import io
import os
import sys
import tarfile
from datetime import datetime, timedelta
from pathlib import Path

import maxminddb
import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth
from rich.console import Console
from rich.traceback import install as rich_traceback_install

from geoip_nginx.network import merge_ip_ranges

rich_traceback_install()
load_dotenv()
console = Console()

MAXMIND_ACCOUNT_ID = os.getenv("MAXMIND_ACCOUNT_ID", "")
MAXMIND_LICENSE_KEY = os.getenv("MAXMIND_LICENSE_KEY", "")
COUNTRY_CODE_LENGTH = 2

DOWNLOAD_URL = "https://download.maxmind.com/geoip/databases/GeoLite2-Country/download?suffix=tar.gz"

DB_PATH = Path("/usr/share/GeoIP/GeoLite2-Country.mmdb")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
DB_MAX_AGE = timedelta(days=7)

DEFAULT_OUTPUT_PATH = Path("/etc/nginx/maxmind_geoip_allowlist.conf")

REQUESTS_TIMEOUT = 10


def download_geolite2_db() -> bool:
    """Download the GeoLite2 database if it's missing or outdated."""
    if DB_PATH.is_file() and DB_PATH.stat().st_mtime >= (datetime.now() - DB_MAX_AGE).timestamp():
        console.log(f"GeoLite2 database is up to date at {DB_PATH}. Skipping download.")
        return True

    auth = HTTPBasicAuth(MAXMIND_ACCOUNT_ID, MAXMIND_LICENSE_KEY)
    response = requests.get(DOWNLOAD_URL, auth=auth, timeout=REQUESTS_TIMEOUT)
    response.raise_for_status()

    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.name.endswith("GeoLite2-Country.mmdb"):
                member.name = Path(member.name).name
                tar.extract(member, path=DB_PATH.parent)
                extracted_path = DB_PATH.parent / member.name
                extracted_path.rename(DB_PATH)
                return True

    console.log("[bold red]Failed to find GeoLite2-Country.mmdb in the downloaded archive.[/]")
    return False


def get_country_ip_ranges(country_codes: list[str]) -> list[str]:
    """Get IP ranges for a given country code from the GeoLite2 database."""
    for country_code in country_codes:
        if len(country_code) != COUNTRY_CODE_LENGTH:
            console.log(f"[bold red]Invalid country code: {country_code}. Must be a 2-letter ISO code.[/]")

    ranges = []
    with maxminddb.open_database(DB_PATH) as reader:
        for network, record in reader:
            if not record:
                continue

            country = record.get("country")

            if not country:
                continue

            if country.get("iso_code") in [country_code.upper() for country_code in country_codes]:
                ranges.append(str(network))

    return ranges


def _write_allowlist_file(output_path: Path, ip_ranges: list[str]) -> None:
    """Write the allowlist file for Nginx."""
    console.print(f"Writing allowlist to {output_path}...")
    with output_path.open("w") as f:
        for ip_range in ip_ranges:
            f.write(f"allow {ip_range};\n")


def main() -> None:
    """Main entry point for the script."""
    console.log(f"Maxmind Account ID: [bold green]{MAXMIND_ACCOUNT_ID}[/]")
    console.log(f"Maxmind License Key: [bold green]{MAXMIND_LICENSE_KEY[:4] + '*' * (len(MAXMIND_LICENSE_KEY) - 4)}[/]")

    parser = argparse.ArgumentParser(description="Generate GeoIP allowlist for Nginx.")
    parser.add_argument("--output", type=Path, help="Path to save the nginx conf file", default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--countries", nargs="+", help="List of country codes to allow", required=True)
    args = parser.parse_args()

    output_path: Path = args.output
    countries: list[str] = args.countries
    del args  # Stop me using args directly

    console.log(f"Output file: [bold green]{output_path}[/]")
    console.log(f"Allowed countries: [bold green]{', '.join(countries)}[/]")
    if not download_geolite2_db():
        console.log("[bold red]Failed to download GeoLite2 database. Exiting.[/]")
        sys.exit(1)

    all_ip_ranges = []
    ip_ranges = get_country_ip_ranges(countries)
    all_ip_ranges.extend(ip_ranges)

    # all_ip_ranges = merge_ip_ranges(all_ip_ranges)
    _write_allowlist_file(output_path, all_ip_ranges)


if __name__ == "__main__":
    main()
