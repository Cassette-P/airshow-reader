"""Gemini File API integration, streaming structured generation, thinking level adaptation, and disaster recovery backoff."""

from collections import deque
from pathlib import Path
import re
import time
from typing import Callable, Optional

from google import genai
from google.genai import types
from pydantic import BaseModel

from pipeline.audio import get_audio_duration_sec
from pipeline.config import (
    MODEL_USAGE_STATS,
    _format_level,
    get_ch_a_model_pool,
    get_ch_b_api_keys,
    get_ch_b_free_rpd,
    get_ch_b_free_rpm,
    get_ch_b_model_pool,
    get_ch_b_overflow_to_paid,
    get_env_stripped,
    get_next_fallback_thinking_level,
    log,
    resolve_ch_a_thinking_level,
)
from pipeline.models import (
    ChannelAOutput,
    ChannelBOutput,
    clamp_and_sort_highlights,
    clamp_and_sort_references,
    clamp_and_sort_vocab_notes,
    clamp_key_takeaways,
)
from pipeline.prompts import (
    CHANNEL_B_USER_PROMPT,
    build_channel_a_system_instruction,
    build_channel_a_user_prompt,
    build_channel_b_system_instruction,
    build_channel_b_user_prompt,
)
from pipeline.storage import (
    format_sec_to_mmss,
    normalize_speaker,
    parse_mmss_to_sec,
)


def create_genai_client(api_key: Optional[str] = None) -> genai.Client:
    """Create a new genai.Client instance with 300-second request timeout.

    In google-genai SDK, types.HttpOptions.timeout is in milliseconds: 300s = 300,000ms.
    """
    key = api_key or get_env_stripped("GEMINI_API_KEY")
    http_opts = types.HttpOptions(timeout=300000)
    return genai.Client(api_key=key, http_options=http_opts)


def is_transport_disconnect_or_timeout(exc: Exception) -> bool:
    """Identify transport-level disconnect and timeout exceptions."""
    known_types = {
        "RemoteProtocolError",
        "ReadTimeout",
        "ConnectTimeout",
        "ConnectionError",
        "ConnectError",
        "ReadError",
        "WriteError",
        "PoolTimeout",
        "NetworkError",
    }
    for cls in type(exc).__mro__:
        if cls.__name__ in known_types:
            return True

    err_str = str(exc).lower()
    disconnect_keywords = [
        "server disconnected",
        "connection reset",
        "timed out",
        "timeout",
        "remote protocol error",
        "connection closed",
        "broken pipe",
        "connection aborted",
    ]
    return any(kw in err_str for kw in disconnect_keywords)


class QuotaExhaustedRPDError(RuntimeError):
    """Raised immediately when Free Tier daily quota (RPD) or key access is exhausted."""

    def __init__(self, model: str, message: str, auth_error: bool = False):
        super().__init__(message)
        self.model = model
        self.auth_error = auth_error


def is_rate_limit_error(exc: Exception) -> bool:
    """Identify HTTP 429 / RESOURCE_EXHAUSTED rate limit exceptions."""
    for cls in type(exc).__mro__:
        cls_name = cls.__name__.lower()
        if "toomanyrequests" in cls_name or "resourceexhausted" in cls_name:
            return True
    err_str = str(exc).lower()
    return any(
        kw in err_str
        for kw in [
            "429",
            "resource_exhausted",
            "resource exhausted",
            "too many requests",
            "quota exceeded",
            "rate limit",
        ]
    )


def is_rpd_exhausted_error(exc: Exception) -> bool:
    """Distinguish daily quota exhaustion (RPD / limit: 0) from transient RPM/TPM 429s."""
    if not is_rate_limit_error(exc):
        return False
    err_str = str(exc).lower()
    if "limit: 0" in err_str or "limit=0" in err_str or "quota_value: 0" in err_str:
        return True
    rpd_keywords = [
        "perday",
        "per_day",
        "per day",
        "requestsperday",
        "requests_per_day",
        "daily",
    ]
    rpm_keywords = [
        "perminute",
        "per_minute",
        "per minute",
        "requestsperminute",
        "tokensperminute",
    ]
    has_rpd = any(kw in err_str for kw in rpd_keywords)
    has_rpm = any(kw in err_str for kw in rpm_keywords)
    return has_rpd and not has_rpm


def is_api_key_auth_error(exc: Exception) -> bool:
    """Identify invalid/revoked API key or restricted project errors (401/403)."""
    if is_transport_disconnect_or_timeout(exc):
        return False
    err_str = str(exc).lower()
    return any(
        kw in err_str
        for kw in [
            "api_key_invalid",
            "api key not valid",
            "invalid api key",
            "401",
            "403",
            "permission_denied",
            "permission denied",
            "unauthenticated",
            "free tier unavailable",
            "access restricted",
        ]
    )


def is_service_unavailable_error(exc: Exception) -> bool:
    """Identify HTTP 503 / UNAVAILABLE / high-demand congestion errors."""
    if is_rate_limit_error(exc):
        return False
    for cls in type(exc).__mro__:
        if "serviceunavailable" in cls.__name__.lower():
            return True
    err_str = str(exc).lower()
    return any(
        kw in err_str
        for kw in [
            "503",
            "unavailable",
            "high demand",
            "overloaded",
            "temporarily unavailable",
        ]
    )


def parse_retry_delay_sec(exc: Exception) -> Optional[float]:
    """Extract server-suggested retry delay in seconds from a Gemini 429 error message."""
    err_str = str(exc)
    patterns = [
        r"retry\s+in\s+(\d+(?:\.\d+)?)\s*s",
        r"retry_?delay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s?",
        r"retry\s+after\s+(\d+(?:\.\d+)?)\s*s?",
    ]
    for pattern in patterns:
        match = re.search(pattern, err_str, flags=re.IGNORECASE)
        if match:
            try:
                val = float(match.group(1))
                if val > 0:
                    return val
            except ValueError:
                continue
    return None


class SlidingWindowRateLimiter:
    """Dual-guard RPM limiter: enforces both rolling window cap and smooth minimum spacing.

    For 5 RPM:
    - Rolling window: at most 5 request starts within any 65.0s window (60s + 5s clock/RTT margin).
    - Smooth spacing: at least 62.0 / 5 = 12.4s between consecutive request starts to prevent burst 429s.
    """

    def __init__(
        self,
        max_requests: int = 5,
        window_sec: float = 65.0,
        min_interval_sec: Optional[float] = None,
        clock: Optional[Callable[[], float]] = None,
        sleeper: Optional[Callable[[float], None]] = None,
    ):
        self.max_requests = max(0, int(max_requests))
        self.window_sec = float(window_sec)
        if min_interval_sec is not None:
            self.min_interval_sec = max(0.0, float(min_interval_sec))
        elif self.max_requests > 0:
            self.min_interval_sec = round(62.0 / self.max_requests, 2)
        else:
            self.min_interval_sec = 0.0
        self._timestamps: deque[float] = deque()
        self._clock = clock
        self._sleeper = sleeper

    def _now(self) -> float:
        return self._clock() if self._clock is not None else time.monotonic()

    def _sleep(self, sec: float) -> None:
        if self._sleeper is not None:
            self._sleeper(sec)
        else:
            time.sleep(sec)

    def acquire(self, stage: str = "CH-B", show_id: str = "") -> float:
        """Wait if needed to satisfy RPM limits, then record this request's start timestamp.

        Returns seconds slept.
        """
        if self.max_requests <= 0:
            return 0.0

        now = self._now()
        while self._timestamps and (now - self._timestamps[0]) >= self.window_sec:
            self._timestamps.popleft()

        wait_window = 0.0
        if len(self._timestamps) >= self.max_requests:
            wait_window = self.window_sec - (now - self._timestamps[0])

        wait_spacing = 0.0
        if self._timestamps and self.min_interval_sec > 0:
            elapsed_since_last = now - self._timestamps[-1]
            if elapsed_since_last < self.min_interval_sec:
                wait_spacing = self.min_interval_sec - elapsed_since_last

        wait_sec = max(wait_window, wait_spacing)
        slept = 0.0
        if wait_sec > 0.01:
            reason = (
                f"rolling window ({len(self._timestamps)}/{self.max_requests} in {self.window_sec:.0f}s)"
                if wait_window >= wait_spacing
                else f"smooth spacing (min {self.min_interval_sec:.1f}s for {self.max_requests} RPM)"
            )
            log(
                stage,
                f"[RATE-LIMIT] Free Tier {self.max_requests} RPM guard: sleeping {wait_sec:.1f}s ({reason})...",
                show_id=show_id,
            )
            self._sleep(wait_sec)
            slept = wait_sec

        now_after = max(self._now(), now + slept)
        while self._timestamps and (now_after - self._timestamps[0]) >= self.window_sec:
            self._timestamps.popleft()
        self._timestamps.append(now_after)
        return slept


