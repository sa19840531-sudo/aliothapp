# Mobile Demo Host Runner（Linux Docker 整合用）
# 內含 adb + airtest，經 Docker 網路連 alioth-redroid:5555

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV ANDROID_HOME=/opt/android-sdk
ENV ANDROID_SDK_ROOT=/opt/android-sdk
ENV PATH="${ANDROID_HOME}/platform-tools:${PATH}"

# OpenCV/Airtest 需要 libGL；slim 預設沒有
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl unzip ca-certificates \
        libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p "${ANDROID_HOME}" \
    && curl -fsSL https://dl.google.com/android/repository/platform-tools-latest-linux.zip -o /tmp/platform-tools.zip \
    && unzip -q /tmp/platform-tools.zip -d "${ANDROID_HOME}" \
    && rm /tmp/platform-tools.zip

WORKDIR /workspace

COPY pyproject.toml README.md ./
COPY services ./services

RUN pip install --no-cache-dir -e ".[dev]" \
    && pip install --no-cache-dir "airtest>=1.3,<2.0"

EXPOSE 8765

CMD ["python", "-m", "services.api.app.mobile_demo_host_runner"]
