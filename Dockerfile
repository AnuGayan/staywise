# Concierge agent image for WSO2 Agent Manager. The MCP servers and Ollama run on the Mac host;
# inside k3d the host is reachable at 192.168.5.2 (host.k3d.internal does not resolve there).
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY concierge ./concierge
COPY servers ./servers
COPY mcp_servers.remote.json mcp_servers.gateway.json ./
COPY config ./config
RUN pip install --no-cache-dir ".[amp]"

ENV MCP_HOST=192.168.5.2 \
    OLLAMA_HOST=http://192.168.5.2:11434 \
    OLLAMA_MODEL=qwen3:1.7b \
    CONCIERGE_MEMBERS=/app/config/members.json
# Tracing: Agent Manager injects AMP_OTEL_ENDPOINT and AMP_AGENT_API_KEY; amp-instrument exports
# OTLP traces there (Ollama and MCP calls via Traceloop). AMP_TRACE_CONTENT=false keeps prompts and
# tool results out of the spans.
# Sign-in: set THUNDER_ISSUER, CONCIERGE_CLIENT_SECRET and CONCIERGE_PUBLIC_URL from .env.thunder in the
# deployment, and register CONCIERGE_PUBLIC_URL with AGENT_PUBLIC_URLS when running thunder-setup.sh.

USER 10001
EXPOSE 9100
CMD ["amp-instrument", "hardrock-concierge", "--serve", "--host", "0.0.0.0", "--port", "9100", "-c", "/app/mcp_servers.remote.json"]
