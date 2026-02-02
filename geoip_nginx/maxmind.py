"""MaxMind database download and query utilities."""

import io
import sys
import tarfile
from datetime import datetime, timedelta
from pathlib import Path

from dataclasses import dataclass
import maxminddb
import requests
from requests.auth import HTTPBasicAuth
from rich.console import Console

console = Console()

REQUESTS_TIMEOUT = 10
COUNTRY_CODE_LENGTH = 2


@dataclass
class FoundASN:
    """Found ASN information."""

    country: str
    asn_number: int
    organization: str
    range_count: int = 0
    address_count: int = 0


class MaxMindHandler:
    """Handler for MaxMind GeoLite2 databases."""

    DOWNLOAD_URL_COUNTRY = "https://download.maxmind.com/geoip/databases/GeoLite2-Country/download?suffix=tar.gz"
    DOWNLOAD_URL_ASN = "https://download.maxmind.com/geoip/databases/GeoLite2-ASN/download?suffix=tar.gz"

    def __init__(
        self,
        account_id: str,
        license_key: str,
        db_base_path: Path,
        max_age: timedelta = timedelta(days=7),
    ):
        """Initialize the MaxMind handler and download databases.

        Args:
            account_id: MaxMind account ID for authentication
            license_key: MaxMind license key for authentication
            db_base_path: Base directory where databases will be stored
            max_age: Maximum age before databases are considered outdated
        """
        self.account_id = account_id
        self.license_key = license_key
        self.db_base_path = Path(db_base_path)
        self.max_age = max_age

        # Define database paths
        self.db_path_country = self.db_base_path / "GeoLite2-Country.mmdb"
        self.db_path_asn = self.db_base_path / "GeoLite2-ASN.mmdb"

        # Download both databases on initialization
        self._download_databases()

    def _download_databases(self) -> None:
        """Download both Country and ASN databases."""
        # Download Country database
        if not self._download_db(
            download_url=self.DOWNLOAD_URL_COUNTRY,
            db_path=self.db_path_country,
            mmdb_filename="GeoLite2-Country.mmdb",
        ):
            console.log("[bold red]Failed to download GeoLite2-Country database.[/]")
            sys.exit(1)

        # Download ASN database
        if not self._download_db(
            download_url=self.DOWNLOAD_URL_ASN,
            db_path=self.db_path_asn,
            mmdb_filename="GeoLite2-ASN.mmdb",
        ):
            console.log("[bold red]Failed to download GeoLite2-ASN database.[/]")
            sys.exit(1)

    def _download_db(
        self,
        download_url: str,
        db_path: Path,
        mmdb_filename: str,
    ) -> bool:
        """Download a MaxMind database if it's missing or outdated.

        Args:
            download_url: The URL to download the database from
            db_path: Path where the database should be saved
            mmdb_filename: Name of the .mmdb file inside the tar.gz archive

        Returns:
            True if the database is available (already up to date or successfully downloaded)
        """
        # Ensure parent directory exists
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # Check if database exists and is recent enough
        if db_path.is_file() and db_path.stat().st_mtime >= (datetime.now() - self.max_age).timestamp():
            console.log(f"{db_path.name} is up to date at {db_path}. Skipping download.")
            return True

        console.log(f"Downloading {mmdb_filename} from MaxMind...")
        auth = HTTPBasicAuth(self.account_id, self.license_key)
        response = requests.get(download_url, auth=auth, timeout=REQUESTS_TIMEOUT)
        response.raise_for_status()

        with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tar:
            for member in tar.getmembers():
                if member.name.endswith(mmdb_filename):
                    member.name = Path(member.name).name
                    tar.extract(member, path=db_path.parent)
                    extracted_path = db_path.parent / member.name
                    extracted_path.rename(db_path)
                    console.log(f"Successfully downloaded {mmdb_filename} to {db_path}")
                    return True

        console.log(f"[bold red]Failed to find {mmdb_filename} in the downloaded archive.[/]")
        return False

    def get_country_ip_ranges(self, country_codes: list[str]) -> list[str]:
        """Get IP ranges for given country codes from the GeoLite2-Country database.

        Args:
            country_codes: List of 2-letter ISO country codes

        Returns:
            List of IP ranges (CIDR notation) for the specified countries
        """
        # Validate country codes
        for country_code in country_codes:
            if len(country_code) != COUNTRY_CODE_LENGTH:
                console.log(f"[bold red]Invalid country code: {country_code}. Must be a 2-letter ISO code.[/]")

        found_asns: dict[str, FoundASN] = {}

        ranges = []
        with (
            maxminddb.open_database(self.db_path_country) as country_reader,
            maxminddb.open_database(self.db_path_asn) as asn_reader,
        ):
            for network, record in country_reader:
                if not record:
                    continue

                country = record.get("country")

                if not country:
                    continue

                if country.get("iso_code") in [country_code.upper() for country_code in country_codes]:
                    ranges.append(str(network))

                    # Look up ASN information for this network
                    network_address_count = network.num_addresses

                    asn_record = asn_reader.get(network.network_address)
                    if asn_record:
                        asn_number = asn_record.get("autonomous_system_number")
                        assert asn_number is None or isinstance(asn_number, int)
                        if asn_number:
                            asn_org = asn_record.get("autonomous_system_organization", "Unknown")
                            asn_info = f"AS{asn_number} - {asn_org}"
                            if asn_info not in found_asns:
                                found_asns[asn_info] = FoundASN(
                                    country=country.get("iso_code"),
                                    asn_number=asn_number,
                                    organization=asn_org,
                                    range_count=1,
                                    address_count=network_address_count,
                                )
                            else:
                                found_asns[asn_info].range_count += 1
                                found_asns[asn_info].address_count += network_address_count

        if found_asns:
            # Sort by address count (descending)
            sorted_asns = sorted(found_asns.values(), key=lambda x: x.address_count, reverse=True)
            console.log("\n[bold cyan]Found ASNs (sorted by address count):[/]")
            for asn in sorted_asns:
                console.log(
                    f"  AS{asn.asn_number} - {asn.organization} ({asn.country}): {asn.range_count} ranges, {asn.address_count:,} addresses"
                )
        return ranges

    def get_asn_ip_ranges(self, asn_numbers: list[int]) -> list[str]:
        """Get IP ranges for given ASN numbers from the GeoLite2-ASN database.

        Args:
            asn_numbers: List of Autonomous System Numbers

        Returns:
            List of IP ranges (CIDR notation) for the specified ASNs
        """
        ranges = []
        with maxminddb.open_database(self.db_path_asn) as reader:
            for network, record in reader:
                if not record:
                    continue

                asn = record.get("autonomous_system_number")

                if not asn:
                    continue

                if asn in asn_numbers:
                    ranges.append(str(network))

        return ranges
