"""Glossary loading and Channel B / Channel A / Enrich prompt assembly."""

import sys

from pipeline.config import (
    LEGACY_GLOSSARY_FILE,
    PROJECT_ROOT,
    log,
)

CHANNEL_B_USER_PROMPT = (
    "Please transcribe and translate this podcast audio chunk into structured bilingual segments "
    "according to the rules and schema."
)


def load_glossary_for_show(show: dict) -> str:
    """Load glossary file specified by show config."""
    glossary_rel = show.get("glossary_file", "")
    if not glossary_rel:
        return ""
    glossary_path = PROJECT_ROOT / glossary_rel
    if not glossary_path.exists():
        if show.get("id") == "airshow" and LEGACY_GLOSSARY_FILE.exists():
            glossary_path = LEGACY_GLOSSARY_FILE
        else:
            log("ERROR", f"Glossary file not found at {glossary_path}", show_id=show.get("id", ""))
            sys.exit(1)
    for enc in ["utf-8", "utf-8-sig", "gbk", "gb18030"]:
        try:
            with open(glossary_path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    log("ERROR", f"Failed to decode glossary file {glossary_path}", show_id=show.get("id", ""))
    sys.exit(1)


def build_channel_b_system_instruction(
    show_title: str,
    show_subtitle: str,
    glossary_text: str,
    show_notes: str = "",
) -> str:
    """Build Channel B system instruction prompt."""
    system_instruction = (
        f"You are an expert bilingual podcast transcriptionist and translator for '{show_title}' ({show_subtitle}).\n"
        "Your task is to transcribe and translate the provided podcast audio chunk into Chinese and English bilingual segments.\n\n"
        "Rules:\n"
        "1. Segment into natural semantic blocks of 2-4 sentences each.\n"
        "2. For each block, provide:\n"
        "   - timestamp: string timestamp relative to the beginning of this audio chunk, strictly formatted as 'MM:SS' (e.g., '00:02', '01:06', '02:55'). Never omit the colon.\n"
        "   - speaker: speaker full English name normalized according to the glossary and episode show notes (use 'Guest' only if speaker name cannot be identified).\n"
        "   - en: the original English spoken text (2-4 sentences per block).\n"
        "   - zh: natural, fluent, professional Chinese translation adhering strictly to the glossary and domain conventions.\n"
        "3. Adhere strictly to the following speaker and terminology glossary:\n\n"
        f"{glossary_text}"
    )
    if show_notes:
        system_instruction += (
            f"\n\nEpisode Show Notes (for guest names, topics, and context reference):\n"
            f"{show_notes}"
        )
    return system_instruction


def build_channel_a_system_instruction(
    show_title: str,
    show_subtitle: str,
    glossary_text: str,
    show_notes: str = "",
) -> str:
    """Build Channel A executive summarization system instruction prompt."""
    system_instruction = (
        f"You are an executive podcast intelligence analyst and editor for '{show_title}' ({show_subtitle}).\n"
        "Analyze the provided timestamped podcast transcript and produce a structured Chinese executive summary, "
        "along with selected vocabulary notes and referenced works.\n\n"
        "Requirements:\n"
        "1. summary_zh: A comprehensive core summary in Chinese (200-350 Chinese characters) covering main issues, perspectives, disagreements, and conclusions.\n"
        "2. key_takeaways_zh: Exactly 3 to 5 key takeaways in Chinese, each being a clear, sharp one-sentence conclusion.\n"
        "3. highlights: 4 to 10 key highlights/chapters from the episode, sorted chronologically by start_sec. Each highlight must include:\n"
        "   - start_sec: integer seconds where this key topic begins (must correspond to actual transcript timestamps and be within [0, duration_sec]).\n"
        "   - title_zh: concise Chinese topic title (strictly 15 Chinese characters or fewer).\n"
        "   - summary_zh: 2-3 sentence Chinese explanation of key arguments or data points.\n"
        "   - quote_en: short direct English quote from the speaker in that segment.\n"
        "4. vocab_notes: Select 6 to 10 high-value English vocabulary terms, idioms, colloquial expressions, or industry jargon that appear in this episode. If there is an obvious ASR/transcription error in the transcript (e.g., 'stick' transcribed instead of 'shtick'), directly correct 'term' to the standard, proper English spelling. For each item:\n"
        "   - term: original English word, phrase, idiom, or industry jargon (standardized spelling).\n"
        "   - meaning_zh: concise and accurate Chinese contextual definition.\n"
        "   - note_zh: one-sentence note on idiomatic usage, industry/policy background, or nuance differences.\n"
        "   - start_sec: integer seconds where it is first or most typically uttered in the transcript (within [0, duration_sec]).\n"
        "5. references: 0 to 10 books, articles, research reports, podcasts/media, or data platforms/tools genuinely discussed, cited, or recommended by hosts or guests (quality over quantity; return empty list [] if none explicitly referenced). CRITICAL REDLINE: STRICTLY PROHIBITED from including podcast sponsors, commercial ads, or paid sponsor reads (sponsor / ad read / promo discounts). Only include works, media, reports, or tools organically discussed in the actual conversation or end-of-episode recommendations. For each item:\n"
        "   - title: title of the work, retaining accurate English original name (well-established Chinese translation may be added in parentheses; do not invent translations).\n"
        "   - creator: author, institution, director, or publisher (empty string \"\" if unknown).\n"
        "   - category: one of 'book', 'article', 'media', 'other'.\n"
        "   - note_zh: one-sentence explanation of why it was mentioned or recommended.\n"
        "   - start_sec: approximate integer seconds where it is mentioned in the transcript (within [0, duration_sec]), or null if only found in show notes and not located in audio.\n\n"
        "Terminology and Glossary Reference:\n"
        f"{glossary_text}"
    )
    if show_notes:
        system_instruction += (
            f"\n\nEpisode Show Notes (for guest names, topics, and context reference):\n"
            f"{show_notes}"
        )
    return system_instruction


def build_channel_a_user_prompt(
    title: str,
    show_title: str,
    duration_sec: int,
    transcript_text: str,
) -> str:
    """Build Channel A user prompt with timestamped transcript."""
    return (
        f"Podcast Episode Title: {title}\n"
        f"Podcast Show: {show_title}\n"
        f"Total Duration (seconds): {duration_sec}\n\n"
        f"Timestamped Transcript:\n"
        f"{transcript_text}\n\n"
        "Please provide the structured summary_zh, key_takeaways_zh, highlights, vocab_notes, and references according to the schema."
    )


def build_enrich_system_instruction(
    show_title: str,
    show_subtitle: str,
    glossary_text: str,
    show_notes: str = "",
) -> str:
    """Build Enrichment system instruction prompt."""
    system_instruction = (
        f"You are an expert podcast knowledge curator and terminologist for '{show_title}' ({show_subtitle}).\n"
        "Analyze the provided timestamped podcast transcript, episode show notes, and terminology glossary to extract high-value vocabulary notes and referenced works/media.\n\n"
        "Requirements:\n"
        "1. vocab_notes: Select 6 to 10 high-value English vocabulary terms, idioms, colloquial expressions, or industry jargon that appear in this episode. If there is an obvious ASR/transcription error in the transcript (e.g., 'stick' transcribed instead of 'shtick'), directly correct 'term' to the standard, proper English spelling. For each item:\n"
        "   - term: original English word, phrase, idiom, or industry jargon (standardized spelling).\n"
        "   - meaning_zh: concise and accurate Chinese contextual definition.\n"
        "   - note_zh: one-sentence note on idiomatic usage, industry/policy background, or nuance differences.\n"
        "   - start_sec: integer seconds where it is first or most typically uttered in the transcript (must fall within [0, duration_sec]). Timestamps in transcript are formatted as [MM:SS] or [HH:MM:SS]; convert to total seconds (e.g. [01:30] -> 90).\n"
        "2. references: 0 to 10 books, articles, research reports, podcasts/media, or data platforms/tools genuinely discussed, cited, or recommended by hosts or guests (quality over quantity; return empty list [] if none explicitly referenced). CRITICAL REDLINE: STRICTLY PROHIBITED from including podcast sponsors, commercial ads, or paid sponsor reads (sponsor / ad read / promo discounts). Only include works, media, reports, or tools organically discussed in the actual conversation or end-of-episode recommendations. For each item:\n"
        "   - title: title of the work, retaining accurate English original name (well-established Chinese translation may be added in parentheses; do not invent translations).\n"
        "   - creator: author, institution, director, or publisher (empty string \"\" if unknown).\n"
        "   - category: one of 'book', 'article', 'media', 'other'.\n"
        "   - note_zh: one-sentence explanation of why it was mentioned or recommended.\n"
        "   - start_sec: approximate integer seconds where it is mentioned in the transcript (within [0, duration_sec]), or null if only found in show notes and not located in audio.\n\n"
        "Terminology and Glossary Reference:\n"
        f"{glossary_text}"
    )
    if show_notes:
        system_instruction += (
            f"\n\nEpisode Show Notes (for guest names, topics, and context reference):\n"
            f"{show_notes}"
        )
    return system_instruction


def build_enrich_user_prompt(
    title: str,
    show_title: str,
    duration_sec: int,
    transcript_text: str,
) -> str:
    """Build Enrichment user prompt with timestamped transcript."""
    return (
        f"Podcast Episode Title: {title}\n"
        f"Podcast Show: {show_title}\n"
        f"Total Duration (seconds): {duration_sec}\n\n"
        f"Timestamped Transcript:\n"
        f"{transcript_text}\n\n"
        "Please provide the structured vocab_notes and references according to the schema."
    )
