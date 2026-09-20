import json
import logging
import re
from contextvars import ContextVar
from typing import Any, Dict, List, Optional, Tuple, TypeVar

from pydantic import BaseModel
from app.core.config import settings

# Browser-use BaseChatModel protocol & types
from browser_use.llm.base import BaseChatModel

logger = logging.getLogger("cobrowse_agent.llm_gateway")

_trace_id_ctx: ContextVar[Optional[str]] = ContextVar("trace_id", default=None)
_session_id_ctx: ContextVar[Optional[str]] = ContextVar("session_id", default=None)
_user_id_ctx: ContextVar[Optional[str]] = ContextVar("user_id", default=settings.langfuse_default_user_id)
_span_ctx: ContextVar[Optional[Any]] = ContextVar("span", default=None)
_trace_obj_ctx: ContextVar[Optional[Any]] = ContextVar("trace_obj", default=None)
_trace_name_ctx: ContextVar[Optional[str]] = ContextVar("trace_name", default=None)
trace_name_ctx: ContextVar[Optional[str]] = _trace_name_ctx


def get_current_trace_name() -> Optional[str]:
    return _trace_name_ctx.get()


def set_current_trace_name(name: Optional[str]) -> None:
    _trace_name_ctx.set(name)


def get_current_trace_id() -> Optional[str]:
    return _trace_id_ctx.get()


def set_current_trace_id(trace_id: Optional[str]) -> None:
    _trace_id_ctx.set(trace_id)


def get_current_session_id() -> Optional[str]:
    return _session_id_ctx.get()


def set_current_session_id(session_id: Optional[str]) -> None:
    _session_id_ctx.set(session_id)


def get_current_user_id() -> Optional[str]:
    return _user_id_ctx.get()


def set_current_user_id(user_id: Optional[str]) -> None:
    _user_id_ctx.set(user_id)


def get_current_trace_obj() -> Optional[Any]:
    return _trace_obj_ctx.get()


def set_current_trace_obj(trace_obj: Optional[Any]) -> None:
    _trace_obj_ctx.set(trace_obj)


def get_current_span() -> Optional[Any]:
    return _span_ctx.get()


def set_current_span(span: Optional[Any]) -> None:
    _span_ctx.set(span)


# ==============================================================================
# Model Pricing Table & Canonical Family Normalization
# Rates in USD per million tokens (input, output)
# ==============================================================================

MODEL_FAMILY_PRICING: Dict[str, Dict[str, float]] = {
    # Anthropic
    "claude-3-5-sonnet": {"input": 3.00, "output": 15.00},
    "claude-3-5-haiku": {"input": 0.80, "output": 4.00},
    "claude-3-opus": {"input": 15.00, "output": 75.00},
    "claude-3-haiku": {"input": 0.25, "output": 1.25},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    # OpenAI
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4-turbo": {"input": 10.00, "output": 30.00},
    "o1": {"input": 15.00, "output": 60.00},
    "o1-mini": {"input": 3.00, "output": 12.00},
    "o3-mini": {"input": 1.10, "output": 4.40},
    # Google Vertex AI / Gemini
    "gemini-3.8-flash": {"input": 0.75, "output": 3.75},
    "gemini-3.7-flash": {"input": 0.75, "output": 3.75},
    "gemini-3.5-flash": {"input": 1.50, "output": 9.00},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
    "gemini-2.0-flash-lite": {"input": 0.075, "output": 0.30},
    "gemini-1.5-pro": {"input": 1.25, "output": 5.00},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30},
    # Ollama / Local Models
    "ollama": {"input": 0.00, "output": 0.00},
}

# Conservative default rates applied to unknown models to ensure kill-switch enforcement (Pessimistic safety ceiling)
CONSERVATIVE_FALLBACK_RATES = {"input": 3.00, "output": 15.00}


def normalize_model_family(raw_model_name: str, provider: str = "") -> str:
    """Normalizes versioned or prefixed model strings into canonical pricing family keys."""
    if provider.lower() == "ollama":
        return "ollama"

    clean = raw_model_name.lower().strip()
    if "/" in clean:
        clean = clean.split("/")[-1]

    # Anthropic versioned pattern: claude-3-5-sonnet-20241022 or anthropic.claude-3-5-sonnet-20241022-v2:0
    if re.search(r"claude-3-5-sonnet", clean):
        return "claude-3-5-sonnet"
    if re.search(r"claude-3-5-haiku", clean):
        return "claude-3-5-haiku"
    if re.search(r"claude-sonnet-5", clean):
        return "claude-sonnet-5"
    if re.search(r"claude-haiku-4-5", clean):
        return "claude-haiku-4-5"
    if re.search(r"claude-3-opus", clean):
        return "claude-3-opus"
    if re.search(r"claude-3-haiku", clean):
        return "claude-3-haiku"

    # OpenAI versioned pattern: gpt-4o-2024-08-06, gpt-4o-mini-2024-07-18
    if re.search(r"gpt-4o-mini", clean):
        return "gpt-4o-mini"
    if re.search(r"gpt-4o", clean):
        return "gpt-4o"
    if re.search(r"gpt-4-turbo", clean):
        return "gpt-4-turbo"
    if re.search(r"o3-mini", clean):
        return "o3-mini"
    if re.search(r"o1-mini", clean):
        return "o1-mini"
    if re.search(r"o1", clean):
        return "o1"

    # Google Gemini pattern: gemini-2.0-flash-exp, gemini-1.5-flash-001
    for fam in (
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.5-flash",
        "gemini-2.5-flash",
        "gemini-2.0-flash-lite",
        "gemini-2.0-flash",
        "gemini-1.5-pro",
        "gemini-1.5-flash",
    ):
        if fam in clean:
            return fam

    return clean


