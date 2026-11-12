# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Single-module CLI (`geoip_nginx/__main__.py`) that downloads the MaxMind GeoLite2-Country (and, if ASNs are excluded, GeoLite2-ASN) DB and writes an nginx `geo` include (`<cidr> 1;` per line) for the configured ISO country codes minus networks of excluded ASNs. Config is TOML (`countries`, `exclude_asns`), default `./config.toml`, see `config.example.toml`. See README.md for the nginx side.

## Commands

```sh
uv sync                                   # install deps (Python >=3.14)
uv run geoip-nginx --config config.example.toml --output ./output.conf   # run without touching /etc/nginx
uv run ruff check . && uv run ruff format .
uv run ty check
uv run pytest                             # tests + coverage report
```

## Flow and invariants

- Credentials come from `MAXMIND_ACCOUNT_ID` / `MAXMIND_LICENSE_KEY` (env or `.env` via dotenv); only needed when the DB must be downloaded.
- DBs live in `/usr/share/GeoIP/<edition>.mmdb`; each re-downloaded only if missing or older than 7 days (mtime). Running needs write access there and to the output path (default `/etc/nginx/ipallowlist_maxmind_geoip.conf`).
- Country match falls back to `registered_country` when `country` is absent (anycast ranges).
- Networks are collapsed per IP version (`ipaddress.collapse_addresses` rejects mixed versions).
- ASN exclusion is range subtraction (`subtract_networks`), so partially excluded networks are re-split into CIDRs; output stays `1`-only, no `0` overrides.
- All writes go through `_atomic_write` (tmp + rename, chmod 644) so nginx never reads a partial file.
- Never write an empty allowlist — it would lock everyone out. Exit instead.

## Style

Ruff with `select = ["ALL"]`, line length 120, Google docstrings. Keep code passing that.
