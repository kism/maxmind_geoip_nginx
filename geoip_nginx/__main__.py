"""Main module for GeoIP Nginx allowlist generation."""

import argparse
import bisect
import io
import ipaddress
import logging
import os
import sys
import tarfile
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import maxminddb
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

MAXMIND_ACCOUNT_ID = os.getenv("MAXMIND_ACCOUNT_ID", "")
MAXMIND_LICENSE_KEY = os.getenv("MAXMIND_LICENSE_KEY", "")

DOWNLOAD_URL = "https://download.maxmind.com/geoip/databases/{edition}/download?suffix=tar.gz"

DB_DIR = Path("/usr/share/GeoIP")
DB_MAX_AGE = timedelta(days=7)

DEFAULT_CONFIG_PATH = Path("config.toml")
DEFAULT_OUTPUT_PATH = Path("/etc/nginx/ipallowlist_maxmind_geoip.conf")

REQUESTS_TIMEOUT = 10

type IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def download_geolite2_db(edition: str) -> Path:
    """Download a GeoLite2 database (e.g. GeoLite2-Country) if it's missing or outdated, return its path."""
    db_path = DB_DIR / f"{edition}.mmdb"
    if db_path.is_file() and db_path.stat().st_mtime >= (datetime.now() - DB_MAX_AGE).timestamp():
        logger.info("%s database is up to date at %s. Skipping download.", edition, db_path)
        return db_path

    if not (MAXMIND_ACCOUNT_ID and MAXMIND_LICENSE_KEY):
        sys.exit("MAXMIND_ACCOUNT_ID and MAXMIND_LICENSE_KEY must be set to download the database.")

    response = httpx.get(
        DOWNLOAD_URL.format(edition=edition),
        auth=(MAXMIND_ACCOUNT_ID, MAXMIND_LICENSE_KEY),
        timeout=REQUESTS_TIMEOUT,
        follow_redirects=True,
    )
    response.raise_for_status()

    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tar:
        member = next((m for m in tar.getmembers() if m.name.endswith(db_path.name)), None)
        db_file = tar.extractfile(member) if member else None
        if db_file is None:
            sys.exit(f"{db_path.name} not found in the downloaded archive.")
        _atomic_write(db_path, db_file.read())

    logger.info("Downloaded %s database to %s.", edition, db_path)
    return db_path


def get_country_networks(db_path: Path, country_codes: set[str]) -> list[IPNetwork]:
    """Get the networks for the given ISO country codes, collapsed losslessly."""
    networks: list[IPNetwork] = []
    with maxminddb.open_database(db_path) as reader:
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


def get_asn_networks(db_path: Path, asns: set[int]) -> list[IPNetwork]:
    """Get the networks announced by the given AS numbers."""
    networks: list[IPNetwork] = []
    found: set[int] = set()
    with maxminddb.open_database(db_path) as reader:
        for network, record in reader:
            asn = record.get("autonomous_system_number") if isinstance(record, dict) else None
            if asn in asns:
                networks.append(network)
                found.add(asn)

    for asn in sorted(asns - found):
        logger.warning("AS%d has no networks in the ASN database, typo?", asn)
    return networks


def subtract_networks(allowed: list[IPNetwork], excluded: list[IPNetwork]) -> list[IPNetwork]:
    """Remove every address in excluded from allowed, re-splitting partially excluded networks into CIDRs."""
    v4, v6 = ipaddress.IPv4Network, ipaddress.IPv6Network
    return [
        *_subtract([n for n in allowed if isinstance(n, v4)], [n for n in excluded if isinstance(n, v4)]),
        *_subtract([n for n in allowed if isinstance(n, v6)], [n for n in excluded if isinstance(n, v6)]),
    ]


def _subtract[N: (ipaddress.IPv4Network, ipaddress.IPv6Network)](
    allowed: list[N], excluded: list[N]
) -> list[IPNetwork]:
    """Range subtraction for a single IP version."""
    result: list[IPNetwork] = []
    # Collapsed networks are sorted and disjoint, so their end addresses are sorted too
    cuts = list(ipaddress.collapse_addresses(excluded))
    cut_starts = [int(c.network_address) for c in cuts]
    cut_ends = [int(c.broadcast_address) for c in cuts]
    for net in ipaddress.collapse_addresses(allowed):
        addr = type(net.network_address)
        start, end = int(net.network_address), int(net.broadcast_address)
        i = bisect.bisect_left(cut_ends, start)  # first cut that could overlap
        while i < len(cuts) and cut_starts[i] <= end:
            if cut_starts[i] > start:
                result.extend(ipaddress.summarize_address_range(addr(start), addr(cut_starts[i] - 1)))
            start = cut_ends[i] + 1
            i += 1
        if start <= end:
            result.extend(ipaddress.summarize_address_range(addr(start), addr(end)))
    return result


def load_config(config_path: Path) -> tuple[set[str], set[int]]:
    """Load and validate the countries and excluded ASNs from the TOML config."""
    with config_path.open("rb") as f:
        config = tomllib.load(f)

    countries = config.get("countries")
    if not isinstance(countries, list) or not countries:
        sys.exit(f"{config_path}: `countries` must be a non-empty list of ISO country codes.")
    countries = {str(c).upper() for c in countries}
    invalid = sorted(c for c in countries if len(c) != 2 or not c.isalpha())  # noqa: PLR2004 ISO 3166-1 alpha-2
    if invalid:
        sys.exit(f"{config_path}: invalid country code(s): {', '.join(invalid)}. Must be 2-letter ISO codes.")

    exclude_asns = config.get("exclude_asns", [])
    if not isinstance(exclude_asns, list) or not all(type(a) is int and a > 0 for a in exclude_asns):
        sys.exit(f"{config_path}: `exclude_asns` must be a list of AS numbers, e.g. [16509, 14061].")

    return countries, set(exclude_asns)


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
    parser.add_argument("--config", type=Path, help="Path to the TOML config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("-v", "--verbose", action="store_true", help="Show debug messages")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    output_path: Path = args.output
    countries, exclude_asns = load_config(args.config)
    del args  # Stop me using args directly

    logger.debug("Maxmind Account ID: %s", MAXMIND_ACCOUNT_ID)
    logger.debug("Maxmind License Key: %s", MAXMIND_LICENSE_KEY[:4] + "*" * (len(MAXMIND_LICENSE_KEY) - 4))
    logger.info("Output file: %s", output_path)
    logger.info("Allowed countries: %s", ", ".join(sorted(countries)))
    logger.info("Excluded ASNs: %s", ", ".join(f"AS{a}" for a in sorted(exclude_asns)) or "none")

    networks = get_country_networks(download_geolite2_db("GeoLite2-Country"), countries)
    if exclude_asns:
        excluded = get_asn_networks(download_geolite2_db("GeoLite2-ASN"), exclude_asns)
        logger.info("Excluding %d networks from %d ASNs...", len(excluded), len(exclude_asns))
        networks = subtract_networks(networks, excluded)

    # An empty allowlist next to `deny all;` locks everyone out, so never write one
    if not networks:
        sys.exit("No networks found for the given countries. Not writing an empty allowlist.")

    logger.info("Writing %d networks to %s...", len(networks), output_path)
    _atomic_write(output_path, "".join(f"{net} 1;\n" for net in networks).encode())


if __name__ == "__main__":
    main()
