"""Main module for GeoIP Nginx allowlist generation."""

import argparse
import io
import ipaddress
import os
import sys
import tarfile
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import maxminddb
from dotenv import load_dotenv
from rich.console import Console
from rich.traceback import install as rich_traceback_install

rich_traceback_install()
load_dotenv()
console = Console()

MAXMIND_ACCOUNT_ID = os.getenv("MAXMIND_ACCOUNT_ID", "")
MAXMIND_LICENSE_KEY = os.getenv("MAXMIND_LICENSE_KEY", "")

DOWNLOAD_URL = "https://download.maxmind.com/geoip/databases/GeoLite2-Country/download?suffix=tar.gz"

DB_PATH = Path("/usr/share/GeoIP/GeoLite2-Country.mmdb")
DB_MAX_AGE = timedelta(days=7)

DEFAULT_OUTPUT_PATH = Path("/etc/nginx/ipallowlist_maxmind_geoip.conf")

REQUESTS_TIMEOUT = 10

type IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def download_geolite2_db() -> None:
    """Download the GeoLite2 database if it's missing or outdated."""
    if DB_PATH.is_file() and DB_PATH.stat().st_mtime >= (datetime.now() - DB_MAX_AGE).timestamp():
        console.log(f"GeoLite2 database is up to date at {DB_PATH}. Skipping download.")
        return

    if not (MAXMIND_ACCOUNT_ID and MAXMIND_LICENSE_KEY):
        sys.exit("MAXMIND_ACCOUNT_ID and MAXMIND_LICENSE_KEY must be set to download the database.")

    response = httpx.get(
        DOWNLOAD_URL,
        auth=(MAXMIND_ACCOUNT_ID, MAXMIND_LICENSE_KEY),
        timeout=REQUESTS_TIMEOUT,
        follow_redirects=True,
    )
    response.raise_for_status()

    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tar:
        member = next((m for m in tar.getmembers() if m.name.endswith(DB_PATH.name)), None)
        db_file = tar.extractfile(member) if member else None
        if db_file is None:
            sys.exit(f"{DB_PATH.name} not found in the downloaded archive.")
        _atomic_write(DB_PATH, db_file.read())

    console.log(f"Downloaded GeoLite2 database to {DB_PATH}.")


def get_country_networks(country_codes: set[str]) -> list[IPNetwork]:
    """Get the networks for the given ISO country codes, collapsed losslessly."""
    networks: list[IPNetwork] = []
    with maxminddb.open_database(DB_PATH) as reader:
        for network, record in reader:
            if not isinstance(record, dict):
                continue
            # Some ranges (e.g. anycast) have no geolocated country, only the registered one
            country = record.get("country") or record.get("registered_country")
            if isinstance(country, dict) and country.get("iso_code") in country_codes:
                networks.append(network)

    # collapse_addresses refuses mixed IP versions, so collapse each separately
    return [
        *ipaddress.collapse_addresses(n for n in networks if isinstance(n, ipaddress.IPv4Network)),
        *ipaddress.collapse_addresses(n for n in networks if isinstance(n, ipaddress.IPv6Network)),
    ]


def _atomic_write(path: Path, data: bytes) -> None:
    """Write via a temp file + rename so readers (nginx reload) never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_bytes(data)
    tmp_path.chmod(0o644)
    tmp_path.replace(path)


def main() -> None:
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(description="Generate GeoIP allowlist for Nginx.")
    parser.add_argument("--output", type=Path, help="Path to save the nginx conf file", default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--countries", nargs="+", type=str.upper, help="List of country codes to allow", required=True)
    args = parser.parse_args()

    output_path: Path = args.output
    countries: set[str] = set(args.countries)
    del args  # Stop me using args directly

    invalid = sorted(c for c in countries if len(c) != 2 or not c.isalpha())  # noqa: PLR2004 ISO 3166-1 alpha-2
    if invalid:
        parser.error(f"Invalid country code(s): {', '.join(invalid)}. Must be 2-letter ISO codes.")

    console.log(f"Maxmind Account ID: [bold green]{MAXMIND_ACCOUNT_ID}[/]")
    console.log(f"Maxmind License Key: [bold green]{MAXMIND_LICENSE_KEY[:4] + '*' * (len(MAXMIND_LICENSE_KEY) - 4)}[/]")
    console.log(f"Output file: [bold green]{output_path}[/]")
    console.log(f"Allowed countries: [bold green]{', '.join(sorted(countries))}[/]")

    download_geolite2_db()
    networks = get_country_networks(countries)

    # An empty allowlist next to `deny all;` locks everyone out, so never write one
    if not networks:
        sys.exit("No networks found for the given countries. Not writing an empty allowlist.")

    console.log(f"Writing {len(networks)} networks to {output_path}...")
    _atomic_write(output_path, "".join(f"{net} 1;\n" for net in networks).encode())


if __name__ == "__main__":
    main()
