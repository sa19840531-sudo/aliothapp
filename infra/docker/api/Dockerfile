FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
# Official 云手机 / MoreLogin ADB 在容器内必须可调用 adb。
ENV ANDROID_HOME=/opt/android-sdk
ENV ANDROID_SDK_ROOT=/opt/android-sdk
ENV PATH="${ANDROID_HOME}/platform-tools:${PATH}"

WORKDIR /workspace

COPY pyproject.toml README.md ./
COPY services ./services
COPY infra/env/web-login-auth-origins.json ./infra/env/web-login-auth-origins.json
COPY infra/playwright-cli/package.json infra/playwright-cli/package-lock.json /opt/alioth-cli/
COPY infra/playwright-cli/patch-cdp-context.mjs /opt/alioth-cli/
ENV ALIOTH_PLAYWRIGHT_CLI_SCRIPT=/opt/alioth-cli/node_modules/@playwright/cli/playwright-cli.js

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl unzip ca-certificates nodejs npm tesseract-ocr tesseract-ocr-chi-sim \
    && node -e 'if (Number(process.versions.node.split(".")[0]) < 20) process.exit(1)' \
    && npm ci --prefix /opt/alioth-cli --ignore-scripts --no-audit --no-fund \
    && node /opt/alioth-cli/patch-cdp-context.mjs \
    && mkdir -p "${ANDROID_HOME}" \
    && curl -fsSL https://dl.google.com/android/repository/platform-tools-latest-linux.zip \
        -o /tmp/platform-tools.zip \
    && unzip -q /tmp/platform-tools.zip -d "${ANDROID_HOME}" \
    && rm /tmp/platform-tools.zip \
    && rm -rf /var/lib/apt/lists/* \
    && test -x "${ANDROID_HOME}/platform-tools/adb" \
    && pip install --no-cache-dir -e ".[dev]" \
    && python -m playwright install --with-deps chromium

COPY infra/docker/api/ensure-playwright.sh /usr/local/bin/ensure-playwright.sh
RUN chmod +x /usr/local/bin/ensure-playwright.sh

EXPOSE 8000

CMD ["uvicorn", "services.api.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