def get_model_rates(model_name: str, provider: str = "") -> Tuple[Dict[str, float], bool]:
    """Retrieves token pricing per million. Returns (rates, is_fallback)."""
    canonical_family = normalize_model_family(model_name, provider)
    if canonical_family in MODEL_FAMILY_PRICING:
        return MODEL_FAMILY_PRICING[canonical_family], False

    # Check direct match on raw model
    clean_raw = model_name.lower().strip()
    if clean_raw in MODEL_FAMILY_PRICING:
        return MODEL_FAMILY_PRICING[clean_raw], False

    logger.warning(
        f"[LLM Budget Guard] Unknown model '{model_name}'. Applied safety ceiling rate "
        f"(${CONSERVATIVE_FALLBACK_RATES['input']:.2f} in / ${CONSERVATIVE_FALLBACK_RATES['output']:.2f} out per MTok) for kill-switch enforcement. "
        f"Estimated cost may be higher than actual provider billing."
    )
    return CONSERVATIVE_FALLBACK_RATES, True


def calculate_token_cost(
    model_name: str,
    prompt_tokens: int,
    completion_tokens: int,
    prompt_cached_tokens: Optional[int] = 0,
    prompt_cache_creation_tokens: Optional[int] = 0,
    provider: str = "",
) -> Dict[str, Any]:
    """Estimates the cost of an LLM generation in USD, accounting for Anthropic cache read/write pricing."""
    rates, is_fallback = get_model_rates(model_name, provider)
    r_in = rates["input"]
    r_out = rates["output"]

    cached = max(0, prompt_cached_tokens or 0)
    created = max(0, prompt_cache_creation_tokens or 0)
    base_prompt = max(0, prompt_tokens - cached - created)
    completion = max(0, completion_tokens or 0)

    # Anthropic Pricing Rules:
    # Cache Read = 10% of base input rate
    # Cache Write (Creation) = 125% of base input rate
    cost_usd = (
        (base_prompt * r_in)
        + (cached * (r_in * 0.10))
        + (created * (r_in * 1.25))
        + (completion * r_out)
    ) / 1_000_000.0

    return {
        "cost_usd": round(cost_usd, 7),
        "input_cost_usd": round(((base_prompt * r_in) + (cached * (r_in * 0.10)) + (created * (r_in * 1.25))) / 1_000_000.0, 7),
        "output_cost_usd": round((completion * r_out) / 1_000_000.0, 7),
        "total_cost_usd": round(cost_usd, 7),
        "is_fallback": is_fallback,
        "base_prompt_tokens": base_prompt,
        "cached_tokens": cached,
        "cache_creation_tokens": created,
        "completion_tokens": completion,
    }


# ==============================================================================
# Universal ObservedChatModel Decorator
# ==============================================================================

