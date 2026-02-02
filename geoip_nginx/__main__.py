"""Main module for GeoIP Nginx allowlist generation."""

import argparse
import os
import sys
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.traceback import install as rich_traceback_install

from .maxmind import MaxMindHandler

rich_traceback_install()
load_dotenv()
console = Console()

MAXMIND_ACCOUNT_ID = os.getenv("MAXMIND_ACCOUNT_ID", "")
MAXMIND_LICENSE_KEY = os.getenv("MAXMIND_LICENSE_KEY", "")

DB_BASE_PATH = Path("/usr/share/GeoIP")
DB_MAX_AGE = timedelta(days=7)

DEFAULT_OUTPUT_PATH = Path("/etc/nginx/ipallowlist_maxmind_geoip.conf")


def _write_allowlist_file(output_path: Path, ip_ranges: list[str]) -> None:
    """Write the allowlist file for Nginx."""
    console.print(f"Writing allowlist to {output_path}...")
    with output_path.open("w") as f:
        for ip_range in ip_ranges:
            f.write(f"allow {ip_range};\n")

    perms = 0o644
    console.print(f"Setting permissions to {oct(perms)}...")
    output_path.chmod(perms)


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

    # Initialize MaxMind handler (downloads both databases)
    maxmind_handler = MaxMindHandler(
        account_id=MAXMIND_ACCOUNT_ID,
        license_key=MAXMIND_LICENSE_KEY,
        db_base_path=DB_BASE_PATH,
        max_age=DB_MAX_AGE,
    )

    # Get IP ranges for the specified countries
    all_ip_ranges = []
    ip_ranges = maxmind_handler.get_country_ip_ranges(countries)
    all_ip_ranges.extend(ip_ranges)

    _write_allowlist_file(output_path, all_ip_ranges)


if __name__ == "__main__":
    main()
