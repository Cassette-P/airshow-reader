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

GENERIC_SPEAKER_PLACEHOLDERS = {
    "guest",
    "man",
    "woman",
    "man 1",
    "man 2",
    "woman 1",
    "woman 2",
    "speaker",
    "unknown",
    "narrator",
    "waiter",
    "archival recording",
    "ad narrator",
    "ad narrator 1",
    "ad narrator 2",
}


def build_channel_b_user_prompt(
    chunk_index: int = 0,
    total_chunks: int = 1,
    prev_tail_segments: list[dict] | None = None,
    observed_speakers: list[str] | None = None,
) -> str:
    """Build per-chunk Channel B user prompt with read-only previous-chunk continuity context."""
    if chunk_index <= 0:
        return CHANNEL_B_USER_PROMPT

    filtered_speakers: list[str] = []
    if observed_speakers:
        seen: set[str] = set()
        for spk in observed_speakers:
            s = str(spk or "").strip()
            if not s or s.lower() in GENERIC_SPEAKER_PLACEHOLDERS or s in seen:
                continue
            seen.add(s)
            filtered_speakers.append(s)

    valid_tail = [seg for seg in (prev_tail_segments or []) if isinstance(seg, dict) and seg.get("en")]
    if not filtered_speakers and not valid_tail:
        return CHANNEL_B_USER_PROMPT

    lines = [
        CHANNEL_B_USER_PROMPT,
        "",
        f"Cross-Chunk Continuity Context (Chunk {chunk_index + 1} of {max(total_chunks, chunk_index + 1)} — READ-ONLY reference, DO NOT re-transcribe):",
    ]
    if filtered_speakers:
        lines.append(
            "- Speakers already identified in earlier chunks (reuse exact name spelling if the same person speaks): "
            + ", ".join(filtered_speakers[:30])
        )
    if valid_tail:
        lines.append("- Previous audio chunk ended with these final segment(s):")
        for seg in valid_tail[-2:]:
            spk = str(seg.get("speaker") or "Guest").strip()
            en_tail = str(seg.get("en") or "").strip()
            zh_tail = str(seg.get("zh") or "").strip()
            if len(en_tail) > 280:
                en_tail = "..." + en_tail[-280:]
            if len(zh_tail) > 200:
                zh_tail = "……" + zh_tail[-200:]
            lines.append(f"  * [{spk}] EN: {en_tail}")
            if zh_tail:
                lines.append(f"    ZH: {zh_tail}")
    lines.append(
        "- Continuity rules: (1) Do NOT repeat or re-transcribe words already covered in the previous chunk; "
        "(2) Keep 'timestamp' strictly relative to the beginning of THIS audio chunk (starting near '00:00'); "
        "(3) If the previous chunk ended mid-sentence or mid-word, use that context to accurately recognize the opening word(s) of this chunk, keep the exact same speaker name if the same voice continues, and make the Chinese translation connect naturally without broken fragments; "
        "(4) Keep proper-noun spellings and Chinese terminology consistent with earlier chunks."
    )
    return "\n".join(lines)