class ChannelBQuotaState:
    """Process-wide Free Tier quota & RPM pacing state shared across episodes in one run."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.rate_limiter: Optional[SlidingWindowRateLimiter] = None
        self.rate_limiter_rpm: int = -1
        self.free_rpd_used: dict[str, int] = {}
        self.free_rpd_exhausted_models: set[str] = set()
        self.consecutive_503_chunks: dict[str, int] = {}
        self.free_503_tripped_models: set[str] = set()
        self.free_key_disabled: bool = False

    def get_rate_limiter(self, rpm: int) -> Optional[SlidingWindowRateLimiter]:
        if rpm <= 0:
            return None
        if self.rate_limiter is None or self.rate_limiter_rpm != rpm:
            self.rate_limiter = SlidingWindowRateLimiter(max_requests=rpm)
            self.rate_limiter_rpm = rpm
        return self.rate_limiter

    def can_use_free_for_model(self, model: str, rpd_budget: int) -> tuple[bool, str]:
        if self.free_key_disabled:
            return False, "free_key_disabled"
        if model in self.free_rpd_exhausted_models:
            return False, "rpd_429_exhausted"
        if model in self.free_503_tripped_models:
            return False, "free_503_circuit_breaker"
        used = self.free_rpd_used.get(model, 0)
        if rpd_budget > 0 and used >= rpd_budget:
            return False, f"rpd_budget_reached ({used}/{rpd_budget})"
        return True, "ok"

    def record_free_success(self, model: str) -> int:
        self.consecutive_503_chunks[model] = 0
        self.free_rpd_used[model] = self.free_rpd_used.get(model, 0) + 1
        return self.free_rpd_used[model]

    def record_free_503_chunk_failure(self, model: str, threshold: int = 2) -> tuple[int, bool]:
        count = self.consecutive_503_chunks.get(model, 0) + 1
        self.consecutive_503_chunks[model] = count
        tripped = count >= threshold
        if tripped:
            self.free_503_tripped_models.add(model)
        return count, tripped

    def reset_consecutive_503(self, model: str) -> None:
        self.consecutive_503_chunks[model] = 0

    def mark_free_rpd_exhausted(self, model: str, disable_key: bool = False) -> None:
        self.free_rpd_exhausted_models.add(model)
        if disable_key:
            self.free_key_disabled = True


CH_B_QUOTA_STATE = ChannelBQuotaState()


def reset_ch_b_quota_state() -> None:
    """Reset process-wide Channel B Free Tier quota state (primarily for tests)."""
    CH_B_QUOTA_STATE.reset()


def clean_json_markdown(text: str) -> str:
    """Strip markdown code fence blocks if present."""
    cleaned = text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


def is_interactions_unsupported_error(exc: Exception) -> bool:
    """Identify if an error indicates Interactions API is not supported by the SDK/model/endpoint."""
    if isinstance(exc, (AttributeError, NotImplementedError)):
        return True
    err_str = str(exc).lower()
    unsupported_keywords = [
        "not supported",
        "unsupported",
        "unknown method",
        "unknown field",
        "not implemented",
        "not found",
        "404",
        "interactions not enabled",
        "invalid argument",
        "invalid_request",
        "400",
        "only available in v1beta",
    ]
    return any(kw in err_str for kw in unsupported_keywords) and not is_transport_disconnect_or_timeout(exc)


def extract_interaction_text(interaction: object) -> str:
    """Extract model output text from Interaction object."""
    # 1. output_text property
    text = getattr(interaction, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text

    # 2. Extract from steps
    steps = getattr(interaction, "steps", None)
    if isinstance(steps, list):
        text_parts: list[str] = []
        for step in reversed(steps):
            step_type = getattr(step, "type", None) or (step.get("type") if isinstance(step, dict) else None)
            if step_type == "user_input":
                break
            if step_type != "model_output":
                continue
            content = getattr(step, "content", None) or (step.get("content") if isinstance(step, dict) else None)
            if isinstance(content, list):
                for item in reversed(content):
                    item_type = getattr(item, "type", None) or (item.get("type") if isinstance(item, dict) else None)
                    if item_type == "text":
                        item_text = getattr(item, "text", None) or (item.get("text") if isinstance(item, dict) else None)
                        if isinstance(item_text, str):
                            text_parts.append(item_text)
                if text_parts:
                    return "".join(reversed(text_parts))

    # 3. Fallback to outputs if Lyria/legacy shape
    outputs = getattr(interaction, "outputs", None) or (interaction.get("outputs") if isinstance(interaction, dict) else None)
    if isinstance(outputs, list):
        text_parts = []
        for item in outputs:
            t = getattr(item, "text", None) or (item.get("text") if isinstance(item, dict) else None)
            if isinstance(t, str):
                text_parts.append(t)
        if text_parts:
            return "".join(text_parts)

    return ""


def generate_structured_with_retry(
    client: genai.Client,
    model_pool: list[str],
    contents: list,
    system_instruction: str,
    response_schema: type[BaseModel],
    stage: str = "GENAI",
    temperature: float = 0.2,
    max_retries: int = 5,
    thinking_level: Optional[str] = None,
    thinking_level_resolver: Optional[Callable[[str], Optional[str]]] = None,
    show_id: str = "",
    client_factory: Optional[Callable[[], genai.Client]] = None,
    max_output_tokens: Optional[int] = None,
    rate_limiter: Optional[SlidingWindowRateLimiter] = None,
    fail_fast_on_rpd: bool = False,
    is_free_tier: bool = False,
    usage_tier_tag: str = "",
) -> BaseModel:
    """Execute structured generation using streaming response with 5-retry backoff across model_pool."""
    backoff_delays = [15, 20, 25, 30]
    last_exception = None

    if client_factory is None:
        client_factory = lambda: create_genai_client()

    for m_idx, current_model in enumerate(model_pool):
        if callable(thinking_level_resolver):
            resolved_thinking_level = thinking_level_resolver(current_model)
        else:
            resolved_thinking_level = thinking_level

        current_thinking_level = resolved_thinking_level
        log(
            stage,
            f"Using model candidate [{m_idx + 1}/{len(model_pool)}]: '{current_model}' (initial thinking_level='{current_thinking_level}')...",
            show_id=show_id,
        )
        skipped_404 = False
        consecutive_disconnects = 0

        for attempt in range(1, max_retries + 1):
            try:
                full_text = ""
                while True:
                    # 1. Instantiate thinking_config, degrading on local SDK validation errors
                    while current_thinking_level:
                        try:
                            _ = types.ThinkingConfig(thinking_level=current_thinking_level)
                            break
                        except Exception as t_init_err:
                            next_lvl = get_next_fallback_thinking_level(current_thinking_level)
                            log(
                                stage,
                                f"Local SDK does not support thinking_level='{current_thinking_level}' ({t_init_err}). "
                                f"Degrading: '{current_thinking_level}' -> {_format_level(next_lvl)}.",
                                show_id=show_id,
                            )
                            current_thinking_level = next_lvl

                    config_kwargs = {
                        "system_instruction": system_instruction,
                        "response_mime_type": "application/json",
                        "response_schema": response_schema,
                        "temperature": temperature,
                    }
                    if max_output_tokens is not None:
                        config_kwargs["max_output_tokens"] = max_output_tokens
                    if current_thinking_level:
                        try:
                            config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=current_thinking_level)
                        except Exception as t_init_err:
                            next_lvl = get_next_fallback_thinking_level(current_thinking_level)
                            log(
                                stage,
                                f"Local SDK does not support thinking_level='{current_thinking_level}' ({t_init_err}). "
                                f"Degrading: '{current_thinking_level}' -> {_format_level(next_lvl)}.",
                                show_id=show_id,
                            )
                            current_thinking_level = next_lvl
                            continue

                    try:
                        gen_config = types.GenerateContentConfig(**config_kwargs)
                    except Exception as cfg_err:
                        if current_thinking_level:
                            next_lvl = get_next_fallback_thinking_level(current_thinking_level)
                            log(
                                stage,
                                f"GenerateContentConfig validation failed with thinking_level='{current_thinking_level}' ({cfg_err}). "
                                f"Degrading: '{current_thinking_level}' -> {_format_level(next_lvl)}.",
                                show_id=show_id,
                            )
                            current_thinking_level = next_lvl
                            continue
                        raise cfg_err

                    thinking_desc = (
                        f"effective thinking_level='{current_thinking_level}'"
                        if current_thinking_level
                        else "effective thinking_level=None"
                    )
                    log(
                        stage,
                        f"Calling Gemini model '{current_model}' via streaming (attempt {attempt}/{max_retries}, {thinking_desc})...",
                        show_id=show_id,
                    )

                    try:
                        if rate_limiter is not None:
                            rate_limiter.acquire(stage=stage, show_id=show_id)
                        response = client.models.generate_content_stream(
                            model=current_model,
                            contents=contents,
                            config=gen_config,
                        )
                        chunks_text = []
                        for chunk in response:
                            if chunk.text:
                                chunks_text.append(chunk.text)
                        full_text = "".join(chunks_text).strip()
                        break  # Streaming succeeded, exit thinking degradation loop
                    except Exception as api_err:
                        if current_thinking_level:
                            err_lower = str(api_err).lower()
                            is_thinking_err = any(
                                kw in err_lower
                                for kw in [
                                    "thinking",
                                    "unsupported",
                                    "unknown field",
                                    "invalid argument",
                                    "not supported",
                                    "thinking_level",
                                    "thinking_config",
                                    "thought",
                                ]
                            )
                            if is_thinking_err and not is_transport_disconnect_or_timeout(api_err):
                                next_lvl = get_next_fallback_thinking_level(current_thinking_level)
                                log(
                                    stage,
                                    f"Model '{current_model}' call failed with thinking_level='{current_thinking_level}' ({api_err}). "
                                    f"Degrading: '{current_thinking_level}' -> {_format_level(next_lvl)} and retrying immediately...",
                                    show_id=show_id,
                                )
                                current_thinking_level = next_lvl
                                continue
                        raise api_err

                if not full_text:
                    raise ValueError(f"Received empty streamed response from model '{current_model}'.")

                cleaned_text = clean_json_markdown(full_text)

                parsed = response_schema.model_validate_json(cleaned_text)
                log(
                    stage,
                    f"Successfully received and parsed response using model '{current_model}' on attempt {attempt} "
                    f"(effective thinking_level='{current_thinking_level}').",
                    show_id=show_id,
                )
                if stage in MODEL_USAGE_STATS:
                    stat_key = f"{current_model} ({usage_tier_tag})" if usage_tier_tag else current_model
                    MODEL_USAGE_STATS[stage][stat_key] = MODEL_USAGE_STATS[stage].get(stat_key, 0) + 1
                return parsed

            except Exception as e:
                last_exception = e
                err_str = str(e)
                log("ERROR", f"[{stage}] Attempt {attempt}/{max_retries} with model '{current_model}' failed: {e}", show_id=show_id)

                if fail_fast_on_rpd and is_api_key_auth_error(e):
                    log(
                        "WARN",
                        f"[{stage}] Free Tier API key authentication/access error on model '{current_model}'. Failing fast for fallback...",
                        show_id=show_id,
                    )
                    raise QuotaExhaustedRPDError(current_model, err_str, auth_error=True) from e

                if is_rpd_exhausted_error(e):
                    if fail_fast_on_rpd:
                        log(
                            "WARN",
                            f"[{stage}] Free Tier daily quota (RPD) exhausted for model '{current_model}'. Failing fast for overflow/fallback...",
                            show_id=show_id,
                        )
                        raise QuotaExhaustedRPDError(current_model, err_str) from e
                    log(
                        "WARN",
                        f"[{stage}] Daily quota (RPD) exhausted for model '{current_model}'. Skipping remaining retries for this model.",
                        show_id=show_id,
                    )
                    skipped_404 = True
                    break

                is_404 = "404" in err_str or "NOT_FOUND" in err_str or "NotFound" in type(e).__name__
                if is_404:
                    log("ERROR", f"[{stage}] Model '{current_model}' returned 404/NOT_FOUND. Skipping model immediately without waiting.", show_id=show_id)
                    skipped_404 = True
                    break

                if is_transport_disconnect_or_timeout(e):
                    consecutive_disconnects += 1
                    try:
                        if hasattr(client, "close"):
                            client.close()
                    except Exception:
                        pass
                    if callable(client_factory):
                        try:
                            client = client_factory()
                            log(stage, "Rebuilt Gemini client after transport disconnect/timeout.", show_id=show_id)
                        except Exception as rebuild_err:
                            log("WARN", f"Failed to rebuild Gemini client: {rebuild_err}", show_id=show_id)

                    if consecutive_disconnects >= 2:
                        if current_thinking_level is not None:
                            next_lvl = get_next_fallback_thinking_level(current_thinking_level)
                            log(
                                stage,
                                f"2 consecutive disconnects at thinking_level='{current_thinking_level}', stepping down to {_format_level(next_lvl)}",
                                show_id=show_id,
                            )
                            current_thinking_level = next_lvl
                        consecutive_disconnects = 0
                else:
                    consecutive_disconnects = 0

                if attempt < max_retries:
                    delay: float = float(backoff_delays[attempt - 1] if (attempt - 1) < len(backoff_delays) else 30)
                    if is_rate_limit_error(e):
                        suggested_delay = parse_retry_delay_sec(e)
                        if suggested_delay is not None:
                            delay = min(max(suggested_delay + 2.0, 15.0), 75.0)
                            log(
                                stage,
                                f"[RATE-LIMIT] Transient 429 RPM/TPM hit on '{current_model}'. "
                                f"Honoring server retryDelay (+2s buffer): sleeping {delay:.1f}s (attempt {attempt}/{max_retries})...",
                                show_id=show_id,
                            )
                        elif is_free_tier:
                            delay = max(delay, 65.0)
                            log(
                                stage,
                                f"[RATE-LIMIT] Transient 429 RPM/TPM hit on Free Tier '{current_model}'. "
                                f"Sleeping {delay:.1f}s for rolling minute window reset (attempt {attempt}/{max_retries})...",
                                show_id=show_id,
                            )
                        else:
                            log(stage, f"Retrying model '{current_model}' after {delay:.1f}s backoff (attempt {attempt}/{max_retries})...", show_id=show_id)
                    else:
                        log(stage, f"Retrying model '{current_model}' after {delay:.1f}s backoff (attempt {attempt}/{max_retries})...", show_id=show_id)
                    time.sleep(delay)
                else:
                    log("ERROR", f"[{stage}] All {max_retries} attempts exhausted for model '{current_model}'.", show_id=show_id)

        if m_idx < len(model_pool) - 1:
            if not skipped_404:
                log(stage, f"Model '{current_model}' exhausted. Sleeping 10s before switching to next candidate model...", show_id=show_id)
                time.sleep(10)

    if last_exception is not None:
        raise last_exception
    raise RuntimeError(f"All models in pool {model_pool} exhausted without returning a valid response.")


def generate_channel_a_structured_with_retry(
    client: genai.Client,
    model_pool: list[str],
    user_prompt: str = "",
    system_instruction: str = "",
    response_schema: Optional[type[BaseModel]] = None,
    temperature: float = 0.2,
    max_retries: int = 5,
    thinking_level_resolver: Optional[Callable[[str], Optional[str]]] = None,
    show_id: str = "",
    client_factory: Optional[Callable[[], genai.Client]] = None,
    contents: Optional[list] = None,
    stage: str = "CH-A",
    thinking_level: Optional[str] = None,
) -> BaseModel:
    """Execute Channel A generation using dual-transport architecture:
    1. Primary: Interactions API (background=True + short-connection polling)
    2. Fallback: generate_content_stream with include_thoughts=True (thought heartbeat keepalive)
    """
    if not user_prompt and contents:
        user_prompt = str(contents[0])
    if response_schema is None:
        response_schema = ChannelAOutput
    backoff_delays = [15, 20, 25, 30]
    last_exception = None

    if client_factory is None:
        client_factory = lambda: create_genai_client()

    bg_timeout_raw = get_env_stripped("CH_A_BG_TIMEOUT_SEC", "900")
    try:
        bg_timeout_sec = float(bg_timeout_raw)
    except ValueError:
        bg_timeout_sec = 900.0

    for m_idx, current_model in enumerate(model_pool):
        if callable(thinking_level_resolver):
            resolved_thinking_level = thinking_level_resolver(current_model)
        elif thinking_level is not None:
            resolved_thinking_level = thinking_level
        else:
            resolved_thinking_level = resolve_ch_a_thinking_level(current_model)

        current_thinking_level = resolved_thinking_level
        log(
            stage,
            f"Using Channel A model candidate [{m_idx + 1}/{len(model_pool)}]: '{current_model}' "
            f"(initial thinking_level='{current_thinking_level}')...",
            show_id=show_id,
        )

        skipped_404 = False
        consecutive_disconnects = 0
        consecutive_bg_timeouts = 0
        force_fallback_stream = not hasattr(client, "interactions")
        if force_fallback_stream:
            log(
                "WARN",
                f"Client does not have 'interactions' attribute. Defaulting to stream_with_thoughts for model '{current_model}'.",
                show_id=show_id,
            )

        for attempt in range(1, max_retries + 1):
            t_attempt_start = time.time()
            try:
                # Mode 1: Primary interactions_bg
                if not force_fallback_stream:
                    interaction = None
                    while not force_fallback_stream:
                        log(
                            "CH-A",
                            f"Attempting Channel A via transport=interactions_bg (attempt {attempt}/{max_retries}, "
                            f"model='{current_model}', thinking_level='{current_thinking_level}')...",
                            show_id=show_id,
                        )
                        gen_config: dict[str, object] = {
                            "max_output_tokens": 65536,
                            "temperature": temperature,
                        }
                        if current_thinking_level:
                            gen_config["thinking_level"] = current_thinking_level

                        resp_format = {
                            "type": "text",
                            "mime_type": "application/json",
                            "schema": response_schema.model_json_schema(),
                        }

                        try:
                            interaction = client.interactions.create(
                                model=current_model,
                                input=user_prompt,
                                system_instruction=system_instruction,
                                background=True,
                                store=True,
                                generation_config=gen_config,
                                response_format=resp_format,
                            )
                            break
                        except Exception as create_err:
                            err_str = str(create_err).lower()
                            is_thinking_err = (
                                current_thinking_level is not None
                                and any(kw in err_str for kw in ["thinking_level", "thinking_config", "thought"])
                                and not any(kw in err_str for kw in ["response_format", "response_mime_type", "only available in", "invalid_request"])
                            )
                            if is_thinking_err:
                                next_lvl = get_next_fallback_thinking_level(current_thinking_level)
                                log(
                                    "CH-A",
                                    f"Interactions API call rejected thinking_level='{current_thinking_level}' ({create_err}). "
                                    f"Stepping down: '{current_thinking_level}' -> {_format_level(next_lvl)} and retrying...",
                                    show_id=show_id,
                                )
                                current_thinking_level = next_lvl
                                continue

                            is_fast_fallback = (
                                is_interactions_unsupported_error(create_err)
                                or "400" in err_str
                                or "invalid_request" in err_str
                                or "invalid argument" in err_str
                            )
                            if is_fast_fallback:
                                log(
                                    "WARN",
                                    f"Interactions API request invalid or unsupported for model '{current_model}' ({create_err}). "
                                    f"Fast-falling back to stream_with_thoughts immediately...",
                                    show_id=show_id,
                                )
                                force_fallback_stream = True
                                break
                            raise create_err

                    if not force_fallback_stream:
                        interaction_id = getattr(interaction, "id", None)
                        if not interaction_id:
                            raise RuntimeError(f"Interactions API did not return an interaction id: {interaction}")

                        log(
                            "CH-A",
                            f"Channel A background task submitted: interaction_id='{interaction_id}'. "
                            f"Starting polling (interval=10s, timeout={bg_timeout_sec}s)...",
                            show_id=show_id,
                        )

                        poll_start_time = time.time()
                        consecutive_poll_net_errors = 0
                        final_interaction = None

                        while True:
                            elapsed = time.time() - poll_start_time
                            if elapsed > bg_timeout_sec:
                                try:
                                    if hasattr(client.interactions, "cancel"):
                                        client.interactions.cancel(id=interaction_id)
                                        log(
                                            "CH-A",
                                            f"Cancelled timed-out interaction '{interaction_id}'.",
                                            show_id=show_id,
                                        )
                                except Exception as cancel_err:
                                    log(
                                        "WARN",
                                        f"Failed to cancel timed-out interaction '{interaction_id}': {cancel_err}",
                                        show_id=show_id,
                                    )
                                raise TimeoutError(
                                    f"Channel A background interaction '{interaction_id}' timed out after {elapsed:.1f}s (timeout={bg_timeout_sec}s)."
                                )

                            time.sleep(10)

                            try:
                                poll_obj = client.interactions.get(id=interaction_id)
                                consecutive_poll_net_errors = 0
                            except Exception as poll_err:
                                if is_transport_disconnect_or_timeout(poll_err):
                                    consecutive_poll_net_errors += 1
                                    log(
                                        "WARN",
                                        f"Channel A interaction poll network error: {poll_err}. "
                                        f"Consecutive errors: {consecutive_poll_net_errors}/5. "
                                        f"Rebuilding client and resuming poll for '{interaction_id}'...",
                                        show_id=show_id,
                                    )
                                    try:
                                        if hasattr(client, "close"):
                                            client.close()
                                    except Exception:
                                        pass
                                    client = client_factory()
                                    if consecutive_poll_net_errors >= 5:
                                        raise RuntimeError(
                                            f"Channel A interaction '{interaction_id}' polling failed after 5 consecutive network errors: {poll_err}"
                                        ) from poll_err
                                    continue
                                else:
                                    raise poll_err

                            status = str(getattr(poll_obj, "status", "")).lower()
                            poll_elapsed = time.time() - poll_start_time
                            log(
                                "CH-A",
                                f"Channel A interaction '{interaction_id}' status='{status}' (elapsed: {poll_elapsed:.1f}s)...",
                                show_id=show_id,
                            )

                            if status in ("in_progress", "queued", "idle", "requires_continuation"):
                                continue
                            elif status == "completed":
                                final_interaction = poll_obj
                                break
                            elif status in ("failed", "incomplete", "cancelled", "budget_exceeded", "requires_action"):
                                errs = getattr(poll_obj, "errors", None)
                                raise RuntimeError(
                                    f"Channel A interaction '{interaction_id}' terminated with non-completed status='{status}'. Details: {errs}"
                                )
                            else:
                                log(
                                    "WARN",
                                    f"Channel A interaction '{interaction_id}' received unexpected status='{status}'. Continuing poll...",
                                    show_id=show_id,
                                )

                        raw_text = extract_interaction_text(final_interaction)
                        if not raw_text:
                            raise ValueError(f"Channel A interaction '{interaction_id}' completed but contained empty text.")

                        cleaned_text = clean_json_markdown(raw_text)
                        parsed = response_schema.model_validate_json(cleaned_text)

                        usage = getattr(final_interaction, "usage", None)
                        input_tokens = (
                            getattr(usage, "total_input_tokens", None)
                            or getattr(usage, "initial_input_tokens", None)
                            or 0
                        )
                        thought_tokens = getattr(usage, "total_thought_tokens", None) or 0
                        output_tokens = getattr(usage, "total_output_tokens", None) or 0

                        total_time = time.time() - t_attempt_start
                        poll_time = time.time() - poll_start_time
                        log(
                            "CH-A",
                            f"Channel A call completed: transport=interactions_bg, model='{current_model}', "
                            f"thinking_level='{current_thinking_level}', poll_time={poll_time:.1f}s, total_time={total_time:.1f}s, "
                            f"tokens=(input={input_tokens}, thought={thought_tokens}, output={output_tokens}).",
                            show_id=show_id,
                        )
                        if stage in MODEL_USAGE_STATS:
                            MODEL_USAGE_STATS[stage][current_model] = MODEL_USAGE_STATS[stage].get(current_model, 0) + 1
                        return parsed

                # Mode 2: Fallback stream_with_thoughts
                if force_fallback_stream:
                    use_include_thoughts = True
                    full_text = ""
                    usage_meta = None
                    while True:
                        while current_thinking_level:
                            try:
                                _ = types.ThinkingConfig(
                                    thinking_level=current_thinking_level,
                                    include_thoughts=use_include_thoughts,
                                )
                                break
                            except Exception as t_init_err:
                                next_lvl = get_next_fallback_thinking_level(current_thinking_level)
                                log(
                                    "CH-A",
                                    f"Local SDK does not support thinking_level='{current_thinking_level}' with include_thoughts={use_include_thoughts} ({t_init_err}). "
                                    f"Degrading: '{current_thinking_level}' -> {_format_level(next_lvl)}.",
                                    show_id=show_id,
                                )
                                current_thinking_level = next_lvl

                        config_kwargs = {
                            "system_instruction": system_instruction,
                            "response_mime_type": "application/json",
                            "response_schema": response_schema,
                            "temperature": temperature,
                            "max_output_tokens": 65536,
                        }
                        if current_thinking_level:
                            config_kwargs["thinking_config"] = types.ThinkingConfig(
                                thinking_level=current_thinking_level,
                                include_thoughts=use_include_thoughts,
                            )
                        elif use_include_thoughts:
                            try:
                                config_kwargs["thinking_config"] = types.ThinkingConfig(include_thoughts=True)
                            except Exception:
                                use_include_thoughts = False

                        try:
                            gen_config_obj = types.GenerateContentConfig(**config_kwargs)
                        except Exception as cfg_err:
                            if current_thinking_level:
                                next_lvl = get_next_fallback_thinking_level(current_thinking_level)
                                log(
                                    "CH-A",
                                    f"GenerateContentConfig validation failed with thinking_level='{current_thinking_level}' ({cfg_err}). "
                                    f"Degrading: '{current_thinking_level}' -> {_format_level(next_lvl)}.",
                                    show_id=show_id,
                                )
                                current_thinking_level = next_lvl
                                continue
                            if use_include_thoughts:
                                log(
                                    "CH-A",
                                    f"GenerateContentConfig validation failed with include_thoughts=True ({cfg_err}). Disabling include_thoughts.",
                                    show_id=show_id,
                                )
                                use_include_thoughts = False
                                continue
                            raise cfg_err

                        log(
                            "CH-A",
                            f"Calling Gemini model '{current_model}' via transport=stream_with_thoughts "
                            f"(attempt {attempt}/{max_retries}, thinking_level='{current_thinking_level}', max_output_tokens=65536)...",
                            show_id=show_id,
                        )

                        try:
                            response = client.models.generate_content_stream(
                                model=current_model,
                                contents=[user_prompt],
                                config=gen_config_obj,
                            )

                            chunks_text = []
                            usage_meta = None
                            for chunk in response:
                                if getattr(chunk, "usage_metadata", None):
                                    usage_meta = chunk.usage_metadata
                                has_parts = False
                                if hasattr(chunk, "candidates") and chunk.candidates:
                                    cand = chunk.candidates[0]
                                    if hasattr(cand, "content") and cand.content and hasattr(cand.content, "parts") and cand.content.parts:
                                        has_parts = True
                                        for part in cand.content.parts:
                                            if getattr(part, "thought", False):
                                                continue
                                            p_text = getattr(part, "text", "")
                                            if p_text:
                                                chunks_text.append(p_text)
                                if not has_parts and getattr(chunk, "text", None):
                                    chunks_text.append(chunk.text)

                            full_text = "".join(chunks_text).strip()
                            break
                        except Exception as stream_err:
                            err_lower = str(stream_err).lower()
                            is_thinking_err = any(
                                kw in err_lower
                                for kw in [
                                    "thinking",
                                    "thinking_level",
                                    "thinking_config",
                                    "include_thoughts",
                                    "thought",
                                ]
                            ) and not is_transport_disconnect_or_timeout(stream_err)
                            if is_thinking_err and current_thinking_level:
                                next_lvl = get_next_fallback_thinking_level(current_thinking_level)
                                log(
                                    "CH-A",
                                    f"Stream fallback rejected thinking_level='{current_thinking_level}' ({stream_err}). "
                                    f"Degrading: '{current_thinking_level}' -> {_format_level(next_lvl)} and retrying immediately...",
                                    show_id=show_id,
                                )
                                current_thinking_level = next_lvl
                                continue
                            if is_thinking_err and use_include_thoughts:
                                log(
                                    "CH-A",
                                    f"Stream fallback rejected include_thoughts=True ({stream_err}). Disabling include_thoughts and retrying immediately...",
                                    show_id=show_id,
                                )
                                use_include_thoughts = False
                                continue
                            raise stream_err

                    if not full_text:
                        raise ValueError(f"Received empty streamed response from model '{current_model}'.")

                    cleaned_text = clean_json_markdown(full_text)
                    parsed = response_schema.model_validate_json(cleaned_text)

                    input_tokens = getattr(usage_meta, "prompt_token_count", None) or 0
                    thought_tokens = getattr(usage_meta, "thoughts_token_count", None) or 0
                    output_tokens = getattr(usage_meta, "candidates_token_count", None) or 0
                    total_time = time.time() - t_attempt_start

                    log(
                        "CH-A",
                        f"Channel A call completed: transport=stream_with_thoughts, model='{current_model}', "
                        f"thinking_level='{current_thinking_level}', total_time={total_time:.1f}s, "
                        f"tokens=(input={input_tokens}, thought={thought_tokens}, output={output_tokens}).",
                        show_id=show_id,
                    )
                    if stage in MODEL_USAGE_STATS:
                        MODEL_USAGE_STATS[stage][current_model] = MODEL_USAGE_STATS[stage].get(current_model, 0) + 1
                    return parsed

            except Exception as e:
                last_exception = e
                err_str = str(e)
                log("ERROR", f"[CH-A] Attempt {attempt}/{max_retries} with model '{current_model}' failed: {e}", show_id=show_id)

                is_404 = "404" in err_str or "NOT_FOUND" in err_str or "NotFound" in type(e).__name__
                if is_404:
                    log("ERROR", f"[CH-A] Model '{current_model}' returned 404/NOT_FOUND. Skipping model immediately.", show_id=show_id)
                    skipped_404 = True
                    break

                if isinstance(e, TimeoutError):
                    consecutive_bg_timeouts += 1
                    if consecutive_bg_timeouts >= 2:
                        log(
                            "ERROR",
                            f"[CH-A] Model '{current_model}' reached {consecutive_bg_timeouts} consecutive background timeouts. Moving to next candidate model.",
                            show_id=show_id,
                        )
                        break
                else:
                    consecutive_bg_timeouts = 0

                if is_transport_disconnect_or_timeout(e):
                    consecutive_disconnects += 1
                    try:
                        if hasattr(client, "close"):
                            client.close()
                    except Exception:
                        pass
                    if callable(client_factory):
                        try:
                            client = client_factory()
                            log("CH-A", "Rebuilt Gemini client after transport disconnect/timeout.", show_id=show_id)
                        except Exception as rebuild_err:
                            log("WARN", f"Failed to rebuild Gemini client: {rebuild_err}", show_id=show_id)

                    if consecutive_disconnects >= 2:
                        if current_thinking_level is not None:
                            next_lvl = get_next_fallback_thinking_level(current_thinking_level)
                            log(
                                "CH-A",
                                f"2 consecutive disconnects at thinking_level='{current_thinking_level}', stepping down to {_format_level(next_lvl)}",
                                show_id=show_id,
                            )
                            current_thinking_level = next_lvl
                        consecutive_disconnects = 0
                else:
                    consecutive_disconnects = 0

                if attempt < max_retries:
                    delay = backoff_delays[attempt - 1] if (attempt - 1) < len(backoff_delays) else 30
                    log("CH-A", f"Retrying Channel A model '{current_model}' after {delay}s backoff (attempt {attempt}/{max_retries})...", show_id=show_id)
                    time.sleep(delay)
                else:
                    log("ERROR", f"[CH-A] All {max_retries} attempts exhausted for model '{current_model}'.", show_id=show_id)

        if m_idx < len(model_pool) - 1:
            if not skipped_404:
                log("CH-A", f"Model '{current_model}' exhausted. Sleeping 10s before switching to next candidate model...", show_id=show_id)
                time.sleep(10)

    if last_exception is not None:
        raise last_exception
    raise RuntimeError(f"All Channel A models in pool {model_pool} exhausted without returning a valid response.")


def _transcribe_single_chunk(
    client: genai.Client,
    api_key: str,
    chunk_path: Path,
    model_pool: list[str],
    system_instruction: str,
    thinking_level: str,
    show_id: str = "",
    max_retries: int = 5,
    rate_limiter: Optional[SlidingWindowRateLimiter] = None,
    fail_fast_on_rpd: bool = False,
    is_free_tier: bool = False,
    usage_tier_tag: str = "",
    user_prompt: str = CHANNEL_B_USER_PROMPT,
) -> ChannelBOutput:
    """Upload one audio chunk to the client's project File API, transcribe, and clean up cloud file."""
    tier_prefix = f"[{usage_tier_tag.upper()}] " if usage_tier_tag else ""
    log("CH-B", f"{tier_prefix}Uploading {chunk_path.name} to Gemini File API...", show_id=show_id)
    uploaded = None
    for up_attempt in range(1, 4):
        try:
            uploaded = client.files.upload(file=str(chunk_path))
            break
        except Exception as up_err:
            if fail_fast_on_rpd and is_api_key_auth_error(up_err):
                raise QuotaExhaustedRPDError(model_pool[0], str(up_err), auth_error=True) from up_err
            if up_attempt == 3:
                raise
            log("CH-B", f"{tier_prefix}Upload attempt {up_attempt} failed: {up_err}. Retrying in 5s...", show_id=show_id)
            time.sleep(5)

    log("CH-B", f"{tier_prefix}Uploaded cloud file: {uploaded.name}. Polling for ACTIVE state...", show_id=show_id)
    try:
        file_info = client.files.get(name=uploaded.name)
        while not str(file_info.state).endswith("ACTIVE"):
            if str(file_info.state).endswith("FAILED"):
                raise RuntimeError(f"Cloud file {uploaded.name} entered FAILED state.")
            time.sleep(2)
            file_info = client.files.get(name=uploaded.name)
        log("CH-B", f"{tier_prefix}Cloud file {uploaded.name} is ACTIVE.", show_id=show_id)

        contents = [file_info, user_prompt]

        return generate_structured_with_retry(
            client=client,
            model_pool=model_pool,
            contents=contents,
            system_instruction=system_instruction,
            response_schema=ChannelBOutput,
            stage="CH-B",
            temperature=0.2,
            max_retries=max_retries,
            thinking_level=thinking_level,
            show_id=show_id,
            client_factory=lambda: create_genai_client(api_key=api_key),
            max_output_tokens=32768,
            rate_limiter=rate_limiter,
            fail_fast_on_rpd=fail_fast_on_rpd,
            is_free_tier=is_free_tier,
            usage_tier_tag=usage_tier_tag,
        )
    finally:
        try:
            client.files.delete(name=uploaded.name)
            log("CH-B", f"{tier_prefix}Deleted cloud file {uploaded.name}.", show_id=show_id)
        except Exception as del_err:
            log("CH-B", f"{tier_prefix}Warning: Failed to delete cloud file {uploaded.name}: {del_err}", show_id=show_id)


