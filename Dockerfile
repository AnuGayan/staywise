# Concierge agent image for WSO2 Agent Manager. MCP servers come from the variables Agent Manager sets for
# each attached MCP server (<PREFIX>_URL, <PREFIX>_API_KEY; prefixes in MCP_ENV_PREFIXES). The model is
# reached through the gateway's LLM provider (LLM_BASE_URL, LLM_API_KEY, LLM_API_KEY_HEADER).
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY concierge ./concierge
COPY servers ./servers
COPY mcp_servers.remote.json mcp_servers.gateway.json ./
COPY config ./config
RUN pip install --no-cache-dir ".[amp]"

ENV MCP_FROM_ENV=true \
    MCP_ENV_PREFIXES=HOTEL_INFO,RESERVATIONS,UNITY_LOYALTY \
    LLM_API_KEY_HEADER=X-API-Key \
    OLLAMA_MODEL=qwen3:1.7b \
    CONCIERGE_MEMBERS=/app/config/members.json
# Tracing: Agent Manager injects AMP_OTEL_ENDPOINT and AMP_AGENT_API_KEY; amp-instrument exports
# OTLP traces there (Ollama and MCP calls via Traceloop). AMP_TRACE_CONTENT=false keeps prompts and
# tool results out of the spans.
# Sign-in: set THUNDER_ISSUER, CONCIERGE_CLIENT_SECRET and CONCIERGE_PUBLIC_URL from .env.thunder in the
# deployment, and register CONCIERGE_PUBLIC_URL with AGENT_PUBLIC_URLS when running thunder-setup.sh.

USER 10001
EXPOSE 9100
CMD ["amp-instrument", "hardrock-concierge", "--serve", "--host", "0.0.0.0", "--port", "9100"]
