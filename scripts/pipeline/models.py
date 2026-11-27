"""Pydantic data schemas and field clamping/sorting logic."""

from typing import Any, Literal, Optional
from urllib.parse import urlparse
from pydantic import BaseModel, Field

VALID_LINK_SOURCES: tuple[str, ...] = (
    "article",
    "official",
    "apple_podcasts",
    "podcast_site",
    "wikipedia",
    "goodreads",
    "douban",
)

SOURCE_LABEL_MAP: dict[str, str] = {
    "wikipedia": "Wikipedia",
    "douban": "豆瓣",
    "goodreads": "Goodreads",
    "apple_podcasts": "Apple Podcasts",
    "podcast_site": "播客官网",
    "article": "原文",
    "official": "官网",
}

SOURCE_PRIORITY: dict[str, int] = {src: idx for idx, src in enumerate(VALID_LINK_SOURCES)}


class ReferenceLink(BaseModel):
    source: Literal[
        "wikipedia",
        "douban",
        "goodreads",
        "apple_podcasts",
        "podcast_site",
        "article",
        "official",
    ] = Field(description="Platform/source identifier for this verified direct link")
    label: str = Field(description="Canonical display label mapped strictly from source")
    url: str = Field(description="Verified direct canonical URL (http/https)")


class TranscriptSegment(BaseModel):
    timestamp: str = Field(
        description="Segment start timestamp strictly formatted as 'MM:SS' (e.g. '00:02', '01:06', '02:55') relative to the beginning of this audio chunk. Colon must NOT be omitted."
    )
    speaker: str = Field(
        description="Speaker full name normalized according to glossary and episode show notes, or guest full name ('Guest' if unknown)"
    )
    en: str = Field(
        description="Original English spoken text, transcribed in natural 2-4 sentence semantic blocks"
    )
    zh: str = Field(
        description="Fluent, idiomatic Chinese translation adhering strictly to the glossary terminology"
    )


class ChannelBOutput(BaseModel):
    segments: list[TranscriptSegment] = Field(
        description="List of transcribed and translated bilingual segments for this chunk"
    )


class HighlightItem(BaseModel):
    start_sec: int = Field(
        description="Start time of this highlight in seconds from the start of the episode (0 <= start_sec <= duration_sec)"
    )
    title_zh: str = Field(
        description="Concise Chinese title (within 15 Chinese characters)"
    )
    summary_zh: str = Field(
        description="2-3 sentence Chinese explanation of key argument or data"
    )
    quote_en: str = Field(
        description="Short original English quote from this segment"
    )


class VocabNote(BaseModel):
    term: str = Field(
        description="Original English word, phrase, idiom, or industry jargon"
    )
    meaning_zh: str = Field(
        description="Concise and accurate Chinese contextual definition"
    )
    note_zh: str = Field(
        description="One-sentence note on idiomatic usage, policy/industry background, or difference from literal meaning"
    )
    start_sec: int = Field(
        description="Integer seconds from episode start where this term first or most typically appears in the transcript (must fall within [0, duration_sec])"
    )


class ReferenceItem(BaseModel):
    title: str = Field(
        description="Title of book, article, research report, media/podcast, or tool (retain accurate English name; well-known Chinese title may be appended in parentheses; never invent translations)"
    )
    creator: str = Field(
        default="",
        description="Author, institution, director, or publisher (empty string '' if unknown)"
    )
    category: Literal["book", "article", "media", "other"] = Field(
        description="Category: 'book', 'article', 'media', or 'other'"
    )
    note_zh: str = Field(
        description="One-sentence explanation of why it was mentioned or recommended by hosts/guests"
    )
    start_sec: Optional[int] = Field(
        default=None,
        description="Approximate integer seconds where mentioned in transcript (within [0, duration_sec]), or null if from show notes only"
    )


class EnrichmentResponse(BaseModel):
    vocab_notes: list[VocabNote] = Field(
        default_factory=list,
        description="6-10 selected high-value English vocabulary terms, idioms, or industry jargon with Chinese meaning, contextual usage note, and timestamp"
    )
    references: list[ReferenceItem] = Field(
        default_factory=list,
        description="0-10 external works, books, articles, media, or tools mentioned in the episode or show notes. Empty list if none explicitly referenced."
    )


class ChannelAOutput(BaseModel):
    summary_zh: str = Field(
        description="200-350 character Chinese core summary covering main issues, perspectives, and conclusions"
    )
    key_takeaways_zh: list[str] = Field(
        description="3-5 key takeaways as one-sentence conclusions in Chinese"
    )
    highlights: list[HighlightItem] = Field(
        description="4-10 highlights chronologically ordered"
    )
    vocab_notes: list[VocabNote] = Field(
        default_factory=list,
        description="6-10 selected high-value English vocabulary terms, idioms, or industry jargon with Chinese meaning, contextual usage note, and timestamp"
    )
    references: list[ReferenceItem] = Field(
        default_factory=list,
        description="0-10 external works, books, articles, media, or tools mentioned in the episode or show notes. Empty list if none explicitly referenced."
    )


# --- Field clamping and sorting functions ---


