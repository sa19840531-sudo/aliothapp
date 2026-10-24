FROM nginx:1.27-alpine

COPY infra/docker/gateway/nginx.conf /etc/nginx/conf.d/default.conf
COPY infra/docker/gateway/render-radar-vhost.sh /docker-entrypoint.d/40-render-radar-vhost.sh

RUN chmod +x /docker-entrypoint.d/40-render-radar-vhost.sh

EXPOSE 80 443 8088
