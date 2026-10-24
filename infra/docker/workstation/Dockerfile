FROM node:22-bookworm-slim AS build

WORKDIR /src

COPY app/workstation/package.json app/workstation/package-lock.json ./
RUN npm ci

COPY app/workstation/ ./
RUN npm run build

FROM nginx:1.27-alpine

COPY infra/docker/web/spa.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html

EXPOSE 80
