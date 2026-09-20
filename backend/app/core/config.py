import os
from pathlib import Path
from typing import List, Optional
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent
ENV_FILE = BASE_DIR / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM Provider Configuration
    # Supported: vertex, anthropic, openai, ollama
    llm_provider: str = Field(default="vertex", min_length=1, description="Target LLM provider: vertex, anthropic, openai, or ollama")
    llm_model_name: str = Field(default="", description="Target LLM model name (defaults based on provider if empty)")

    # Google Cloud / Vertex AI (Required when llm_provider='vertex')
    google_cloud_project: str = Field(default="", description="GCP project ID")
    google_cloud_location: str = Field(default="", description="GCP region or location")
    google_application_credentials: str = Field(default="", description="Path to GCP service account key")
    vertex_model_name: str = Field(default="gemini-2.0-flash", description="Legacy Vertex model name (fallback for llm_model_name)")

    # Anthropic (Required when llm_provider='anthropic')
    anthropic_api_key: str = Field(default="", description="Anthropic API key (starts with sk-ant-)")

    # OpenAI (Required when llm_provider='openai')
    openai_api_key: str = Field(default="", description="OpenAI API key (starts with sk-)")

    # Ollama (Required when llm_provider='ollama')
    ollama_host: str = Field(default="http://localhost:11434", description="Ollama server host URL")
    # Backend Server & Networking (Strictly required)
    backend_host: str = Field(..., min_length=1, description="Backend listen host")
    backend_port: int = Field(..., ge=1, le=65535, description="Backend port (1-65535)")
    frontend_url: str = Field(..., min_length=1, description="Frontend base URL")
    cors_origins: str = Field(..., min_length=1, description="Allowed CORS origins, comma-separated")
    allowed_hosts: str = Field(..., min_length=1, description="Allowed Host headers for DNS rebinding protection")

    # Langfuse Observability (Host is strictly required; keys optional for offline dev)
    langfuse_public_key: str = Field(default="", description="Langfuse public API key")
    langfuse_secret_key: str = Field(default="", description="Langfuse secret API key")
    langfuse_host: str = Field(default="", description="Langfuse host URL (optional, leave empty for offline/disabled)")
    # Chromium / Playwright
    chrome_persistent_dir: str = Field(default="./.chrome_profile", min_length=1)
    default_headless: bool = Field(default=False)

    # Security & Tokens (Strictly required, Fail-Fast)
    api_token: str = Field(..., min_length=16, description="Bearer security token for authenticating frontend, extension, and CDP bridge")
    allow_routable_network: bool = Field(default=False, description="Explicit opt-in required to bind on non-loopback network interfaces (e.g. 0.0.0.0 or LAN IP)")
    allowed_extension_id: str = Field(default="ckfcbipidpedpdlijgmhmoapjlnfiaog", description="Exact Chrome Extension ID authorized to connect to the backend")
    default_target_url: str = Field(default="https://www.google.com")
    max_task_cost_usd: float = Field(default=1.50, gt=0.0)
    max_task_llm_steps: int = Field(default=30, ge=1)
    langfuse_default_user_id: str = Field(default="local-user@cobrowse.dev", min_length=1)
    stream_chunk_size: int = Field(default=4, ge=1)
    default_viewport_width: int = Field(default=1280, ge=320)
    default_viewport_height: int = Field(default=900, ge=240)
    cdp_virtual_session_id: str = Field(default="session-workspace-iframe-main", min_length=1)
    @field_validator("frontend_url")
    @classmethod
    def validate_http_urls(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed.startswith(("http://", "https://")):
            raise ValueError(f"URL must start with 'http://' or 'https://', got '{v}'")
        return trimmed.rstrip("/")

    @field_validator("langfuse_host")
    @classmethod
    def validate_langfuse_host(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            return ""
        if not trimmed.startswith(("http://", "https://")):
            raise ValueError(f"URL must start with 'http://' or 'https://', got '{v}'")
        return trimmed.rstrip("/")

    @model_validator(mode="after")
    def enforce_configuration_invariants(self) -> "Settings":
        """Enforces fail-fast configuration invariants for network confinement and LLM credentials."""
        # 1. Loopback Confinement Guard
        host = self.backend_host.strip().lower()
        loopback_hosts = {"127.0.0.1", "localhost", "::1"}
        is_loopback = host in loopback_hosts

        if not is_loopback and not self.allow_routable_network:
            raise ValueError(
                f"\n[SECURITY FAIL-FAST] Insecure network binding detected!\n"
                f"BACKEND_HOST is configured to '{self.backend_host}', exposing browser automation control to the local network or internet without loopback confinement.\n"
                f"To prevent accidental exposure:\n"
                f"  1. Either keep BACKEND_HOST='127.0.0.1' (recommended for local use),\n"
                f"  2. Or explicitly set ALLOW_ROUTABLE_NETWORK=true in your .env if running behind a secure TLS reverse proxy / containerized gateway (e.g. AWS Fargate)."
            )

        # 2. LLM Provider Fail-Fast Guard
        provider = self.llm_provider.strip().lower()
        valid_providers = {"vertex", "anthropic", "openai", "ollama"}
        if provider not in valid_providers:
            raise ValueError(
                f"\n[LLM CONFIG FAIL-FAST] Unsupported LLM_PROVIDER: '{self.llm_provider}'.\n"
                f"Must be one of: {', '.join(sorted(valid_providers))}."
            )

        if provider == "vertex":
            if not self.google_cloud_project.strip():
                raise ValueError("\n[LLM CONFIG FAIL-FAST] LLM_PROVIDER='vertex' requires GOOGLE_CLOUD_PROJECT to be configured.")
            if not self.google_application_credentials.strip():
                raise ValueError("\n[LLM CONFIG FAIL-FAST] LLM_PROVIDER='vertex' requires GOOGLE_APPLICATION_CREDENTIALS path to be configured.")
            creds_path = Path(self.google_application_credentials.strip())
            if not creds_path.exists():
                raise ValueError(
                    f"\n[LLM CONFIG FAIL-FAST] GOOGLE_APPLICATION_CREDENTIALS file not found at: '{self.google_application_credentials}'."
                )

        elif provider == "anthropic":
            key = self.anthropic_api_key.strip()
            # Fast configuration sanity check on key prefix
            if not key or not key.startswith("sk-ant-"):
                raise ValueError(
                    "\n[LLM CONFIG FAIL-FAST] LLM_PROVIDER='anthropic' requires ANTHROPIC_API_KEY starting with 'sk-ant-'."
                )

        elif provider == "openai":
            key = self.openai_api_key.strip()
            # Fast configuration sanity check on key prefix
            if not key or not (key.startswith("sk-") or key.startswith("sess-")):
                raise ValueError(
                    "\n[LLM CONFIG FAIL-FAST] LLM_PROVIDER='openai' requires OPENAI_API_KEY starting with 'sk-'."
                )

        elif provider == "ollama":
            host_val = self.ollama_host.strip()
            if not host_val or not host_val.startswith(("http://", "https://")):
                raise ValueError(
                    f"\n[LLM CONFIG FAIL-FAST] LLM_PROVIDER='ollama' requires OLLAMA_HOST starting with http:// or https:// (got '{self.ollama_host}')."
                )

        # Resolve effective model name
        if not self.llm_model_name.strip():
            defaults = {
                "vertex": self.vertex_model_name or "gemini-2.0-flash",
                "anthropic": "claude-3-5-sonnet-20241022",
                "openai": "gpt-4o",
                "ollama": "qwen2.5:32b",
            }
            self.llm_model_name = defaults.get(provider, "gemini-2.0-flash")

        return self
    @property
    def parsed_cors_origins(self) -> List[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def parsed_allowed_hosts(self) -> List[str]:
        return [host.strip().lower() for host in self.allowed_hosts.split(",") if host.strip()]
    @property
    def LANGFUSE_PUBLIC_KEY(self) -> str:
        return self.langfuse_public_key or os.getenv("LANGFUSE_PUBLIC_KEY", "")

    @property
    def LANGFUSE_SECRET_KEY(self) -> str:
        return self.langfuse_secret_key or os.getenv("LANGFUSE_SECRET_KEY", "")

    @property
    def LANGFUSE_HOST(self) -> str:
        return self.langfuse_host
    def setup_credentials(self) -> None:
        """Propagate environment variables for SDKs and Langfuse."""
        provider = self.llm_provider.strip().lower()
        if provider == "vertex":
            if self.google_cloud_project:
                os.environ["GOOGLE_CLOUD_PROJECT"] = self.google_cloud_project
            if self.google_cloud_location:
                os.environ["GOOGLE_CLOUD_LOCATION"] = self.google_cloud_location
            if self.google_application_credentials:
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = self.google_application_credentials
        elif provider == "anthropic":
            if self.anthropic_api_key:
                os.environ["ANTHROPIC_API_KEY"] = self.anthropic_api_key
        elif provider == "openai":
            if self.openai_api_key:
                os.environ["OPENAI_API_KEY"] = self.openai_api_key

        if self.LANGFUSE_PUBLIC_KEY:
            os.environ["LANGFUSE_PUBLIC_KEY"] = self.LANGFUSE_PUBLIC_KEY
        if self.LANGFUSE_SECRET_KEY:
            os.environ["LANGFUSE_SECRET_KEY"] = self.LANGFUSE_SECRET_KEY
        if self.LANGFUSE_HOST:
            os.environ["LANGFUSE_HOST"] = self.LANGFUSE_HOST
settings = Settings()
settings.setup_credentials()
