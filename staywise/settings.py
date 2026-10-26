"""Agent settings from the environment (`.env.thunder` from the Thunder setup script, then `.env`)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from staywise.auth.thunder import ClientCredentials
from staywise.envfile import PROJECT_ROOT

DEFAULT_THUNDER = "http://default-idp.amp.localhost:8080"
DEFAULT_RESOURCE = "urn:staywise:hotel-api"
DEFAULT_PUBLIC_URL = "http://localhost:9100"


class SettingsError(ValueError):
    """Required settings are missing."""


@dataclass(frozen=True)
class Settings:
    """Everything the agent process needs to start."""

    public_url: str
    ui_url: str
    thunder_base: str
    thunder_internal_url: str | None
    resource: str
    web_client: ClientCredentials
    stepup_client: ClientCredentials
    agent_client: ClientCredentials
    mock_phone_key: str
    use_actor_token: bool
    actor_scopes: frozenset[str]
    mcp_config: Path
    policy_path: Path
    web_dist: Path

    @property
    def base_path(self) -> str:
        """The path prefix the app is served under ("" at the root, "/staywise" behind Agent Manager's route)."""
        return urlsplit(self.public_url).path.rstrip("/")

    @property
    def login_redirect_uri(self) -> str:
        """Where Thunder returns after sign-in."""
        return f"{self.public_url}/auth/callback"

    @property
    def stepup_redirect_uri(self) -> str:
        """Where Thunder returns after a step-up."""
        return f"{self.public_url}/auth/stepup/callback"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build from environment variables.

        Raises:
            SettingsError: naming every missing client credential.
        """
        values = os.environ if env is None else env
        required = [
            "WEB_CLIENT_ID",
            "WEB_CLIENT_SECRET",
            "STEPUP_CLIENT_ID",
            "STEPUP_CLIENT_SECRET",
            "AGENT_CLIENT_ID",
            "AGENT_CLIENT_SECRET",
            "MOCK_PHONE_KEY",
        ]
        missing = [name for name in required if not values.get(name)]
        if missing:
            raise SettingsError(
                f"missing {', '.join(missing)}; run wso2/thunder/thunder-setup.sh to create .env.thunder"
            )
        public_url = values.get("PUBLIC_URL", DEFAULT_PUBLIC_URL).rstrip("/")
        return cls(
            public_url=public_url,
            ui_url=values.get("UI_URL", public_url).rstrip("/"),
            thunder_base=values.get("THUNDER_BASE", DEFAULT_THUNDER).rstrip("/"),
            # Back-channel (token, JWKS) when the browser-facing THUNDER_BASE is not reachable from here,
            # e.g. the in-cluster service from a pod in Agent Manager.
            thunder_internal_url=values.get("THUNDER_INTERNAL_URL", "").rstrip("/") or None,
            resource=values.get("THUNDER_RESOURCE", DEFAULT_RESOURCE),
            web_client=ClientCredentials(values["WEB_CLIENT_ID"], values["WEB_CLIENT_SECRET"]),
            stepup_client=ClientCredentials(values["STEPUP_CLIENT_ID"], values["STEPUP_CLIENT_SECRET"]),
            agent_client=ClientCredentials(values["AGENT_CLIENT_ID"], values["AGENT_CLIENT_SECRET"]),
            mock_phone_key=values["MOCK_PHONE_KEY"],
            use_actor_token=values.get("OBO_ACTOR_TOKEN", "true").lower() != "false",
            # The actor token shape verified live against Thunder 1.0.0: the agent's own token for ops:notify.
            actor_scopes=frozenset(values.get("ACTOR_TOKEN_SCOPES", "ops:notify").split()),
            mcp_config=Path(values.get("MCP_CONFIG", PROJECT_ROOT / "config" / "mcp_servers.json")),
            policy_path=Path(values.get("STEPUP_POLICY", PROJECT_ROOT / "config" / "stepup_policy.yaml")),
            web_dist=Path(values.get("WEB_DIST", PROJECT_ROOT / "web" / "dist")),
        )
