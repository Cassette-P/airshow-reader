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
) -> BaseModel:
    """Execute structured generation using streaming response with 5-retry backoff across model_pool."""
    backoff_delays = [15, 20, 25, 30]
    last_exception = None

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
                            if is_thinking_err:
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

                cleaned_text = full_text
                if cleaned_text.startswith("```json"):
                    cleaned_text = cleaned_text[7:]
                elif cleaned_text.startswith("```"):
                    cleaned_text = cleaned_text[3:]
                if cleaned_text.endswith("```"):
                    cleaned_text = cleaned_text[:-3]
                cleaned_text = cleaned_text.strip()

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


def run_channel_b(
    chunks: list[Path],
    glossary_text: str,
    duration_sec: int,
    max_chunks: int = 0,
    show_notes: str = "",
    show_title: str = "",
    show_subtitle: str = "",
    show_id: str = "",
) -> list[dict]:
    """[5. CHANNEL_B] 逐片上传 Gemini File API → 注入术语与 Show Notes 转写+翻译 → 立即删除云端文件."""
    api_key = get_env_stripped("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

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


def run_channel_a(
    transcript: list[dict],
    glossary_text: str,
    title: str = "",
    duration_sec: int = 0,
    show_notes: str = "",
    show_title: str = "",
    show_subtitle: str = "",
    show_id: str = "",
) -> dict:
    """[6. CHANNEL_A] 将拼合的纯文本带时间戳逐字稿单次发给 Gemini，生成摘要与 Highlights."""
    api_key = get_env_stripped("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

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

    log("CH-A", f"Starting Channel A summarization for episode '{title}' (duration: {duration_sec}s)...", show_id=show_id)

    lines = [f"[{format_sec_to_mmss(item['start_sec'])}] ({item['start_sec']}s) {item['speaker']}: {item['en']}" for item in transcript]
    transcript_text = "\n".join(lines)

    system_instruction = build_channel_a_system_instruction(
        show_title=show_title,
        show_subtitle=show_subtitle,
        glossary_text=glossary_text,
        show_notes=show_notes,
    )
    user_prompt = build_channel_a_user_prompt(
        title=title,
        show_title=show_title,
        duration_sec=duration_sec,
        transcript_text=transcript_text,
    )

    parsed_resp: ChannelAOutput = generate_structured_with_retry(
        client=client,
        model_pool=model_pool,
        contents=[user_prompt],
        system_instruction=system_instruction,
        response_schema=ChannelAOutput,
        stage="CH-A",
        temperature=0.2,
        thinking_level=None,
        thinking_level_resolver=lambda m: resolve_ch_a_thinking_level(m, env_thinking_ch_a),
        show_id=show_id,
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
