import io
import os
import sys
import tarfile
from contextlib import contextmanager
from ipaddress import ip_network
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import geoip_nginx.__main__ as m

AU = {"country": {"iso_code": "AU"}}
NZ = {"country": {"iso_code": "NZ"}}
AU_REG = {"registered_country": {"iso_code": "AU"}}  # anycast: no geolocated country


def fake_db(monkeypatch: pytest.MonkeyPatch, rows: list[tuple[str, object]]) -> None:
    @contextmanager
    def open_database(_path: Path):  # noqa: ANN202
        yield [(ip_network(n), r) for n, r in rows]

    monkeypatch.setattr(m.maxminddb, "open_database", open_database)


def nets(*cidrs: str) -> list:
    return [ip_network(c) for c in cidrs]


# subtract_networks


def test_subtract_splits_partial_overlap() -> None:
    assert m.subtract_networks(nets("10.0.0.0/24"), nets("10.0.0.0/25")) == nets("10.0.0.128/25")
    assert m.subtract_networks(nets("10.0.0.0/24"), nets("10.0.0.64/26")) == nets("10.0.0.0/26", "10.0.0.128/25")


def test_subtract_full_and_none() -> None:
    assert m.subtract_networks(nets("10.0.0.0/24"), nets("10.0.0.0/16")) == []
    assert m.subtract_networks(nets("10.0.0.0/24"), nets("192.168.0.0/24")) == nets("10.0.0.0/24")


def test_subtract_mixed_versions() -> None:
    allowed = nets("10.0.0.0/24", "2001:db8::/32")
    excluded = nets("10.0.0.0/25", "2001:db8::/33")
    assert m.subtract_networks(allowed, excluded) == nets("10.0.0.128/25", "2001:db8:8000::/33")


def test_subtract_multiple_cuts_in_one_network() -> None:
    result = m.subtract_networks(nets("10.0.0.0/24"), nets("10.0.0.0/26", "10.0.0.128/26"))
    assert result == nets("10.0.0.64/26", "10.0.0.192/26")


# get_*_networks


def test_get_country_networks(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_db(
        monkeypatch,
        [
            ("10.0.0.0/25", AU),
            ("10.0.0.128/25", AU_REG),
            ("10.0.1.0/24", NZ),
            ("10.0.2.0/24", "not a dict"),
            ("2001:db8::/32", AU),
        ],
    )
    assert m.get_country_networks(Path("x"), {"AU"}) == nets("10.0.0.0/24", "2001:db8::/32")


def test_get_asn_networks(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_db(
        monkeypatch,
        [
            ("10.0.0.0/24", {"autonomous_system_number": 1}),
            ("10.0.1.0/24", {"autonomous_system_number": 2}),
            ("10.0.2.0/24", None),
        ],
    )
    assert m.get_asn_networks(Path("x"), {1, 999}) == nets("10.0.0.0/24")


# load_config


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_load_config(tmp_path: Path) -> None:
    path = write_config(tmp_path, 'countries = ["au", "NZ"]\nexclude_asns = [1, 2]')
    assert m.load_config(path) == ({"AU", "NZ"}, {1, 2})
    assert m.load_config(write_config(tmp_path, 'countries = ["AU"]')) == ({"AU"}, set())


@pytest.mark.parametrize(
    "text",
    [
        "",
        "countries = []",
        'countries = "AU"',
        'countries = ["AUS"]',
        'countries = ["A1"]',
        'countries = ["AU"]\nexclude_asns = ["AS1"]',
        'countries = ["AU"]\nexclude_asns = [0]',
        'countries = ["AU"]\nexclude_asns = [true]',
        'countries = ["AU"]\nexclude_asns = 1',
    ],
)
def test_load_config_invalid(tmp_path: Path, text: str) -> None:
    with pytest.raises(SystemExit):
        m.load_config(write_config(tmp_path, text))


# _atomic_write


def test_atomic_write(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "out.conf"
    m._atomic_write(path, b"hello")
    assert path.read_bytes() == b"hello"
    assert path.stat().st_mode & 0o777 == 0o644
    assert not path.with_name("out.conf.tmp").exists()


# download_geolite2_db


def make_tarball(name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def db_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(m, "DB_DIR", tmp_path)
    monkeypatch.setattr(m, "MAXMIND_ACCOUNT_ID", "id")
    monkeypatch.setattr(m, "MAXMIND_LICENSE_KEY", "key")
    return tmp_path


def mock_get(monkeypatch: pytest.MonkeyPatch, content: bytes) -> MagicMock:
    get = MagicMock(return_value=MagicMock(content=content))
    monkeypatch.setattr(m.httpx, "get", get)
    return get


def test_download_skips_fresh_db(db_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (db_dir / "GeoLite2-Country.mmdb").write_bytes(b"old")
    get = mock_get(monkeypatch, b"")
    assert m.download_geolite2_db("GeoLite2-Country") == db_dir / "GeoLite2-Country.mmdb"
    get.assert_not_called()


def test_download_replaces_stale_db(db_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = db_dir / "GeoLite2-Country.mmdb"
    db.write_bytes(b"old")
    os.utime(db, (0, 0))
    mock_get(monkeypatch, make_tarball("GeoLite2-Country_20260101/GeoLite2-Country.mmdb", b"new"))
    assert m.download_geolite2_db("GeoLite2-Country") == db
    assert db.read_bytes() == b"new"


def test_download_missing_from_archive(db_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del db_dir
    mock_get(monkeypatch, make_tarball("README.txt", b"nope"))
    with pytest.raises(SystemExit):
        m.download_geolite2_db("GeoLite2-Country")


def test_download_needs_credentials(db_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del db_dir
    monkeypatch.setattr(m, "MAXMIND_LICENSE_KEY", "")
    with pytest.raises(SystemExit):
        m.download_geolite2_db("GeoLite2-Country")


# main


def run_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config: str, rows: list) -> Path:
    out = tmp_path / "out.conf"
    monkeypatch.setattr(m, "MAXMIND_LICENSE_KEY", "abcdefgh")
    monkeypatch.setattr(m, "download_geolite2_db", Path)
    fake_db(monkeypatch, rows)
    argv = ["geoip-nginx", "--config", str(write_config(tmp_path, config)), "--output", str(out)]
    monkeypatch.setattr(sys, "argv", argv)
    m.main()
    return out


def test_main_writes_allowlist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # One fake DB serves as both Country and ASN DB
    rows = [("10.0.0.0/24", AU | {"autonomous_system_number": 1}), ("10.0.1.0/24", NZ)]
    out = run_main(monkeypatch, tmp_path, 'countries = ["AU", "NZ"]\nexclude_asns = [2]', rows)
    assert out.read_text() == "10.0.0.0/23 1;\n"


def test_main_never_writes_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rows = [("10.0.0.0/24", AU | {"autonomous_system_number": 1})]
    with pytest.raises(SystemExit):
        run_main(monkeypatch, tmp_path, 'countries = ["AU"]\nexclude_asns = [1]', rows)
    assert not (tmp_path / "out.conf").exists()
