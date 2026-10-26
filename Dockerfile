# StayWise agent: the backend plus its web UI, for an Agent Manager platform-hosted agent (amp-docker).
# Agent Manager runs it as user 65534 on a read-only root filesystem (/tmp is writable), so nothing here
# writes at runtime. Configuration comes from the environment; see README "Host it in Agent Manager".

FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
# Dependencies only; the code runs from /app so config/ and web/dist resolve next to it.
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" \
        > /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt
COPY staywise/ staywise/
COPY config/ config/
COPY --from=web /web/dist web/dist

# Hosted, the MCP servers are always reached through the WSO2 gateway.
ENV MCP_CONFIG=config/mcp_servers.wso2.json
USER 65534:65534
EXPOSE 9100
CMD ["python", "-m", "staywise", "--host", "0.0.0.0", "--port", "9100"]
