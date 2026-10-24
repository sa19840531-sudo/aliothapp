#!/bin/sh
set -eu

gateway_domain=${ALIOTH_GATEWAY_RADAR_DOMAIN:-xnwork.xyz}
gateway_radar_upstream=${ALIOTH_GATEWAY_RADAR_UPSTREAM:-http://host.docker.internal:8787}
gateway_acme_webroot=${ALIOTH_GATEWAY_ACME_WEBROOT:-/var/www/acme-challenge}
gateway_tls_fullchain=${ALIOTH_GATEWAY_TLS_FULLCHAIN:-/etc/letsencrypt/live/${gateway_domain}/fullchain.pem}
gateway_tls_privkey=${ALIOTH_GATEWAY_TLS_PRIVKEY:-/etc/letsencrypt/live/${gateway_domain}/privkey.pem}
gateway_extra_dir=/etc/nginx/alioth-extra

mkdir -p "${gateway_extra_dir}"
rm -f "${gateway_extra_dir}"/*.conf

cat > "${gateway_extra_dir}/radar-http.conf" <<EOF
server {
    listen 80;
    server_name ${gateway_domain};

    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options SAMEORIGIN always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;

    location ^~ /.well-known/acme-challenge/ {
        root ${gateway_acme_webroot};
        default_type text/plain;
        try_files \$uri =404;
    }
EOF

if [ -f "${gateway_tls_fullchain}" ] && [ -f "${gateway_tls_privkey}" ]; then
  cat >> "${gateway_extra_dir}/radar-http.conf" <<'EOF'
    location / {
        return 301 https://$host$request_uri;
    }
}
EOF

  cat > "${gateway_extra_dir}/radar-https.conf" <<EOF
server {
    listen 443 ssl http2;
    server_name ${gateway_domain};

    ssl_certificate ${gateway_tls_fullchain};
    ssl_certificate_key ${gateway_tls_privkey};
    ssl_session_timeout 1d;
    ssl_session_cache shared:AliothRadarTLS:10m;
    ssl_protocols TLSv1.2 TLSv1.3;

    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options SAMEORIGIN always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;

    location / {
        proxy_pass ${gateway_radar_upstream};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_connect_timeout 10s;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
        client_max_body_size 20m;
    }
}
EOF
else
  cat >> "${gateway_extra_dir}/radar-http.conf" <<EOF
    location / {
        proxy_pass ${gateway_radar_upstream};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_connect_timeout 10s;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
        client_max_body_size 20m;
    }
}
EOF
fi

nginx -t >/dev/null