def clamp_and_sort_highlights(raw_highlights: list[Any], duration_sec: int) -> list[dict]:
    """Clamp highlight start_sec into [0, duration_sec], sort ascending, truncate to max 10."""
    highlights = []
    for h in raw_highlights:
        start_sec = h.start_sec if hasattr(h, "start_sec") else h["start_sec"]
        title_zh = h.title_zh if hasattr(h, "title_zh") else h["title_zh"]
        summary_zh = h.summary_zh if hasattr(h, "summary_zh") else h["summary_zh"]
        quote_en = h.quote_en if hasattr(h, "quote_en") else h["quote_en"]
        clamped_sec = max(0, min(int(start_sec), duration_sec))
        highlights.append({
            "start_sec": clamped_sec,
            "title_zh": title_zh.strip(),
            "summary_zh": summary_zh.strip(),
            "quote_en": quote_en.strip(),
        })
    highlights.sort(key=lambda x: x["start_sec"])
    if len(highlights) > 10:
        highlights = highlights[:10]
    return highlights


def clamp_key_takeaways(raw_key_takeaways: list[str]) -> list[str]:
    """Strip key takeaways and limit to at most 5 items."""
    key_takeaways = [k.strip() for k in raw_key_takeaways]
    if len(key_takeaways) > 5:
        key_takeaways = key_takeaways[:5]
    return key_takeaways


def clamp_and_sort_vocab_notes(raw_vocab_notes: list[Any], duration_sec: int) -> list[dict]:
    """Clamp vocab_notes start_sec into [0, duration_sec], truncate to max 10, sort ascending."""
    vocab_notes = []
    for v in raw_vocab_notes:
        term = v.term if hasattr(v, "term") else v["term"]
        meaning_zh = v.meaning_zh if hasattr(v, "meaning_zh") else v["meaning_zh"]
        note_zh = v.note_zh if hasattr(v, "note_zh") else v["note_zh"]
        start_sec = v.start_sec if hasattr(v, "start_sec") else v["start_sec"]
        clamped_sec = max(0, min(int(start_sec), duration_sec))
        vocab_notes.append({
            "term": term.strip(),
            "meaning_zh": meaning_zh.strip(),
            "note_zh": note_zh.strip(),
            "start_sec": clamped_sec,
        })
    if len(vocab_notes) > 10:
        vocab_notes = vocab_notes[:10]
    vocab_notes.sort(key=lambda x: x["start_sec"])
    return vocab_notes


def normalize_reference_links(raw_links: Any, max_links: int = 4) -> list[dict]:
    """Normalize, deduplicate (by source and canonical url), sort by fixed priority, and cap ReferenceLink items."""
    if not isinstance(raw_links, list):
        return []

    seen_sources: set[str] = set()
    seen_urls: set[str] = set()
    normalized: list[dict] = []

    for item in raw_links:
        if hasattr(item, "source"):
            source = str(item.source or "").strip().lower()
            url = str(item.url or "").strip()
        elif isinstance(item, dict):
            source = str(item.get("source") or "").strip().lower()
            url = str(item.get("url") or "").strip()
        else:
            continue

        if source not in SOURCE_LABEL_MAP:
            continue
        if not url or len(url) > 1024 or any(ch.isspace() for ch in url):
            continue
        if not (url.startswith("https://") or url.startswith("http://")):
            continue

        parsed = urlparse(url)
        if not parsed.netloc:
            continue
        dedup_url = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"
        if parsed.query:
            dedup_url += f"?{parsed.query}"

        if source in seen_sources or dedup_url in seen_urls:
            continue

        seen_sources.add(source)
        seen_urls.add(dedup_url)
        normalized.append({
            "source": source,
            "label": SOURCE_LABEL_MAP[source],
            "url": url,
        })

    normalized.sort(key=lambda x: SOURCE_PRIORITY.get(x["source"], 999))
    if len(normalized) > max_links:
        normalized = normalized[:max_links]
    return normalized


def clamp_and_sort_references(raw_references: list[Any], duration_sec: int) -> list[dict]:
    """Clamp references start_sec into [0, duration_sec], validate category, normalize links, truncate to max 10, sort with None at end."""
    references = []
    for r in raw_references:
        title = r.title if hasattr(r, "title") else r["title"]
        creator = r.creator if hasattr(r, "creator") else r.get("creator", "")
        category = r.category if hasattr(r, "category") else r.get("category", "other")
        note_zh = r.note_zh if hasattr(r, "note_zh") else r["note_zh"]
        start_sec = r.start_sec if hasattr(r, "start_sec") else r.get("start_sec")
        raw_links = r.links if hasattr(r, "links") else (r.get("links", []) if isinstance(r, dict) else [])

        st_sec = None
        if start_sec is not None:
            try:
                st_sec = max(0, min(int(start_sec), duration_sec))
            except (ValueError, TypeError):
                st_sec = None
        cat = category if category in ("book", "article", "media", "other") else "other"
        references.append({
            "title": title.strip(),
            "creator": creator.strip() if creator else "",
            "category": cat,
            "note_zh": note_zh.strip(),
            "start_sec": st_sec,
            "links": normalize_reference_links(raw_links),
        })
    if len(references) > 10:
        references = references[:10]
    references.sort(key=lambda x: (x["start_sec"] is None, x["start_sec"] if x["start_sec"] is not None else 0))
    return references
