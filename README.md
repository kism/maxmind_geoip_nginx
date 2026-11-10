# geoip-nginx

Generates an nginx `geo` include of the networks for a set of countries, from the MaxMind GeoLite2-Country database.

```sh
# .env or environment: MAXMIND_ACCOUNT_ID, MAXMIND_LICENSE_KEY
uv run geoip-nginx --countries AU NZ   # writes /etc/nginx/ipallowlist_maxmind_geoip.conf
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
