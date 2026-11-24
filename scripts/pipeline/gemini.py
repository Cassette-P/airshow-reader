"""Gemini File API integration, streaming structured generation, thinking level adaptation, and disaster recovery backoff."""

from pathlib import Path
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
    get_ch_b_model_pool,
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
                    MODEL_USAGE_STATS[stage][current_model] = MODEL_USAGE_STATS[stage].get(current_model, 0) + 1
                return parsed

            except Exception as e:
                last_exception = e
                err_str = str(e)
                log("ERROR", f"[{stage}] Attempt {attempt}/{max_retries} with model '{current_model}' failed: {e}", show_id=show_id)

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
                    delay = backoff_delays[attempt - 1] if (attempt - 1) < len(backoff_delays) else 30
                    log(stage, f"Retrying model '{current_model}' after {delay}s backoff (attempt {attempt}/{max_retries})...", show_id=show_id)
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
        else:
            resolved_thinking_level = resolve_ch_a_thinking_level(current_model)

        current_thinking_level = resolved_thinking_level
        log(
            "CH-A",
            f"Using Channel A model candidate [{m_idx + 1}/{len(model_pool)}]: '{current_model}' "
            f"(initial thinking_level='{current_thinking_level}')...",
            show_id=show_id,
        )

        skipped_404 = False
        consecutive_disconnects = 0
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
                        else:
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
                            elif status in ("failed", "incomplete", "cancelled", "budget_exceeded"):
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
                        if "CH-A" in MODEL_USAGE_STATS:
                            MODEL_USAGE_STATS["CH-A"][current_model] = MODEL_USAGE_STATS["CH-A"].get(current_model, 0) + 1
                        return parsed

                # Mode 2: Fallback stream_with_thoughts
                if force_fallback_stream:
                    while current_thinking_level:
                        try:
                            _ = types.ThinkingConfig(thinking_level=current_thinking_level, include_thoughts=True)
                            break
                        except Exception as t_init_err:
                            next_lvl = get_next_fallback_thinking_level(current_thinking_level)
                            log(
                                "CH-A",
                                f"Local SDK does not support thinking_level='{current_thinking_level}' with include_thoughts=True ({t_init_err}). "
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
                            include_thoughts=True,
                        )
                    else:
                        try:
                            config_kwargs["thinking_config"] = types.ThinkingConfig(include_thoughts=True)
                        except Exception:
                            pass

                    gen_config_obj = types.GenerateContentConfig(**config_kwargs)

                    log(
                        "CH-A",
                        f"Calling Gemini model '{current_model}' via transport=stream_with_thoughts "
                        f"(attempt {attempt}/{max_retries}, thinking_level='{current_thinking_level}', max_output_tokens=65536)...",
                        show_id=show_id,
                    )

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
                    if "CH-A" in MODEL_USAGE_STATS:
                        MODEL_USAGE_STATS["CH-A"][current_model] = MODEL_USAGE_STATS["CH-A"].get(current_model, 0) + 1
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
    """[5. CHANNEL_B] 逐片上传 Gemini File API → 注入术语与 Show Notes 转写+翻译 → 立即删除云端文件."""
    api_key = get_env_stripped("GEMINI_API_KEY")
    client = create_genai_client(api_key=api_key)

    try:
        model_pool = get_ch_b_model_pool()
        log("CH-B", f"Channel B model pool configured: {model_pool}", show_id=show_id)

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
        accumulated_chunk_start_sec = 0

        for idx, chunk_path in enumerate(chunks_to_process):
            log("CH-B", f"--- Processing chunk [{idx + 1}/{len(chunks_to_process)}]: {chunk_path.name} ---", show_id=show_id)
            chunk_dur = get_audio_duration_sec(chunk_path, show_id=show_id)
            log("CH-B", f"Chunk duration via ffprobe: {chunk_dur}s (accumulated base: {accumulated_chunk_start_sec}s).", show_id=show_id)

            log("CH-B", f"Uploading {chunk_path.name} to Gemini File API...", show_id=show_id)
            uploaded = None
            for up_attempt in range(1, 4):
                try:
                    uploaded = client.files.upload(file=str(chunk_path))
                    break
                except Exception as up_err:
                    if up_attempt == 3:
                        raise
                    log("CH-B", f"Upload attempt {up_attempt} failed: {up_err}. Retrying in 5s...", show_id=show_id)
                    time.sleep(5)

            log("CH-B", f"Uploaded cloud file: {uploaded.name}. Polling for ACTIVE state...", show_id=show_id)
            try:
                file_info = client.files.get(name=uploaded.name)
                while not str(file_info.state).endswith("ACTIVE"):
                    if str(file_info.state).endswith("FAILED"):
                        raise RuntimeError(f"Cloud file {uploaded.name} entered FAILED state.")
                    time.sleep(2)
                    file_info = client.files.get(name=uploaded.name)
                log("CH-B", f"Cloud file {uploaded.name} is ACTIVE.", show_id=show_id)

                contents = [file_info, CHANNEL_B_USER_PROMPT]

                parsed_resp: ChannelBOutput = generate_structured_with_retry(
                    client=client,
                    model_pool=model_pool,
                    contents=contents,
                    system_instruction=system_instruction,
                    response_schema=ChannelBOutput,
                    stage="CH-B",
                    temperature=0.2,
                    thinking_level=thinking_level,
                    show_id=show_id,
                    client_factory=lambda: create_genai_client(api_key=api_key),
                )

                chunk_segments = parsed_resp.segments
                log("CH-B", f"Chunk [{idx + 1}/{len(chunks_to_process)}] produced {len(chunk_segments)} segment(s).", show_id=show_id)
                for seg in chunk_segments:
                    offset = parse_mmss_to_sec(seg.timestamp, fallback_sec=0)
                    clamped_offset = max(0, min(offset, chunk_dur))
                    global_start = accumulated_chunk_start_sec + clamped_offset
                    clamped_start = max(0, min(global_start, duration_sec))
                    all_raw_segments.append({
                        "start_sec": clamped_start,
                        "speaker": normalize_speaker(seg.speaker),
                        "en": seg.en.strip(),
                        "zh": seg.zh.strip(),
                    })

            finally:
                try:
                    client.files.delete(name=uploaded.name)
                    log("CH-B", f"Deleted cloud file {uploaded.name}.", show_id=show_id)
                except Exception as del_err:
                    log("CH-B", f"Warning: Failed to delete cloud file {uploaded.name}: {del_err}", show_id=show_id)

            accumulated_chunk_start_sec += chunk_dur

            if idx < len(chunks_to_process) - 1:
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

        log("CH-B", f"Channel B completed successfully. Total transcript items: {len(final_transcript)}.", show_id=show_id)
        return final_transcript
    finally:
        try:
            if hasattr(client, "close"):
                client.close()
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