class ObservedChatModel(BaseChatModel):
    """Universal decorator wrapping any browser-use BaseChatModel with Langfuse tracing and cost enforcement."""

    def __init__(
        self,
        inner_model: Any,
        provider_name: str,
        session_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        trace_obj: Optional[Any] = None,
    ):
        self.inner_model = inner_model
        self.provider_name = provider_name
        self.model = getattr(inner_model, "model", getattr(inner_model, "model_name", settings.llm_model_name))
        self.session_id = session_id
        self.trace_id = trace_id
        self.trace_obj = trace_obj
        self.current_span: Optional[Any] = None
        self.cumulative_cost_usd: float = 0.0

    @property
    def provider(self) -> str:
        return self.provider_name

    @property
    def name(self) -> str:
        return getattr(self.inner_model, "name", self.model)

    @property
    def model_name(self) -> str:
        return self.model

    async def ainvoke(self, messages: Any, output_format: Optional[Any] = None, **kwargs: Any) -> Any:
        langfuse_client = LLMGateway.get_langfuse_client()
        generation = None

        if langfuse_client:
            try:
                serialized_input = []
                for m in messages:
                    if hasattr(m, "content"):
                        role = getattr(m, "role", None) or getattr(m, "type", "user")
                        content = m.content
                        if isinstance(content, (str, int, float, bool)):
                            serialized_input.append({"role": role, "content": str(content)})
                        elif isinstance(content, list):
                            clean_parts = []
                            for part in content:
                                if isinstance(part, dict) and "image_url" in part:
                                    clean_parts.append({"type": "image_url", "url": "<base64_image_data>"})
                                else:
                                    clean_parts.append(part)
                            serialized_input.append({"role": role, "content": clean_parts})
                        else:
                            serialized_input.append({"role": role, "content": str(content)})
                    elif isinstance(m, dict):
                        serialized_input.append(m)
                    else:
                        serialized_input.append(str(m))

                target_parent = self.current_span or self.trace_obj or get_current_span() or get_current_trace_obj() or langfuse_client

                gen_kwargs = {
                    "name": "browser_use_llm_call",
                    "model": self.model_name,
                    "input": serialized_input,
                    "metadata": {
                        "provider": self.provider_name,
                        "model": self.model_name,
                    },
                }
                active_trace = self.trace_obj or get_current_trace_obj()
                active_trace_id = self.trace_id or getattr(active_trace, "id", None) or get_current_trace_id()

                if target_parent is langfuse_client:
                    if active_trace_id:
                        gen_kwargs["trace_id"] = active_trace_id
                    gen_kwargs["user_id"] = get_current_user_id() or settings.langfuse_default_user_id
                generation = target_parent.generation(**gen_kwargs)
            except Exception as e:
                logger.warning(f"Error creating Langfuse generation: {e}")

        try:
            result = await self.inner_model.ainvoke(messages, output_format=output_format, **kwargs)

            out_payload: Any = ""
            if hasattr(result, "completion"):
                out_payload = result.completion
            elif hasattr(result, "content"):
                out_payload = result.content
            elif hasattr(result, "model_dump"):
                out_payload = result.model_dump()
            else:
                out_payload = str(result)

            p_tokens = 0
            c_tokens = 0
            cached_tokens = 0
            cache_creation_tokens = 0
            has_usage_data = False

            # Extract usage safely: handle Ollama where result.usage is None
            if hasattr(result, "usage") and result.usage is not None:
                u = result.usage
                p_tokens = getattr(u, "prompt_tokens", 0) or 0
                c_tokens = getattr(u, "completion_tokens", 0) or 0
                cached_tokens = getattr(u, "prompt_cached_tokens", 0) or 0
                cache_creation_tokens = getattr(u, "prompt_cache_creation_tokens", 0) or 0
                has_usage_data = True
            elif hasattr(result, "usage_metadata") and result.usage_metadata is not None:
                u = result.usage_metadata
                p_tokens = u.get("input_tokens", 0) or 0
                c_tokens = u.get("output_tokens", 0) or 0
                has_usage_data = True

            # If provider omitted usage (e.g. streaming or Ollama text), estimate if non-local
            if not has_usage_data and self.provider_name != "ollama":
                in_chars = sum(len(str(getattr(m, "content", ""))) for m in (messages or []))
                out_chars = len(str(out_payload))
                p_tokens = max(1, in_chars // 4)
                c_tokens = max(1, out_chars // 4)
                has_usage_data = True

            costs = calculate_token_cost(
                self.model_name,
                prompt_tokens=p_tokens,
                completion_tokens=c_tokens,
                prompt_cached_tokens=cached_tokens,
                prompt_cache_creation_tokens=cache_creation_tokens,
                provider=self.provider_name,
            )

            call_cost = costs["total_cost_usd"]
            self.cumulative_cost_usd += call_cost

            logger.info(
                f"[LLM Usage] Provider: {self.provider_name} | Model: {self.model_name} | "
                f"Tokens: {p_tokens} in ({cached_tokens} cached, {cache_creation_tokens} write), {c_tokens} out | "
                f"Cost: ${call_cost:.5f} | "
                f"Cumulative Task Cost: ${self.cumulative_cost_usd:.4f} / ${settings.max_task_cost_usd:.2f}"
            )

            # Strict Kill-Switch Enforcement: abort task immediately if task cost ceiling is breached
            if self.cumulative_cost_usd > settings.max_task_cost_usd:
                error_msg = (
                    f"budget_exceeded: Cumulative LLM cost of ${self.cumulative_cost_usd:.4f} "
                    f"exceeded maximum allowed task budget of ${settings.max_task_cost_usd:.2f}."
                )
                logger.error(f"[LLM Kill-Switch] {error_msg}")
                if generation:
                    try:
                        generation.end(level="ERROR", status_message=error_msg)
                        langfuse_client.flush()
                    except Exception:
                        pass
                raise RuntimeError(error_msg)

            if generation:
                try:
                    usage_details = {
                        "input": p_tokens,
                        "output": c_tokens,
                        "total": p_tokens + c_tokens,
                    }
                    cost_details = {
                        "input": costs["input_cost_usd"],
                        "output": costs["output_cost_usd"],
                        "total": costs["total_cost_usd"],
                    }
                    usage = {
                        "unit": "TOKENS",
                        "input": p_tokens,
                        "output": c_tokens,
                        "total": p_tokens + c_tokens,
                        "inputCost": costs["input_cost_usd"],
                        "outputCost": costs["output_cost_usd"],
                        "totalCost": costs["total_cost_usd"],
                    }
                    gen_meta = {"provider": self.provider_name}
                    if costs.get("is_fallback"):
                        gen_meta["cost_estimation_mode"] = "conservative_fallback_ceiling"

                    generation.end(
                        model=self.model_name,
                        output=out_payload,
                        usage=usage,
                        usage_details=usage_details,
                        cost_details=cost_details,
                        metadata=gen_meta,
                        level="DEFAULT",
                    )
                    langfuse_client.flush()
                except Exception as e:
                    logger.warning(f"Error updating Langfuse generation: {e}")

            return result

        except Exception as err:
            if generation:
                try:
                    generation.end(
                        level="ERROR",
                        status_message=str(err),
                    )
                    langfuse_client.flush()
                except Exception:
                    pass
            raise err


# Backward compatibility alias
ObservedChatGoogle = ObservedChatModel


# ==============================================================================
# Multi-Provider Factory Registry
# ==============================================================================

def get_browser_use_llm(
    temperature: float = 0.2,
    session_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    trace_obj: Optional[Any] = None,
) -> Any:
    """Instantiates the configured LLM provider wrapped with universal Langfuse and cost observability."""
    provider = settings.llm_provider.strip().lower()
    model_name = settings.llm_model_name.strip()

    if provider == "vertex":
        from browser_use.llm.google.chat import ChatGoogle
        inner_model = ChatGoogle(
            model=model_name or settings.vertex_model_name or "gemini-2.0-flash",
            project=settings.google_cloud_project,
            location=settings.google_cloud_location or "global",
            temperature=temperature,
            vertexai=True,
        )

    elif provider == "anthropic":
        from browser_use.llm.anthropic.chat import ChatAnthropic
        inner_model = ChatAnthropic(
            model=model_name or "claude-3-5-sonnet-20241022",
            api_key=settings.anthropic_api_key,
            temperature=temperature,
        )

    elif provider == "openai":
        from browser_use.llm.openai.chat import ChatOpenAI
        inner_model = ChatOpenAI(
            model=model_name or "gpt-4o",
            api_key=settings.openai_api_key,
            temperature=temperature,
        )

    elif provider == "ollama":
        from browser_use.llm.ollama.chat import ChatOllama
        inner_model = ChatOllama(
            model=model_name or "qwen2.5:32b",
            host=settings.ollama_host,
        )

    else:
        raise ValueError(f"Unsupported LLM provider: '{provider}'")

    observed_model = ObservedChatModel(
        inner_model=inner_model,
        provider_name=provider,
        session_id=session_id,
        trace_id=trace_id,
        trace_obj=trace_obj,
    )
    return observed_model


class LLMGateway:
    """LLM Gateway providing Langfuse tracing for browser-use."""

    _langfuse_client: Optional[Any] = None
    _langfuse_initialized: bool = False

    @classmethod
    def get_langfuse_client(cls) -> Optional[Any]:
        """Initializes and returns the Langfuse client if keys are configured."""
        if cls._langfuse_initialized:
            return cls._langfuse_client

        if settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY:
            try:
                from langfuse import Langfuse
                cls._langfuse_client = Langfuse(
                    public_key=settings.LANGFUSE_PUBLIC_KEY,
                    secret_key=settings.LANGFUSE_SECRET_KEY,
                    host=settings.LANGFUSE_HOST,
                )
                cls._langfuse_initialized = True
                logger.info(f"Langfuse initialized successfully on {settings.LANGFUSE_HOST}")
            except Exception as e:
                logger.warning(f"Failed to initialize Langfuse: {e}")
                cls._langfuse_client = None
                cls._langfuse_initialized = True
        else:
            cls._langfuse_client = None
            cls._langfuse_initialized = True

        return cls._langfuse_client

    @classmethod
    def get_browser_use_llm(
        cls,
        temperature: float = 0.2,
        session_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        trace_obj: Optional[Any] = None,
    ) -> Any:
        return get_browser_use_llm(
            temperature=temperature,
            session_id=session_id,
            trace_id=trace_id,
            trace_obj=trace_obj,
        )
