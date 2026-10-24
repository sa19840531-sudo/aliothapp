FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /workspace

COPY pyproject.toml README.md ./
COPY services ./services

RUN pip install --no-cache-dir -e ".[dev]" \
    && pip install --no-cache-dir --pre agent-framework-devui

EXPOSE 8080

CMD ["sh", "-c", "devui services/api/app/agents/devui_entities --host 0.0.0.0 --port 8080 --mode developer --auth-token \"${DEVUI_AUTH_TOKEN:-change-me-local-only}\" --instrumentation"]
