"""This American Life (TAL) custom pipeline hooks.

Handles:
1. Episode number extraction and dual deduplication (guid + episode_number rerun detection).
2. Official episode page scraping (ad-free archive MP3, clean acts & reporters structure).
3. Official transcript page scraping (Speaker Roster calibration for Channel B).

Triggered strictly when show.get("scraper_hook") == "tal".
"""

from datetime import datetime, timedelta, timezone
import json
import re
from typing import Optional

from bs4 import BeautifulSoup
import requests

from pipeline.config import (
    DEFAULT_HEADERS,
    DOCS_DATA_DIR,
    log,
)
from pipeline.rss import (
    get_entry_audio_url,
    get_entry_guid,
    get_entry_pub_date,
    get_entry_show_notes,
)
from pipeline.storage import normalize_speaker

# Regex for extracting episode number from title, episode page link, or archive MP3 URL
TITLE_EP_REGEX = re.compile(r"^\s*(\d+)\s*:\s*(.+)$")
LINK_EP_REGEX = re.compile(r"thisamericanlife\.org/(?:sites/default/files/audio/)?(\d+)(?:/|$)")


def extract_tal_episode_number(title: str = "", link: str = "") -> Optional[int]:
    """Extract integer episode number from title or link."""
    if title:
        m = TITLE_EP_REGEX.search(title)
        if m:
            try:
                return int(m.group(1))
            except (ValueError, TypeError):
                pass
    if link:
        m = LINK_EP_REGEX.search(link)
        if m:
            try:
                return int(m.group(1))
            except (ValueError, TypeError):
                pass
    return None