def load_glossary_for_show(show: dict) -> str:
    """Load glossary file specified by show config."""
    glossary_rel = show.get("glossary_path") or show.get("glossary_file", "")
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
    content_style: str = "analytical",
    speaker_roster: list[str] | None = None,
) -> str:
    """Build Channel B system instruction prompt with content_style routing."""
    if content_style == "narrative":
        system_instruction = (
            f"You are an expert bilingual podcast transcriptionist and narrative journalism translator for '{show_title}' ({show_subtitle}).\n"
            "Your task is to transcribe and translate the provided podcast audio chunk into Chinese and English bilingual segments.\n\n"
            "Rules:\n"
            "1. Segment into crisp, natural semantic blocks of 1-3 sentences each (strictly <= 55 English words or <= 18 seconds per block; NEVER merge long monologues or interviewer-interviewee Q&A exchanges into a single block). Always start a new segment immediately whenever the speaker changes.\n"
            "2. For each block, provide:\n"
            "   - timestamp: string timestamp relative to the beginning of this audio chunk, strictly formatted as 'MM:SS' (e.g., '00:02', '01:06', '04:55'). Never omit the colon. Keep timestamps accurately anchored to actual spoken positions across the entire chunk without drifting.\n"
            "   - speaker: speaker full English name. Priority reference to the provided Speaker Roster, Show Notes, Glossary, and Cross-Chunk Continuity Context to accurately spell full names of interviewees, reporters, and staff credits (resolve first-name-only speakers to full names when their surname appears in Show Notes or context, e.g., 'The Bengsons, Abigail and Shaun' -> 'Shaun Bengson' / 'Abigail Bengson'; use 'Guest' only if completely unidentifiable). Never merge two different speakers into one segment. For multi-lingual voiceover interpretations, label as '<Name> (via translator)'. For archival audio recordings, briefly indicate provenance if mentioned.\n"
            "   - en: the original English spoken text (1-3 sentences, <= 55 words per block; omit bracketed stage directions such as [LAUGHTER]). Skip pure transitional background music, interlude score, commercial sponsor credits / promo reads, and house membership trial promos with promo codes (e.g., This American Life Partners trial reads), while preserving end-of-episode staff credits and post-credits stings.\n"
            "   - zh: natural, lively, and idiomatic Chinese spoken translation capturing narrative tension, humor, wordplay/puns, emotions, and colloquial rhythm, avoiding rigid literal calques or translationese. Keep person names and fixed act labels ('Prologue', 'Act One', 'Act Two', etc.) in original English per glossary rules, but translate standard US state, country, and major city names into standard Chinese (e.g., 蒙大拿州, 内布拉斯加州, 密歇根州, 底特律).\n"
            "3. Adhere strictly to the following speaker and terminology glossary:\n\n"
            f"{glossary_text}"
        )
        if speaker_roster:
            system_instruction += (
                f"\n\nOfficial Speaker Roster (from verified transcript, for spelling and voice calibration):\n"
                f"{', '.join(speaker_roster)}"
            )
        if show_notes:
            system_instruction += (
                f"\n\nEpisode Show Notes & Act Structure (for guest names, topics, and story context):\n"
                f"{show_notes}"
            )
        return system_instruction

    # Default "analytical" style (airshow, sinica)
    system_instruction = (
        f"You are an expert bilingual podcast transcriptionist and translator for '{show_title}' ({show_subtitle}).\n"
        "Your task is to transcribe and translate the provided podcast audio chunk into Chinese and English bilingual segments.\n\n"
        "Rules:\n"
        "1. Segment into crisp, natural semantic blocks of 1-3 sentences each (strictly <= 55 English words or <= 18 seconds per block; NEVER merge interviewer-interviewee Q&A exchanges into a single block). Always start a new segment immediately whenever the speaker changes.\n"
        "2. Skip standalone commercial sponsor reads, conversational host-read mid-roll ads with sponsor promo URLs (e.g., upgrade.com/flex-pay, plusgrade.com, flytulsa.com), and standalone house Substack/membership subscription pitches (e.g., sinicapodcast.com), while preserving all editorial discussion once the hosts return. Never hallucinate speech over intro/outro music, and omit non-speech stage tags like '[laughter]' or '[music]'.\n"
        "3. For each block, provide:\n"
        "   - timestamp: string timestamp relative to the beginning of this audio chunk, strictly formatted as 'MM:SS' (e.g., '00:02', '01:06', '04:55'). Never omit the colon. Keep timestamps accurately anchored to actual spoken positions across the entire chunk without drifting.\n"
        "   - speaker: speaker full English name normalized according to the glossary, episode show notes, and cross-chunk continuity context (use 'Guest' only if speaker name cannot be identified). Never merge two different speakers into one segment, and NEVER assign 'speaker' to the person being directly addressed by name in the second person within that segment.\n"
        "   - en: the original English spoken text (1-3 sentences, <= 55 words per block).\n"
        "   - zh: natural, fluent, professional Chinese translation adhering strictly to the glossary and domain conventions.\n"
        "4. Adhere strictly to the following speaker and terminology glossary:\n\n"
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
    content_style: str = "analytical",
) -> str:
    """Build Channel A executive summarization system instruction prompt with content_style routing."""
    if content_style == "narrative":
        system_instruction = (
            f"You are a narrative podcast story editor and literary curator for '{show_title}' ({show_subtitle}).\n"
            "Analyze the provided timestamped podcast transcript and produce a structured Chinese narrative summary, "
            "along with an Act Guide, chapter highlights, selected vocabulary notes, and referenced works.\n\n"
            "Requirements:\n"
            "1. summary_zh: A comprehensive narrative core summary in Chinese (200-350 Chinese characters) capturing the central theme (母主题/Theme) of this episode, how the Prologue introduces it, and how the various acts/stories illustrate or echo this theme.\n"
            "2. key_takeaways_zh: Act Guide (分幕导读). Exactly 3 to 5 items (one for Prologue, Act One, Act Two, etc.), each clearly indicating the act label (e.g. 'Prologue', 'Act One'), reporter/host name, and a one-sentence story synopsis/premise (记者与一句话故事梗概).\n"
            "3. highlights: 4 to 10 narrative highlights and chapter points from the episode, sorted chronologically by start_sec. MANDATORY REDLINE: You MUST anchor the exact opening start_sec of Prologue and every Act (Act One, Act Two, etc.) based on actual transcript timestamps, so these act markers serve directly as frontend chapter navigation. Remaining highlight slots (up to 10 total) should be allocated to key narrative turning points, emotional peaks, and memorable quotes. Each highlight must include:\n"
            "   - start_sec: integer seconds where this act or narrative turning point begins (must correspond to actual transcript timestamps and be within [0, duration_sec]).\n"
            "   - title_zh: concise Chinese topic/act title (strictly 15 Chinese characters or fewer, e.g. 'Prologue: 引子故事', 'Act One: 第一幕标题').\n"
            "   - summary_zh: 2-3 sentence Chinese explanation of key narrative developments or turning points.\n"
            "   - quote_en: short direct English quote from the speaker in that segment.\n"
            "4. vocab_notes: Select 6 to 10 high-value English vocabulary terms, authentic American colloquialisms, idioms, puns/wordplay, narrative rhetoric, or American cultural/societal references that appear in this episode. If there is an obvious ASR/transcription error in the transcript, directly correct 'term' to the standard, proper English spelling. For each item:\n"
            "   - term: original English word, phrase, idiom, or rhetorical expression (standardized spelling).\n"
            "   - meaning_zh: concise and accurate Chinese contextual definition.\n"
            "   - note_zh: one-sentence note on colloquial nuance, cultural background, or narrative rhetoric.\n"
            "   - start_sec: integer seconds where it is first or most typically uttered in the transcript (within [0, duration_sec]).\n"
            "5. references: 0 to 10 books, investigative articles, research reports, documentaries/podcasts, or featured music albums genuinely discussed, cited, or recommended in the episode or show notes. Natural empty list [] is completely acceptable if none are explicitly referenced (NEVER hallucinate or fabricate titles). CRITICAL REDLINE: STRICTLY PROHIBITED from including podcast sponsors, commercial ads, or paid sponsor reads (sponsor / ad read / promo discounts). Only include works or media organically mentioned in the narrative or end-of-episode credits. For each item:\n"
            "   - title: title of the work, retaining accurate English original name (well-established Chinese translation may be added in parentheses; do not invent translations).\n"
            "   - creator: author, institution, director, or publisher (empty string \"\" if unknown).\n"
            "   - category: one of 'book', 'article', 'media', 'other'.\n"
            "   - note_zh: one-sentence explanation of why it was mentioned or recommended.\n"
            "   - start_sec: approximate integer seconds where it is mentioned in the transcript (within [0, duration_sec]), or null if only found in show notes and not located in audio.\n"
            "   - Do NOT fabricate or guess external URLs or platform IDs; verified direct links are resolved deterministically downstream.\n\n"
            "Terminology and Glossary Reference:\n"
            f"{glossary_text}"
        )
        if show_notes:
            system_instruction += (
                f"\n\nEpisode Show Notes & Act Structure (for guest names, topics, and story context):\n"
                f"{show_notes}"
            )
        return system_instruction

    # Default "analytical" style (airshow, sinica)
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
        "   - start_sec: approximate integer seconds where it is mentioned in the transcript (within [0, duration_sec]), or null if only found in show notes and not located in audio.\n"
        "   - Do NOT fabricate or guess external URLs or platform IDs; verified direct links are resolved deterministically downstream.\n\n"
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
    content_style: str = "analytical",
) -> str:
    """Build Enrichment system instruction prompt with content_style routing."""
    if content_style == "narrative":
        system_instruction = (
            f"You are an expert podcast narrative knowledge curator and terminologist for '{show_title}' ({show_subtitle}).\n"
            "Analyze the provided timestamped podcast transcript, episode show notes, and terminology glossary to extract high-value vocabulary notes and referenced works/media.\n\n"
            "Requirements:\n"
            "1. vocab_notes: Select 6 to 10 high-value English vocabulary terms, authentic American colloquialisms, idioms, puns/wordplay, narrative rhetoric, or American cultural/societal references that appear in this episode. If there is an obvious ASR/transcription error in the transcript, directly correct 'term' to the standard, proper English spelling. For each item:\n"
            "   - term: original English word, phrase, idiom, or rhetorical expression (standardized spelling).\n"
            "   - meaning_zh: concise and accurate Chinese contextual definition.\n"
            "   - note_zh: one-sentence note on colloquial nuance, cultural background, or narrative rhetoric.\n"
            "   - start_sec: integer seconds where it is first or most typically uttered in the transcript (must fall within [0, duration_sec]). Timestamps in transcript are formatted as [MM:SS] or [HH:MM:SS]; convert to total seconds (e.g. [01:30] -> 90).\n"
            "2. references: 0 to 10 books, investigative articles, research reports, documentaries/podcasts, or featured music albums genuinely discussed, cited, or recommended in the episode or show notes. Natural empty list [] is completely acceptable if none are explicitly referenced (NEVER hallucinate or fabricate titles). CRITICAL REDLINE: STRICTLY PROHIBITED from including podcast sponsors, commercial ads, or paid sponsor reads (sponsor / ad read / promo discounts). Only include works or media organically mentioned in the narrative or end-of-episode credits. For each item:\n"
            "   - title: title of the work, retaining accurate English original name (well-established Chinese translation may be added in parentheses; do not invent translations).\n"
            "   - creator: author, institution, director, or publisher (empty string \"\" if unknown).\n"
            "   - category: one of 'book', 'article', 'media', 'other'.\n"
            "   - note_zh: one-sentence explanation of why it was mentioned or recommended.\n"
            "   - start_sec: approximate integer seconds where it is mentioned in the transcript (within [0, duration_sec]), or null if only found in show notes and not located in audio.\n"
            "   - Do NOT fabricate or guess external URLs or platform IDs; verified direct links are resolved deterministically downstream.\n\n"
            "Terminology and Glossary Reference:\n"
            f"{glossary_text}"
        )
        if show_notes:
            system_instruction += (
                f"\n\nEpisode Show Notes & Act Structure (for guest names, topics, and story context):\n"
                f"{show_notes}"
            )
        return system_instruction

    # Default "analytical" style (airshow, sinica)
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
        "   - start_sec: approximate integer seconds where it is mentioned in the transcript (within [0, duration_sec]), or null if only found in show notes and not located in audio.\n"
        "   - Do NOT fabricate or guess external URLs or platform IDs; verified direct links are resolved deterministically downstream.\n\n"
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