def run_channel_b(
    chunks: list[Path],
    glossary_text: str,
    duration_sec: int,
    max_chunks: int = 0,
    show_notes: str = "",
    show_title: str = "",
    show_subtitle: str = "",
    show_id: str = "",
    content_style: str = "analytical",
    speaker_roster: Optional[list[str]] = None,
) -> list[dict]:
    """[5. CHANNEL_B] 逐片上传 Gemini File API → 注入术语与 Show Notes 转写+翻译 → 立即删除云端文件.

    Supports Free-First + Paid-Overflow dual-key routing when GEMINI_API_KEY_CH_B is configured:
    - Uses GEMINI_API_KEY_CH_B (Free Tier) with strict 5 RPM sliding-window + smooth-spacing limiter
    - Tracks RPD budget & detects 429 PerDay exhaustion immediately (zero wasted retries)
    - On RPD exhaustion, re-uploads the chunk using Paid GEMINI_API_KEY (project-isolated File API)
      and seamlessly continues with the primary model (gemini-4-hydrogen)
    """
    free_key, paid_key = get_ch_b_api_keys()
    free_mode = bool(free_key)
    free_rpm = get_ch_b_free_rpm()
    free_rpd_budget = get_ch_b_free_rpd()
    overflow_to_paid = get_ch_b_overflow_to_paid()

    free_client: Optional[genai.Client] = create_genai_client(api_key=free_key) if free_mode else None
    paid_client: Optional[genai.Client] = None if free_mode else create_genai_client(api_key=paid_key)

    def get_paid_client() -> genai.Client:
        nonlocal paid_client
        if paid_client is None:
            paid_client = create_genai_client(api_key=paid_key)
        return paid_client

    try:
        model_pool = get_ch_b_model_pool()
        primary_model = model_pool[0]
        log("CH-B", f"Channel B model pool configured: {model_pool}", show_id=show_id)

        if free_mode:
            log(
                "CH-B",
                f"Channel B Free Tier mode enabled (separate GEMINI_API_KEY_CH_B detected; "
                f"rpm={free_rpm}, rpd_budget={free_rpd_budget}, overflow_to_paid={overflow_to_paid}).",
                show_id=show_id,
            )
            rate_limiter = CH_B_QUOTA_STATE.get_rate_limiter(free_rpm)
        else:
            log(
                "CH-B",
                "Channel B running in Paid/Single-Key mode (GEMINI_API_KEY_CH_B not set or matches GEMINI_API_KEY).",
                show_id=show_id,
            )
            rate_limiter = None

        thinking_level = get_env_stripped("GEMINI_THINKING_CH_B", "low")
        log("CH-B", f"Channel B thinking level configured: '{thinking_level}'", show_id=show_id)

        chunk_sleep_raw = get_env_stripped("CHUNK_SLEEP_SEC", "2.0")
        try:
            chunk_sleep_sec = float(chunk_sleep_raw)
        except ValueError:
            chunk_sleep_sec = 2.0
        log("CH-B", f"Channel B chunk sleep configured: {chunk_sleep_sec}s", show_id=show_id)

        chunks_to_process = chunks[:max_chunks] if max_chunks > 0 else chunks
        log("CH-B", f"Starting Channel B processing for {len(chunks_to_process)} chunk(s) (total chunks: {len(chunks)})...", show_id=show_id)

        system_instruction = build_channel_b_system_instruction(
            show_title=show_title,
            show_subtitle=show_subtitle,
            glossary_text=glossary_text,
            show_notes=show_notes,
            content_style=content_style,
            speaker_roster=speaker_roster,
        )

        all_raw_segments = []
        observed_speakers: list[str] = []
        observed_speakers_set: set[str] = set()
        accumulated_chunk_start_sec = 0
        free_chunks_ok = 0
        paid_chunks_ok = 0

        for idx, chunk_path in enumerate(chunks_to_process):
            log("CH-B", f"--- Processing chunk [{idx + 1}/{len(chunks_to_process)}]: {chunk_path.name} ---", show_id=show_id)
            chunk_dur = get_audio_duration_sec(chunk_path, show_id=show_id)
            log("CH-B", f"Chunk duration via ffprobe: {chunk_dur}s (accumulated base: {accumulated_chunk_start_sec}s).", show_id=show_id)

            chunk_user_prompt = build_channel_b_user_prompt(
                chunk_index=idx,
                total_chunks=len(chunks_to_process),
                prev_tail_segments=all_raw_segments[-2:] if all_raw_segments else None,
                observed_speakers=observed_speakers,
            )

            parsed_resp: ChannelBOutput
            if free_mode:
                can_free, free_reason = CH_B_QUOTA_STATE.can_use_free_for_model(primary_model, free_rpd_budget)
                if can_free and free_client is not None:
                    if overflow_to_paid and paid_key:
                        try:
                            parsed_resp = _transcribe_single_chunk(
                                client=free_client,
                                api_key=free_key,
                                chunk_path=chunk_path,
                                model_pool=[primary_model],
                                system_instruction=system_instruction,
                                thinking_level=thinking_level,
                                show_id=show_id,
                                max_retries=3,
                                rate_limiter=rate_limiter,
                                fail_fast_on_rpd=True,
                                is_free_tier=True,
                                usage_tier_tag="free",
                                user_prompt=chunk_user_prompt,
                            )
                            used_now = CH_B_QUOTA_STATE.record_free_success(primary_model)
                            free_chunks_ok += 1
                            budget_desc = f"{used_now}/{free_rpd_budget}" if free_rpd_budget > 0 else str(used_now)
                            log("CH-B", f"Free Tier chunk succeeded (Free RPD used this run for '{primary_model}': {budget_desc}).", show_id=show_id)
                        except QuotaExhaustedRPDError as q_err:
                            CH_B_QUOTA_STATE.mark_free_rpd_exhausted(primary_model, disable_key=q_err.auth_error)
                            log(
                                "WARN",
                                f"Free Tier exhausted/unavailable for '{primary_model}' ({q_err}). "
                                f"Re-uploading chunk via Paid Tier key to continue with '{primary_model}'...",
                                show_id=show_id,
                            )
                            parsed_resp = _transcribe_single_chunk(
                                client=get_paid_client(),
                                api_key=paid_key,
                                chunk_path=chunk_path,
                                model_pool=model_pool,
                                system_instruction=system_instruction,
                                thinking_level=thinking_level,
                                show_id=show_id,
                                rate_limiter=None,
                                fail_fast_on_rpd=False,
                                is_free_tier=False,
                                usage_tier_tag="paid",
                                user_prompt=chunk_user_prompt,
                            )
                            paid_chunks_ok += 1
                        except Exception as free_err:
                            if is_service_unavailable_error(free_err):
                                consec_503, tripped = CH_B_QUOTA_STATE.record_free_503_chunk_failure(
                                    primary_model, threshold=2
                                )
                                if tripped:
                                    log(
                                        "WARN",
                                        f"Free Tier hit {consec_503} consecutive 503 UNAVAILABLE chunk failures on '{primary_model}'. "
                                        f"Tripping Free Tier 503 circuit breaker for this run; falling back this and remaining chunks to Paid Tier ('{primary_model}')...",
                                        show_id=show_id,
                                    )
                                else:
                                    log(
                                        "WARN",
                                        f"Free Tier 503 UNAVAILABLE on chunk '{chunk_path.name}' (consecutive 503 chunks: {consec_503}/2; {free_err}). "
                                        f"Falling back this chunk to Paid Tier key ('{primary_model}')...",
                                        show_id=show_id,
                                    )
                            else:
                                CH_B_QUOTA_STATE.reset_consecutive_503(primary_model)
                                log(
                                    "WARN",
                                    f"Free Tier failed on chunk '{chunk_path.name}' ({free_err}). "
                                    f"Falling back this chunk to Paid Tier key ('{primary_model}')...",
                                    show_id=show_id,
                                )
                            parsed_resp = _transcribe_single_chunk(
                                client=get_paid_client(),
                                api_key=paid_key,
                                chunk_path=chunk_path,
                                model_pool=model_pool,
                                system_instruction=system_instruction,
                                thinking_level=thinking_level,
                                show_id=show_id,
                                rate_limiter=None,
                                fail_fast_on_rpd=False,
                                is_free_tier=False,
                                usage_tier_tag="paid",
                                user_prompt=chunk_user_prompt,
                            )
                            paid_chunks_ok += 1
                    else:
                        available_free_pool = [
                            m for m in model_pool if m not in CH_B_QUOTA_STATE.free_rpd_exhausted_models
                        ] or list(model_pool)
                        parsed_resp = _transcribe_single_chunk(
                            client=free_client,
                            api_key=free_key,
                            chunk_path=chunk_path,
                            model_pool=available_free_pool,
                            system_instruction=system_instruction,
                            thinking_level=thinking_level,
                            show_id=show_id,
                            rate_limiter=rate_limiter,
                            fail_fast_on_rpd=False,
                            is_free_tier=True,
                            usage_tier_tag="free",
                            user_prompt=chunk_user_prompt,
                        )
                        CH_B_QUOTA_STATE.record_free_success(available_free_pool[0])
                        free_chunks_ok += 1
                else:
                    if overflow_to_paid and paid_key:
                        log(
                            "CH-B",
                            f"Skipping Free Tier for chunk [{idx + 1}/{len(chunks_to_process)}] ({free_reason}); "
                            f"routing directly to Paid Tier ('{primary_model}')...",
                            show_id=show_id,
                        )
                        parsed_resp = _transcribe_single_chunk(
                            client=get_paid_client(),
                            api_key=paid_key,
                            chunk_path=chunk_path,
                            model_pool=model_pool,
                            system_instruction=system_instruction,
                            thinking_level=thinking_level,
                            show_id=show_id,
                            rate_limiter=None,
                            fail_fast_on_rpd=False,
                            is_free_tier=False,
                            usage_tier_tag="paid",
                            user_prompt=chunk_user_prompt,
                        )
                        paid_chunks_ok += 1
                    else:
                        remaining_pool = [
                            m for m in model_pool if m not in CH_B_QUOTA_STATE.free_rpd_exhausted_models
                        ]
                        if not remaining_pool or free_client is None:
                            raise QuotaExhaustedRPDError(
                                primary_model,
                                f"Free Tier quota exhausted ({free_reason}) and Paid overflow is disabled.",
                            )
                        log(
                            "CH-B",
                            f"Free Tier primary model unavailable ({free_reason}); "
                            f"falling back on Free Tier to remaining pool: {remaining_pool}",
                            show_id=show_id,
                        )
                        parsed_resp = _transcribe_single_chunk(
                            client=free_client,
                            api_key=free_key,
                            chunk_path=chunk_path,
                            model_pool=remaining_pool,
                            system_instruction=system_instruction,
                            thinking_level=thinking_level,
                            show_id=show_id,
                            rate_limiter=rate_limiter,
                            fail_fast_on_rpd=False,
                            is_free_tier=True,
                            usage_tier_tag="free",
                            user_prompt=chunk_user_prompt,
                        )
                        CH_B_QUOTA_STATE.record_free_success(remaining_pool[0])
                        free_chunks_ok += 1
            else:
                parsed_resp = _transcribe_single_chunk(
                    client=get_paid_client(),
                    api_key=paid_key,
                    chunk_path=chunk_path,
                    model_pool=model_pool,
                    system_instruction=system_instruction,
                    thinking_level=thinking_level,
                    show_id=show_id,
                    rate_limiter=None,
                    fail_fast_on_rpd=False,
                    is_free_tier=False,
                    usage_tier_tag="",
                    user_prompt=chunk_user_prompt,
                )
                paid_chunks_ok += 1

            chunk_segments = parsed_resp.segments
            log("CH-B", f"Chunk [{idx + 1}/{len(chunks_to_process)}] produced {len(chunk_segments)} segment(s).", show_id=show_id)
            for seg in chunk_segments:
                offset = parse_mmss_to_sec(seg.timestamp, fallback_sec=0)
                clamped_offset = max(0, min(offset, chunk_dur))
                global_start = accumulated_chunk_start_sec + clamped_offset
                clamped_start = max(0, min(global_start, duration_sec))
                norm_spk = normalize_speaker(seg.speaker)
                if norm_spk and norm_spk not in observed_speakers_set:
                    observed_speakers_set.add(norm_spk)
                    observed_speakers.append(norm_spk)
                all_raw_segments.append({
                    "start_sec": clamped_start,
                    "speaker": norm_spk,
                    "en": seg.en.strip(),
                    "zh": seg.zh.strip(),
                })

            accumulated_chunk_start_sec += chunk_dur

            if idx < len(chunks_to_process) - 1 and chunk_sleep_sec > 0:
                log("CH-B", f"Sleeping {chunk_sleep_sec}s between chunks as per rate-limiting policy...", show_id=show_id)
                time.sleep(chunk_sleep_sec)

        final_transcript = []
        prev_start_sec = 0
        for i, seg in enumerate(all_raw_segments):
            start_sec = seg["start_sec"]
            if start_sec < prev_start_sec:
                start_sec = prev_start_sec
            else:
                prev_start_sec = start_sec

            final_transcript.append({
                "id": i + 1,
                "start_sec": start_sec,
                "speaker": normalize_speaker(seg["speaker"]),
                "en": seg["en"],
                "zh": seg["zh"],
            })

        if free_mode:
            used_total = CH_B_QUOTA_STATE.free_rpd_used.get(primary_model, 0)
            budget_str = f"{used_total}/{free_rpd_budget}" if free_rpd_budget > 0 else str(used_total)
            log(
                "CH-B",
                f"Channel B completed successfully. Total transcript items: {len(final_transcript)} | "
                f"Chunks: {len(chunks_to_process)} (free={free_chunks_ok}, paid_overflow={paid_chunks_ok}) | "
                f"Free RPD used this run: {budget_str}.",
                show_id=show_id,
            )
        else:
            log("CH-B", f"Channel B completed successfully. Total transcript items: {len(final_transcript)}.", show_id=show_id)
        return final_transcript
    finally:
        for c in (free_client, paid_client):
            if c is not None and hasattr(c, "close"):
                try:
                    c.close()
                except Exception:
                    pass


