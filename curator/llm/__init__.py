"""Interchangeable LLM backends: claude-code (subscription), anthropic-api (Claude API), openai-compatible
(open-weight models served locally, or a compatible service), fake (tests)."""
from __future__ import annotations

from pathlib import Path

from curator.config import Config
from curator.llm.base import LLMBackend, LLMError, LLMResult, validate_output


INPUT_SHARE = 0.75      # share of input tokens assumed when an estimate in tokens becomes an estimate in $


def prices_per_mtok(cfg: Config) -> tuple[float, float] | None:
    """$ per million tokens (input, output) of the configured model, when known: `llm.prices`, else the
    Claude API price list. None for claude-code (subscription quota) and for unknown prices."""
    if cfg.prices:
        return cfg.prices["input_per_mtok"], cfg.prices["output_per_mtok"]
    if cfg.backend == "anthropic-api":
        from curator.llm.anthropic_api import DEFAULT_MODEL, PRICES
        return PRICES.get(cfg.model or DEFAULT_MODEL)
    if cfg.backend == "openai-compatible":
        return 0.0, 0.0                                  # a local server has no price unless configured
    return None


def estimated_cost(cfg: Config, tokens: int) -> float | None:
    p = prices_per_mtok(cfg)
    return None if p is None else tokens * (INPUT_SHARE * p[0] + (1 - INPUT_SHARE) * p[1]) / 1_000_000


def make_backend(cfg: Config, instance_root: Path) -> LLMBackend:
    if cfg.backend == "fake":
        from curator.llm.fake import FakeBackend
        fx = Path(cfg.fake_fixtures) if cfg.fake_fixtures else None
        if fx is not None and not fx.is_absolute():
            fx = instance_root / fx
        return FakeBackend(fx)
    if cfg.backend == "claude-code":
        from curator.llm.claude_code import ClaudeCodeBackend
        return ClaudeCodeBackend(cfg.model, cfg.timeout_seconds, cfg.command)
    if cfg.backend == "anthropic-api":
        from curator.llm.anthropic_api import AnthropicApiBackend
        return AnthropicApiBackend(cfg.model, cfg.timeout_seconds, cfg.max_output_tokens, cfg.effort, cfg.fallbacks,
                                   cfg.prices)
    if cfg.backend == "openai-compatible":
        from curator.llm.openai_compat import OpenAICompatBackend
        return OpenAICompatBackend(cfg.model, cfg.base_url, cfg.api_key_env, cfg.timeout_seconds,
                                   cfg.max_output_tokens, cfg.prices)
    raise LLMError(f"unknown backend: {cfg.backend}")


__all__ = ["LLMBackend", "LLMError", "LLMResult", "estimated_cost", "make_backend", "prices_per_mtok", "validate_output"]