def get_existing_tal_episode_numbers(show_id: str = "tal") -> set[int]:
    """Extract set of integer episode numbers from docs/data/<show_id>/index.json."""
    index_file = DOCS_DATA_DIR / show_id / "index.json"
    existing_nums = set()
    if not index_file.exists():
        return existing_nums

    try:
        with open(index_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            for ep in data.get("episodes", []):
                ep_num = extract_tal_episode_number(
                    title=ep.get("title", ""),
                    link=ep.get("audio_url", ""),
                )
                if ep_num is not None:
                    existing_nums.add(ep_num)
    except Exception as e:
        log("WARN", f"Failed to read existing episode numbers from {index_file}: {e}", show_id=show_id)

    return existing_nums


def filter_tal_episodes(
    feed_entries: list,
    show: dict,
    limit: int = 1,
    max_age_days: Optional[int] = None,
) -> list:
    """Filter TAL episodes with dual deduplication (guid + episode_number rerun detection).

    Strictly preserves pub_date descending sort order without assuming monotonic episode numbers.
    """
    show_id = show["id"]
    index_file = DOCS_DATA_DIR / show_id / "index.json"
    log("FILTER", f"[TAL-HOOK] Checking existing episodes in {index_file}...", show_id=show_id)

    existing_guids: set[str] = set()
    if index_file.exists():
        try:
            with open(index_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                existing_guids = {ep["guid"] for ep in data.get("episodes", []) if "guid" in ep}
        except Exception as e:
            log("ERROR", f"Failed to read existing {index_file}: {e}", show_id=show_id)
            raise

    existing_ep_nums = get_existing_tal_episode_numbers(show_id)
    log(
        "FILTER",
        f"[TAL-HOOK] Database has {len(existing_guids)} existing GUID(s) and {len(existing_ep_nums)} existing episode number(s).",
        show_id=show_id,
    )

    # Filter out existing GUIDs and rerun episode numbers while strictly preserving feed pub_date order
    candidates = []
    seen_ep_nums = set(existing_ep_nums)
    for entry in feed_entries:
        guid = get_entry_guid(entry)
        title = entry.get("title", "")
        link = entry.get("link", "")

        # 1. Guid check
        if guid in existing_guids:
            continue

        # 2. Episode number rerun / in-feed duplicate check
        ep_num = extract_tal_episode_number(title=title, link=link)
        if ep_num is not None and ep_num in seen_ep_nums:
            log(
                "FILTER",
                f"[TAL-HOOK] Skipping rerun episode #{ep_num}: '{title}' (GUID: {guid}, already in index or earlier in feed).",
                show_id=show_id,
            )
            continue
        if ep_num is not None:
            seen_ep_nums.add(ep_num)

        candidates.append(entry)

    log("FILTER", f"[TAL-HOOK] Found {len(candidates)} unhandled new episode(s).", show_id=show_id)

    if not candidates:
        return []

    # 3. Apply max-age-days cutoff
    effective_max_age = max_age_days if max_age_days is not None else show.get("default_max_age_days", 14)
    if effective_max_age > 0:
        now_utc = datetime.now(timezone.utc)
        cutoff_dt = now_utc - timedelta(days=effective_max_age)
        log(
            "FILTER",
            f"[TAL-HOOK] Applying max-age-days filter: {effective_max_age} day(s) (cutoff UTC: {cutoff_dt.strftime('%Y-%m-%dT%H:%M:%SZ')})...",
            show_id=show_id,
        )
        filtered_entries = []
        for e in candidates:
            pub_date_str = get_entry_pub_date(e)
            try:
                pub_dt = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
            except Exception:
                pub_dt = now_utc
            if pub_dt >= cutoff_dt:
                filtered_entries.append(e)
            else:
                log(
                    "FILTER",
                    f"[TAL-HOOK] Filtered out episode '{e.get('title', 'Unknown')}' ({get_entry_guid(e)}, published {pub_date_str}) older than {effective_max_age} day(s).",
                    show_id=show_id,
                )
        candidates = filtered_entries
        log("FILTER", f"[TAL-HOOK] {len(candidates)} unhandled episode(s) remaining within {effective_max_age}-day limit.", show_id=show_id)

    selected = candidates[:limit]
    log("FILTER", f"[TAL-HOOK] Selected {len(selected)} episode(s) to process (limit: {limit}).", show_id=show_id)
    return selected


def fetch_tal_episode_page(slug_url: str, show_id: str = "tal") -> tuple[str, str, list[dict]]:
    """Scrape official TAL episode page with full slug URL.

    Returns:
        (archive_audio_url, assembled_show_notes, list_of_acts)
    """
    if not slug_url or not slug_url.startswith("http"):
        log("WARN", f"[TAL-HOOK] Invalid or missing slug URL '{slug_url}'. Skipping page scrape.", show_id=show_id)
        return "", "", []

    log("TAL-HOOK", f"Scraping official episode page from {slug_url}...", show_id=show_id)
    try:
        resp = requests.get(slug_url, headers=DEFAULT_HEADERS, timeout=20)
        if resp.status_code != 200:
            log("WARN", f"[TAL-HOOK] Episode page {slug_url} returned HTTP {resp.status_code}.", show_id=show_id)
            return "", "", []
    except Exception as e:
        log("WARN", f"[TAL-HOOK] Failed to request episode page {slug_url}: {e}.", show_id=show_id)
        return "", "", []

    soup = BeautifulSoup(resp.content, "html.parser")

    # 1. Parse ad-free archive audio URL from <script id="playlist-data">
    archive_url = ""
    playlist_script = soup.find("script", id="playlist-data")
    playlist_raw = playlist_script.get_text(strip=True) if playlist_script else ""
    if playlist_raw:
        try:
            pl_data = json.loads(playlist_raw)
            raw_arch = (pl_data.get("archive") or "").strip() if isinstance(pl_data, dict) else ""
            if raw_arch:
                if raw_arch.startswith("//"):
                    archive_url = "https:" + raw_arch
                elif raw_arch.startswith("http://") or raw_arch.startswith("https://"):
                    archive_url = raw_arch
                log("TAL-HOOK", f"Extracted ad-free archive audio URL: {archive_url}", show_id=show_id)
            else:
                log("WARN", f"[TAL-HOOK] Field 'archive' missing or empty in playlist-data at {slug_url}.", show_id=show_id)
        except Exception as e:
            log("WARN", f"[TAL-HOOK] Failed to parse JSON in playlist-data script at {slug_url}: {e}.", show_id=show_id)
    else:
        log("WARN", f"[TAL-HOOK] <script id='playlist-data'> not found at {slug_url}.", show_id=show_id)

    # 2. Parse acts and contributors structure from article.node-act.view-episode
    act_elements = soup.select("article.node-act.view-episode")
    parsed_acts = []
    notes_blocks = []

    for i, act_el in enumerate(act_elements):
        label_el = act_el.select_one(".field-name-field-act-label")
        header_el = act_el.select_one("h2.act-header")
        body_el = act_el.select_one(".field-name-body")

        label = label_el.get_text(strip=True) if label_el else f"Act {i}"
        header = header_el.get_text(strip=True) if header_el else ""
        body = body_el.get_text(strip=True) if body_el else ""

        # Contributor extraction
        reporters = [
            a.get_text(strip=True)
            for a in act_el.select(".field-name-field-authors a, .byline a, .field-name-field-contributors a")
            if a.get_text(strip=True)
        ]
        if not reporters:
            reporters = [
                a.get_text(strip=True)
                for a in act_el.find_all("a")
                if "/archive?contributor=" in a.get("href", "") and a.get_text(strip=True)
            ]

        # Deduplicate reporters while preserving order
        seen_rep = set()
        dedup_reporters = []
        for r in reporters:
            norm_r = normalize_speaker(r)
            if norm_r and norm_r not in seen_rep:
                seen_rep.add(norm_r)
                dedup_reporters.append(norm_r)

        parsed_acts.append({
            "label": label,
            "title": header,
            "reporters": dedup_reporters,
            "body": body,
        })

        # Assemble clean act block for show notes
        act_line = f"### {label}: {header}" if header else f"### {label}"
        block_lines = [act_line]
        if dedup_reporters:
            block_lines.append(f"By: {', '.join(dedup_reporters)}")
        if body:
            block_lines.append(body)
        notes_blocks.append("\n".join(block_lines))

    assembled_show_notes = "\n\n".join(notes_blocks).strip()
    log(
        "TAL-HOOK",
        f"Parsed {len(parsed_acts)} acts from episode page (assembled clean show notes: {len(assembled_show_notes)} chars).",
        show_id=show_id,
    )
    return archive_url, assembled_show_notes, parsed_acts


def fetch_tal_transcript_page(episode_number: int, show_id: str = "tal") -> tuple[list[str], list[str]]:
    """Fetch official transcript page at https://www.thisamericanlife.org/<episode_number>/transcript.

    Returns:
        (speaker_roster, act_headings)
    """
    transcript_url = f"https://www.thisamericanlife.org/{episode_number}/transcript"
    log("TAL-HOOK", f"Fetching official transcript page from {transcript_url}...", show_id=show_id)

    try:
        resp = requests.get(transcript_url, headers=DEFAULT_HEADERS, timeout=20)
        if resp.status_code != 200:
            log("WARN", f"[TAL-HOOK] Transcript page returned HTTP {resp.status_code}. Falling back gracefully.", show_id=show_id)
            return [], []
    except Exception as e:
        log("WARN", f"[TAL-HOOK] Failed to request transcript page {transcript_url}: {e}. Falling back gracefully.", show_id=show_id)
        return [], []

    try:
        soup = BeautifulSoup(resp.content, "html.parser")
        act_headings = []
        speaker_turns = []

        transcript_root = (
            soup.select_one("article.node-transcript, .transcript-content, #transcript, main, article")
            or soup
        )
        for elem in transcript_root.find_all(["h3", "h4"]):
            txt = elem.get_text(strip=True)
            if not txt:
                continue
            if elem.name == "h3":
                if "subscribe" not in txt.lower():
                    act_headings.append(txt)
            elif elem.name == "h4":
                speaker_turns.append(txt)

        seen_spk = set()
        speaker_roster = []
        for s in speaker_turns:
            norm = normalize_speaker(s)
            if norm and norm.lower() != "guest" and norm not in seen_spk:
                seen_spk.add(norm)
                speaker_roster.append(norm)

        log(
            "TAL-HOOK",
            f"Extracted {len(speaker_roster)} speaker(s) and {len(act_headings)} act heading(s) from transcript page.",
            show_id=show_id,
        )
        return speaker_roster, act_headings

    except Exception as e:
        log("WARN", f"[TAL-HOOK] Failed to parse transcript page {transcript_url}: {e}. Falling back gracefully.", show_id=show_id)
        return [], []


def hook_tal_preprocess(entry, show: dict) -> dict:
    """Preprocess a TAL episode entry.

    Replaces audio_url with ad-free archive audio if available, builds pure
    acts show notes, and extracts Speaker Roster from the official transcript.
    """
    show_id = show.get("id", "tal")
    title = entry.get("title", "")
    link = entry.get("link", "")
    rss_audio = get_entry_audio_url(entry)
    rss_notes = get_entry_show_notes(entry, max_chars=3000)

    ep_num = extract_tal_episode_number(title=title, link=link)

    # 1. Fetch official episode page (using RSS entry.link with slug)
    archive_url, act_notes, parsed_acts = fetch_tal_episode_page(link, show_id=show_id)

    # Audio URL selection: archive MP3 preferred; fallback to RSS enclosure
    final_audio_url = archive_url if archive_url else rss_audio
    if not archive_url:
        log("WARN", f"[TAL-HOOK] Using RSS enclosure audio URL as fallback for episode '{title}'.", show_id=show_id)
    else:
        log("TAL-HOOK", f"[TAL-HOOK] Successfully replaced audio URL with ad-free archive: {final_audio_url}", show_id=show_id)

    # Show notes selection: clean acts structure preferred; fallback to RSS description
    final_show_notes = act_notes if act_notes else rss_notes

    # 2. Fetch official transcript speaker roster
    speaker_roster: list[str] = []
    act_headings: list[str] = []
    if ep_num is not None:
        speaker_roster, act_headings = fetch_tal_transcript_page(ep_num, show_id=show_id)
    else:
        log("WARN", f"[TAL-HOOK] Could not determine episode number for '{title}'. Skipping transcript scrape.", show_id=show_id)

    return {
        "audio_url": final_audio_url,
        "show_notes": final_show_notes,
        "speaker_roster": speaker_roster,
        "act_headings": act_headings,
        "parsed_acts": parsed_acts,
        "episode_number": ep_num,
    }