def run_channel_a(
    transcript: list[dict],
    glossary_text: str,
    title: str = "",
    duration_sec: int = 0,
    show_notes: str = "",
    show_title: str = "",
    show_subtitle: str = "",
    show_id: str = "",
    content_style: str = "analytical",
) -> dict:
    """[6. CHANNEL_A] 将拼合的纯文本带时间戳逐字稿单次发给 Gemini，生成摘要与 Highlights."""
    api_key = get_env_stripped("GEMINI_API_KEY")
    client = create_genai_client(api_key=api_key)

    try:
        model_pool = get_ch_a_model_pool()
        log("CH-A", f"Channel A model pool configured: {model_pool}", show_id=show_id)

        env_thinking_ch_a = get_env_stripped("GEMINI_THINKING_CH_A")
        if env_thinking_ch_a:
            log("CH-A", f"Channel A thinking level env override: '{env_thinking_ch_a}'", show_id=show_id)
        else:
            log("CH-A", "Channel A thinking level: model-adaptive (no GEMINI_THINKING_CH_A override)", show_id=show_id)

        for m in model_pool:
            res_lvl = resolve_ch_a_thinking_level(m, env_thinking_ch_a)
            log("CH-A", f"  - Model '{m}': initial thinking_level='{res_lvl}'", show_id=show_id)

        lines = [f"[{format_sec_to_mmss(item['start_sec'])}] {item['speaker']}: {item['en']}" for item in transcript]
        transcript_text = "\n".join(lines)

        system_instruction = build_channel_a_system_instruction(
            show_title=show_title,
            show_subtitle=show_subtitle,
            glossary_text=glossary_text,
            show_notes=show_notes,
            content_style=content_style,
        )
        user_prompt = build_channel_a_user_prompt(
            title=title,
            show_title=show_title,
            duration_sec=duration_sec,
            transcript_text=transcript_text,
        )

        input_chars = len(user_prompt)
        log(
            "CH-A",
            f"Prepared Channel A input: input_chars={input_chars}, transcript_lines={len(lines)}",
            show_id=show_id,
        )
        log("CH-A", f"Starting Channel A summarization for episode '{title}' (duration: {duration_sec}s, content_style: {content_style})...", show_id=show_id)

        parsed_resp: ChannelAOutput = generate_channel_a_structured_with_retry(
            client=client,
            model_pool=model_pool,
            user_prompt=user_prompt,
            contents=[user_prompt],
            system_instruction=system_instruction,
            response_schema=ChannelAOutput,
            temperature=0.2,
            thinking_level_resolver=lambda m: resolve_ch_a_thinking_level(m, env_thinking_ch_a),
            show_id=show_id,
            client_factory=lambda: create_genai_client(api_key=api_key),
        )

        highlights = clamp_and_sort_highlights(parsed_resp.highlights, duration_sec)
        key_takeaways = clamp_key_takeaways(parsed_resp.key_takeaways_zh)
        vocab_notes = clamp_and_sort_vocab_notes(parsed_resp.vocab_notes, duration_sec)
        references = clamp_and_sort_references(parsed_resp.references, duration_sec)

        result = {
            "summary_zh": parsed_resp.summary_zh.strip(),
            "key_takeaways_zh": key_takeaways,
            "highlights": highlights,
            "vocab_notes": vocab_notes,
            "references": references,
        }

        log(
            "CH-A",
            f"Channel A completed. Summary length: {len(result['summary_zh'])} chars, "
            f"Key takeaways: {len(result['key_takeaways_zh'])}, Highlights: {len(result['highlights'])}, "
            f"Vocab notes: {len(result['vocab_notes'])}, References: {len(result['references'])}.",
            show_id=show_id,
        )
        return result
    finally:
        try:
            if hasattr(client, "close"):
                client.close()
        except Exception:
            pass
