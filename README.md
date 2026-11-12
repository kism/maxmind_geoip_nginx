# geoip-nginx

Generates an nginx `geo` include of the networks for a set of countries, from the MaxMind GeoLite2-Country database, minus any networks announced by excluded ASNs (GeoLite2-ASN), e.g. cloud and VPN hosting.

```sh
# .env or environment: MAXMIND_ACCOUNT_ID, MAXMIND_LICENSE_KEY
cp config.example.toml config.toml   # countries and exclude_asns
uv run geoip-nginx                   # reads ./config.toml, writes /etc/nginx/ipallowlist_maxmind_geoip.conf
uv run geoip-nginx --config /path/to/config.toml --output ./output.conf
```

nginx config:

```nginx
# http {}
geo $geoip_allowed {
    default 0;
    include /etc/nginx/ipallowlist_maxmind_geoip.conf;
}

# server {} or location {}
if ($geoip_allowed = 0) { return 403; }
```

Reload nginx after each run (`nginx -t && systemctl reload nginx`).
