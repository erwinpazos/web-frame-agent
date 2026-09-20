"""Comprehensive zero-cost, offline unit test suite for multi-provider LLM gateway,

pricing calculations (Anthropic cache tokens, fallback ceilings), decorator cost tracking,
budget kill-switches, and fail-fast configuration validation.
"""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.core.llm_gateway import (
    ObservedChatModel,
    calculate_token_cost,
    get_model_rates,
    normalize_model_family,
    get_browser_use_llm,
    CONSERVATIVE_FALLBACK_RATES,
    MODEL_FAMILY_PRICING,
)
from browser_use.llm.views import ChatInvokeCompletion, ChatInvokeUsage


class MockInnerModel:
    """Mock implementing browser-use BaseChatModel protocol."""

    def __init__(self, model_name: str = "test-model", provider_name: str = "test"):
        self.model = model_name
        self.name = model_name
        self.provider = provider_name
        self.ainvoke = AsyncMock()


class TestLLMGateway(unittest.IsolatedAsyncioTestCase):
    """Zero-cost unit tests for multi-provider LLM gateway."""

    def test_pricing_normalization_canonical_families(self):
        """Verifies regex normalization maps versioned model strings into canonical families."""
        # Anthropic versioned formats
        self.assertEqual(normalize_model_family("claude-3-5-sonnet-20241022"), "claude-3-5-sonnet")
        self.assertEqual(normalize_model_family("anthropic.claude-3-5-sonnet-20241022-v2:0"), "claude-3-5-sonnet")
        self.assertEqual(normalize_model_family("claude-3-5-haiku-20241022"), "claude-3-5-haiku")
        self.assertEqual(normalize_model_family("claude-sonnet-5"), "claude-sonnet-5")

        # OpenAI versioned formats
        self.assertEqual(normalize_model_family("gpt-4o-2024-08-06"), "gpt-4o")
        self.assertEqual(normalize_model_family("gpt-4o-mini-2024-07-18"), "gpt-4o-mini")
        self.assertEqual(normalize_model_family("o3-mini-2025-01-31"), "o3-mini")

        # Google Gemini formats
        self.assertEqual(normalize_model_family("gemini-2.0-flash-exp"), "gemini-2.0-flash")
        self.assertEqual(normalize_model_family("gemini-1.5-flash-001"), "gemini-1.5-flash")

        # Ollama provider
        self.assertEqual(normalize_model_family("qwen2.5:32b", provider="ollama"), "ollama")
        self.assertEqual(normalize_model_family("llama3.3:70b", provider="ollama"), "ollama")

    def test_anthropic_cache_pricing_exact_formula(self):
        """Tests the exact mathematical formula for Anthropic prompt caching with known numerical values.

        Formule:
        base_prompt = 10000 - 4000 (read) - 2000 (write) = 4000
        Base input cost: 4000 * 3.00 = 12000 u$
        Cache read: 4000 * (3.00 * 0.10) = 1200 u$
        Cache write: 2000 * (3.00 * 1.25) = 7500 u$
        Output cost: 1000 * 15.00 = 15000 u$
        Total: 35700 u$ = 0.035700 $
        """
        res = calculate_token_cost(
            model_name="claude-3-5-sonnet",
            prompt_tokens=10000,
            completion_tokens=1000,
            prompt_cached_tokens=4000,
            prompt_cache_creation_tokens=2000,
        )
        self.assertFalse(res["is_fallback"])
        self.assertEqual(res["base_prompt_tokens"], 4000)
        self.assertEqual(res["cached_tokens"], 4000)
        self.assertEqual(res["cache_creation_tokens"], 2000)
        self.assertEqual(res["completion_tokens"], 1000)
        self.assertAlmostEqual(res["cost_usd"], 0.035700, places=6)
        self.assertAlmostEqual(res["total_cost_usd"], 0.035700, places=6)

    def test_ollama_zero_cost_and_none_usage_safety(self):
        """Verifies that Ollama invocations calculate to 0.00$ and safely handle usage=None without crashing."""
        res = calculate_token_cost(
            model_name="qwen2.5:32b",
            prompt_tokens=5000,
            completion_tokens=500,
            provider="ollama",
        )
        self.assertEqual(res["cost_usd"], 0.0)
        self.assertEqual(res["total_cost_usd"], 0.0)
        self.assertFalse(res["is_fallback"])

    def test_unknown_model_fallback_ceiling_and_warning(self):
        """Verifies unknown model applies conservative fallback rates and logs warning rather than throwing KeyError."""
        with self.assertLogs("cobrowse_agent.llm_gateway", level="WARNING") as log_capture:
            rates, is_fallback = get_model_rates("exotic-custom-llm-v999")
            self.assertTrue(is_fallback)
            self.assertEqual(rates, CONSERVATIVE_FALLBACK_RATES)
            self.assertTrue(any("Unknown model 'exotic-custom-llm-v999'" in log for log in log_capture.output))
            self.assertTrue(any("Applied safety ceiling rate" in log for log in log_capture.output))

        # Check cost calculation with fallback
        cost_res = calculate_token_cost(
            model_name="exotic-custom-llm-v999",
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
        )
        self.assertTrue(cost_res["is_fallback"])
        # Expected: 1M * 3.00$ + 1M * 15.00$ = 18.00$
        self.assertAlmostEqual(cost_res["cost_usd"], 18.00, places=4)

    async def test_observed_chat_model_decorator_ollama_none_usage(self):
        """Verifies ObservedChatModel does not raise AttributeError when Ollama returns usage=None."""
        mock_inner = MockInnerModel(model_name="llama3.3:70b", provider_name="ollama")
        # Ollama returns completion with usage=None
        mock_inner.ainvoke.return_value = ChatInvokeCompletion(
            completion="hello from local llama",
            usage=None,
        )

        observed = ObservedChatModel(
            inner_model=mock_inner,
            provider_name="ollama",
        )

        messages = [{"role": "user", "content": "hi"}]
        result = await observed.ainvoke(messages)
        self.assertEqual(result.completion, "hello from local llama")
        self.assertEqual(observed.cumulative_cost_usd, 0.0)

    async def test_observed_chat_model_budget_kill_switch_triggers_immediately(self):
        """Verifies that exceeding max_task_cost_usd immediately raises RuntimeError with 'budget_exceeded'."""
        mock_inner = MockInnerModel(model_name="claude-3-opus", provider_name="anthropic")
        # Generate an invocation costing ~2.25$ (100k input @ $15/M = 1.50$, 10k output @ $75/M = 0.75$)
        mock_inner.ainvoke.return_value = ChatInvokeCompletion(
            completion="opus response",
            usage=ChatInvokeUsage(
                prompt_tokens=100_000,
                completion_tokens=10_000,
                total_tokens=110_000,
                prompt_cached_tokens=0,
                prompt_cache_creation_tokens=0,
                prompt_image_tokens=None,
            ),
        )

        observed = ObservedChatModel(
            inner_model=mock_inner,
            provider_name="anthropic",
        )

        # Set max_task_cost_usd ceiling to 1.50$
        with patch("app.core.config.settings.max_task_cost_usd", 1.50):
            with self.assertRaises(RuntimeError) as ctx:
                await observed.ainvoke([{"role": "user", "content": "solve task"}])

            self.assertIn("budget_exceeded", str(ctx.exception))
            self.assertIn("exceeded maximum allowed task budget of $1.50", str(ctx.exception))

    def test_pydantic_fail_fast_on_missing_or_invalid_provider_credentials(self):
        """Verifies that Pydantic fail-fast catches misconfigured providers before startup."""
        # 1. Anthropic missing or invalid key prefix
        with self.assertRaises(ValidationError) as ctx:
            Settings(
                llm_provider="anthropic",
                anthropic_api_key="",
                api_token="test-secret-token-16chars",
                backend_host="127.0.0.1",
                backend_port=8000,
                frontend_url="http://localhost:5173",
                cors_origins="http://localhost:5173",
                allowed_hosts="localhost,127.0.0.1",
            )
        self.assertIn("LLM_PROVIDER='anthropic' requires ANTHROPIC_API_KEY", str(ctx.exception))

        with self.assertRaises(ValidationError) as ctx:
            Settings(
                llm_provider="anthropic",
                anthropic_api_key="sk-wrong-openai-prefix",
                api_token="test-secret-token-16chars",
                backend_host="127.0.0.1",
                backend_port=8000,
                frontend_url="http://localhost:5173",
                cors_origins="http://localhost:5173",
                allowed_hosts="localhost,127.0.0.1",
            )
        self.assertIn("LLM_PROVIDER='anthropic' requires ANTHROPIC_API_KEY", str(ctx.exception))

        # 2. OpenAI missing key
        with self.assertRaises(ValidationError) as ctx:
            Settings(
                llm_provider="openai",
                openai_api_key="",
                api_token="test-secret-token-16chars",
                backend_host="127.0.0.1",
                backend_port=8000,
                frontend_url="http://localhost:5173",
                cors_origins="http://localhost:5173",
                allowed_hosts="localhost,127.0.0.1",
            )
        self.assertIn("LLM_PROVIDER='openai' requires OPENAI_API_KEY", str(ctx.exception))

        # 3. Ollama invalid host
        with self.assertRaises(ValidationError) as ctx:
            Settings(
                llm_provider="ollama",
                ollama_host="not-a-valid-url",
                api_token="test-secret-token-16chars",
                backend_host="127.0.0.1",
                backend_port=8000,
                frontend_url="http://localhost:5173",
                cors_origins="http://localhost:5173",
                allowed_hosts="localhost,127.0.0.1",
            )
        self.assertIn("LLM_PROVIDER='ollama' requires OLLAMA_HOST", str(ctx.exception))

        # 4. Unsupported provider
        with self.assertRaises(ValidationError) as ctx:
            Settings(
                llm_provider="nonexistent-provider",
                api_token="test-secret-token-16chars",
                backend_host="127.0.0.1",
                backend_port=8000,
                frontend_url="http://localhost:5173",
                cors_origins="http://localhost:5173",
                allowed_hosts="localhost,127.0.0.1",
            )
        self.assertIn("Unsupported LLM_PROVIDER: 'nonexistent-provider'", str(ctx.exception))

    def test_factory_instantiates_proper_provider_wrapper_without_network_calls(self):
        """Verifies factory selects and configures the right class without performing network requests."""
        # Test Anthropic factory resolution
        with patch("app.core.config.settings.llm_provider", "anthropic"), \
             patch("app.core.config.settings.llm_model_name", "claude-3-5-sonnet-20241022"), \
             patch("app.core.config.settings.anthropic_api_key", "sk-ant-test-key-mocked"), \
             patch("browser_use.llm.anthropic.chat.ChatAnthropic") as mock_anthropic_cls:

            mock_instance = MagicMock()
            mock_instance.model = "claude-3-5-sonnet-20241022"
            mock_anthropic_cls.return_value = mock_instance

            model = get_browser_use_llm()
            self.assertIsInstance(model, ObservedChatModel)
            self.assertEqual(model.provider, "anthropic")
            self.assertEqual(model.model_name, "claude-3-5-sonnet-20241022")
            mock_anthropic_cls.assert_called_once_with(
                model="claude-3-5-sonnet-20241022",
                api_key="sk-ant-test-key-mocked",
                temperature=0.2,
            )

        # Test OpenAI factory resolution
        with patch("app.core.config.settings.llm_provider", "openai"), \
             patch("app.core.config.settings.llm_model_name", "gpt-4o"), \
             patch("app.core.config.settings.openai_api_key", "sk-test-key-mocked"), \
             patch("browser_use.llm.openai.chat.ChatOpenAI") as mock_openai_cls:

            mock_instance = MagicMock()
            mock_instance.model = "gpt-4o"
            mock_openai_cls.return_value = mock_instance

            model = get_browser_use_llm()
            self.assertIsInstance(model, ObservedChatModel)
            self.assertEqual(model.provider, "openai")
            self.assertEqual(model.model_name, "gpt-4o")
            mock_openai_cls.assert_called_once_with(
                model="gpt-4o",
                api_key="sk-test-key-mocked",
                temperature=0.2,
            )

        # Test Ollama factory resolution
        with patch("app.core.config.settings.llm_provider", "ollama"), \
             patch("app.core.config.settings.llm_model_name", "qwen2.5:32b"), \
             patch("app.core.config.settings.ollama_host", "http://localhost:11434"), \
             patch("browser_use.llm.ollama.chat.ChatOllama") as mock_ollama_cls:

            mock_instance = MagicMock()
            mock_instance.model = "qwen2.5:32b"
            mock_ollama_cls.return_value = mock_instance

            model = get_browser_use_llm()
            self.assertIsInstance(model, ObservedChatModel)
            self.assertEqual(model.provider, "ollama")
            self.assertEqual(model.model_name, "qwen2.5:32b")
            mock_ollama_cls.assert_called_once_with(
                model="qwen2.5:32b",
                host="http://localhost:11434",
            )


if __name__ == "__main__":
    unittest.main()
