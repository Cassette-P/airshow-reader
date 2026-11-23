#!/usr/bin/env python3
"""Podcast bilingual reading processor (v3.0).

Multi-podcast configuration-driven engine implementing the state machine:
[0. MIGRATE_IF_NEEDED] -> [1. FETCH_RSS] -> [2. FILTER_NEW] -> [3. DOWNLOAD_AUDIO] -> [4. CHUNK_AUDIO]
-> [5. CHANNEL_B] -> [6. CHANNEL_A] -> [7. ATOMIC_SAVE] -> [8. CLEANUP]
"""

import argparse
import calendar
from datetime import datetime, timezone, timedelta
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Callable, Literal, Optional

import feedparser
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
import requests

# Project root and core configuration paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PODCASTS_CONFIG_FILE = PROJECT_ROOT / "podcasts.json"
GLOSSARIES_DIR = PROJECT_ROOT / "glossaries"
LEGACY_GLOSSARY_FILE = PROJECT_ROOT / "GLOSSARY.md"

DOCS_DATA_DIR = PROJECT_ROOT / "docs" / "data"
SHOWS_MANIFEST_FILE = DOCS_DATA_DIR / "shows.json"

# Legacy v2 paths (for migration)
LEGACY_V2_INDEX_FILE = DOCS_DATA_DIR / "index.json"
LEGACY_V2_EPISODES_DIR = DOCS_DATA_DIR / "episodes"

# Legacy v1 paths (for migration)
LEGACY_DATA_DIR = PROJECT_ROOT / "data"
LEGACY_EPISODES_FILE = LEGACY_DATA_DIR / "episodes.json"
LEGACY_DOCS_EPISODES_FILE = DOCS_DATA_DIR / "episodes.json"

MODEL_USAGE_STATS: dict[str, dict[str, int]] = {
    "CH-B": {},
    "CH-A": {},
    "ENRICH": {},
}

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    )
}


# --- Safe GUID & Env Utility Functions ---


def make_safe_guid(guid: str) -> str:
    """Normalize GUID to a safe filename string: [^A-Za-z0-9._-]+ -> _."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", guid).strip("_")


def get_env_stripped(key: str, default: str = "") -> str:
    """Read environment variable with .strip() and empty-string fallback."""
    val = os.environ.get(key)
    if val is not None:
        val = val.strip()
        if val:
            return val
    return default


def format_sec_to_mmss(sec: int) -> str:
    """Format seconds into MM:SS (or HH:MM:SS if >= 3600)."""
    if sec < 0:
        sec = 0
    hrs = sec // 3600
    rem = sec % 3600
    mins = rem // 60
    secs = rem % 60
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


# --- Pydantic Structured Output Schemas ---


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


# --- Observability & Utilities ---


def normalize_speaker(name: str) -> str:
    """Normalize speaker name by stripping whitespace and title prefixes (Dr., Prof., etc.)."""
    if not name:
        return "Guest"
    s = str(name).strip()
    title_pattern = r"^(?:Dr\.?|Prof\.?|Professor|Ambassador|Mr\.?|Ms\.?|Mrs\.?|Sen\.?|Rep\.?)\s+"
    cleaned = re.sub(title_pattern, "", s, flags=re.IGNORECASE).strip()
    return cleaned if cleaned else s


def log(stage: str, message: str, show_id: str = "") -> None:
    """Print formatted log message conforming to [TAG][STAGE] standard."""
    tag = show_id.upper() if show_id else "AIRSHOW"
    print(f"[{tag}][{stage}] {message}", flush=True)


def ensure_tools_in_path() -> None:
    """Ensure ffmpeg and ffprobe are available in PATH.

    Checks standard PATH and common Windows WinGet / Gyan locations.
    """
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return

    candidates = [
        Path(os.path.expanduser(r"~\AppData\Local\Microsoft\WinGet\Links")),
        Path(
            os.path.expanduser(
                r"~\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.1-full_build\bin"
            )
        ),
    ]

    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            user_path, _ = winreg.QueryValueEx(key, "Path")
            for p in user_path.split(";"):
                p_str = p.strip()
                if p_str:
                    candidates.append(Path(p_str))
    except Exception:
        pass

    for p in candidates:
        if p.exists() and (p / "ffmpeg.exe").exists():
            os.environ["PATH"] = str(p) + os.pathsep + os.environ.get("PATH", "")
            break

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        log("ERROR", "ffmpeg and ffprobe must be installed and accessible in PATH.")
        sys.exit(1)


def load_podcasts_config() -> dict:
    """Load podcasts.json registry from project root."""
    if not PODCASTS_CONFIG_FILE.exists():
        log("ERROR", f"podcasts.json not found at {PODCASTS_CONFIG_FILE}")
        sys.exit(1)
    for enc in ["utf-8", "utf-8-sig", "gbk", "gb18030", "cp936"]:
        try:
            with open(PODCASTS_CONFIG_FILE, "r", encoding=enc) as f:
                return json.load(f)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    log("ERROR", "Failed to decode podcasts.json with supported encodings (utf-8, gbk, etc.).")
    sys.exit(1)


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


def get_entry_show_notes(entry, max_chars: int = 3000) -> str:
    """Extract and sanitize show notes from RSS entry (content/summary/description)."""
    raw_text = ""
    if "content" in entry and entry["content"]:
        for c in entry["content"]:
            if isinstance(c, dict) and "value" in c:
                raw_text = c["value"]
                break
            elif isinstance(c, str):
                raw_text = c
                break
    if not raw_text:
        raw_text = entry.get("summary") or entry.get("description") or ""
    if not raw_text:
        return ""

    text = html.unescape(raw_text)
    text = re.sub(r"(?i)<(br|p|/p|li)[^>]*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()

    if len(text) > max_chars:
        cut = text[:max_chars]
        last_space = cut.rfind(" ")
        if last_space > int(max_chars * 0.8):
            text = cut[:last_space] + "..."
        else:
            text = cut + "..."
    return text


def sync_shows_manifest() -> dict:
    """Atomically generate/update docs/data/shows.json based on podcasts.json and per-show index.json."""
    config = load_podcasts_config()
    all_shows = config.get("shows", [])

    shows_manifest_list = []
    for show in all_shows:
        if not show.get("enabled", True):
            continue
        show_id = show["id"]
        show_index_path = DOCS_DATA_DIR / show_id / "index.json"
        episode_count = 0
        latest_pub_date = ""

        if show_index_path.exists():
            try:
                with open(show_index_path, "r", encoding="utf-8") as f:
                    show_data = json.load(f)
                    episodes = show_data.get("episodes", [])
                    episode_count = len(episodes)
                    if episode_count > 0:
                        latest_pub_date = episodes[0].get("pub_date", "")
            except Exception as e:
                log("ERROR", f"Failed to read show index at {show_index_path}: {e}")

        shows_manifest_list.append({
            "id": show_id,
            "title": show.get("title", ""),
            "subtitle": show.get("subtitle", ""),
            "accent_color": show.get("accent_color", "#0a2540"),
            "index_file": f"{show_id}/index.json",
            "episode_count": episode_count,
            "latest_pub_date": latest_pub_date,
        })

    manifest = {
        "schema_version": "3.0",
        "site_title": config.get("site_title", "Podcast Bilingual Reader"),
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "shows": shows_manifest_list,
    }

    DOCS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_manifest = SHOWS_MANIFEST_FILE.with_suffix(".json.tmp")
    with open(tmp_manifest, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    os.replace(tmp_manifest, SHOWS_MANIFEST_FILE)
    log("SAVE", f"Synced shows manifest to {SHOWS_MANIFEST_FILE} ({len(shows_manifest_list)} shows).")
    return manifest


def migrate_to_v3_if_needed() -> bool:
    """[0. MIGRATE_IF_NEEDED] Migrate legacy files to v3.0 multi-podcast architecture idempotently."""
    migrated_anything = False

    # 1. Migrate root GLOSSARY.md -> glossaries/airshow.md
    airshow_glossary = GLOSSARIES_DIR / "airshow.md"
    if LEGACY_GLOSSARY_FILE.exists() and not airshow_glossary.exists():
        GLOSSARIES_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(str(LEGACY_GLOSSARY_FILE), str(airshow_glossary))
        log("MIGRATE", f"Moved {LEGACY_GLOSSARY_FILE} to {airshow_glossary}.")
        migrated_anything = True

    airshow_dir = DOCS_DATA_DIR / "airshow"
    airshow_index_file = airshow_dir / "index.json"
    airshow_episodes_dir = airshow_dir / "episodes"

    # Legacy v1 direct upgrade if exists
    if not airshow_index_file.exists() and not LEGACY_V2_INDEX_FILE.exists():
        legacy_v1_file = None
        if LEGACY_DOCS_EPISODES_FILE.exists():
            legacy_v1_file = LEGACY_DOCS_EPISODES_FILE
        elif LEGACY_EPISODES_FILE.exists():
            legacy_v1_file = LEGACY_EPISODES_FILE

        if legacy_v1_file:
            log("MIGRATE", f"Found legacy v1 file {legacy_v1_file}. Upgrading directly to v3...")
            with open(legacy_v1_file, "r", encoding="utf-8") as f:
                v1_data = json.load(f)
            airshow_episodes_dir.mkdir(parents=True, exist_ok=True)
            v1_eps = v1_data.get("episodes", [])
            index_eps = []
            for ep in v1_eps:
                guid = ep.get("guid", "")
                if not guid:
                    continue
                safe_guid = make_safe_guid(guid)
                ep_file = airshow_episodes_dir / f"{safe_guid}.json"
                ep_doc = {
                    "schema_version": "3.0",
                    "show_id": "airshow",
                    "guid": guid,
                    "title": ep.get("title", ""),
                    "pub_date": ep.get("pub_date", ""),
                    "duration_sec": int(ep.get("duration_sec", 0)),
                    "audio_url": ep.get("audio_url", ""),
                    "summary_zh": ep.get("summary_zh", ""),
                    "key_takeaways_zh": ep.get("key_takeaways_zh", []),
                    "highlights": ep.get("highlights", []),
                    "transcript": ep.get("transcript", []),
                }
                tmp_ep = ep_file.with_suffix(".json.tmp")
                with open(tmp_ep, "w", encoding="utf-8") as f:
                    json.dump(ep_doc, f, ensure_ascii=False, indent=2)
                os.replace(tmp_ep, ep_file)

                index_eps.append({
                    "guid": guid,
                    "title": ep.get("title", ""),
                    "pub_date": ep.get("pub_date", ""),
                    "duration_sec": int(ep.get("duration_sec", 0)),
                    "audio_url": ep.get("audio_url", ""),
                    "file": f"episodes/{safe_guid}.json",
                    "summary_zh": ep.get("summary_zh", ""),
                    "key_takeaways_zh": ep.get("key_takeaways_zh", []),
                    "highlights_count": len(ep.get("highlights", [])),
                    "transcript_count": len(ep.get("transcript", [])),
                })
            index_eps.sort(key=lambda x: x.get("pub_date", ""), reverse=True)
            airshow_index_doc = {
                "schema_version": "3.0",
                "show_id": "airshow",
                "podcast_title": v1_data.get("podcast_title", "The Air Show"),
                "rss_url": v1_data.get("rss_url", "https://feeds.buzzsprout.com/2329718.rss"),
                "updated_at": v1_data.get("updated_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
                "episodes": index_eps,
            }
            tmp_idx = airshow_index_file.with_suffix(".json.tmp")
            with open(tmp_idx, "w", encoding="utf-8") as f:
                json.dump(airshow_index_doc, f, ensure_ascii=False, indent=2)
            os.replace(tmp_idx, airshow_index_file)

            legacy_v1_file.unlink()
            if LEGACY_DATA_DIR.exists() and not any(LEGACY_DATA_DIR.iterdir()):
                try:
                    LEGACY_DATA_DIR.rmdir()
                except Exception:
                    pass
            migrated_anything = True

    # 2. Migrate v2 flat structure (docs/data/index.json + docs/data/episodes/*.json) -> docs/data/airshow/
    if LEGACY_V2_INDEX_FILE.exists() and not airshow_index_file.exists():
        log("MIGRATE", f"Starting v2->v3 migration: moving {LEGACY_V2_INDEX_FILE} and episodes to {airshow_dir}...")
        airshow_episodes_dir.mkdir(parents=True, exist_ok=True)

        ep_files = list(LEGACY_V2_EPISODES_DIR.glob("*.json")) if LEGACY_V2_EPISODES_DIR.exists() else []
        log("MIGRATE", f"Found {len(ep_files)} episode file(s) in {LEGACY_V2_EPISODES_DIR} to migrate.")

        written_ep_files = []
        for ep_path in ep_files:
            try:
                with open(ep_path, "r", encoding="utf-8") as f:
                    ep_data = json.load(f)
            except Exception as e:
                log("ERROR", f"Failed to read episode file {ep_path}: {e}")
                sys.exit(1)

            ep_data["schema_version"] = "3.0"
            ep_data["show_id"] = "airshow"

            target_ep_path = airshow_episodes_dir / ep_path.name
            tmp_ep_path = target_ep_path.with_suffix(".json.tmp")
            with open(tmp_ep_path, "w", encoding="utf-8") as f:
                json.dump(ep_data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_ep_path, target_ep_path)
            written_ep_files.append((ep_path, target_ep_path))

        # Verify all migrated episodes
        for _, new_p in written_ep_files:
            if not new_p.exists() or new_p.stat().st_size == 0:
                log("ERROR", f"Migration verification failed: {new_p} is missing or empty.")
                sys.exit(1)

        # Read old v2 index.json and migrate to airshow/index.json
        try:
            with open(LEGACY_V2_INDEX_FILE, "r", encoding="utf-8") as f:
                v2_index_data = json.load(f)
        except Exception as e:
            log("ERROR", f"Failed to read v2 index file {LEGACY_V2_INDEX_FILE}: {e}")
            sys.exit(1)

        v2_index_data["schema_version"] = "3.0"
        v2_index_data["show_id"] = "airshow"
        for ep_meta in v2_index_data.get("episodes", []):
            guid = ep_meta.get("guid", "")
            safe_guid = make_safe_guid(guid)
            ep_meta["file"] = f"episodes/{safe_guid}.json"

        tmp_airshow_index = airshow_index_file.with_suffix(".json.tmp")
        with open(tmp_airshow_index, "w", encoding="utf-8") as f:
            json.dump(v2_index_data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_airshow_index, airshow_index_file)

        if not airshow_index_file.exists() or airshow_index_file.stat().st_size == 0:
            log("ERROR", f"Migration verification failed: {airshow_index_file} is missing or empty.")
            sys.exit(1)

        log("MIGRATE", f"Verified {len(written_ep_files)} episodes and {airshow_index_file} written successfully.")

        # Clean up old episode files
        for old_p, _ in written_ep_files:
            try:
                old_p.unlink()
            except Exception as e:
                log("ERROR", f"Failed to unlink old episode {old_p}: {e}")

        # Remove old episodes dir
        if LEGACY_V2_EPISODES_DIR.exists():
            try:
                if not any(LEGACY_V2_EPISODES_DIR.iterdir()):
                    LEGACY_V2_EPISODES_DIR.rmdir()
                    log("MIGRATE", f"Removed empty legacy directory: {LEGACY_V2_EPISODES_DIR}")
            except Exception as e:
                log("ERROR", f"Failed to remove {LEGACY_V2_EPISODES_DIR}: {e}")

        # Remove old index.json
        LEGACY_V2_INDEX_FILE.unlink()
        log("MIGRATE", f"Removed legacy v2 index file: {LEGACY_V2_INDEX_FILE}")
        migrated_anything = True

    # 3. Initialize uninitialized show directories for enabled shows
    config = load_podcasts_config()
    for show in config.get("shows", []):
        if not show.get("enabled", True):
            continue
        show_id = show["id"]
        s_dir = DOCS_DATA_DIR / show_id
        s_index = s_dir / "index.json"
        s_episodes = s_dir / "episodes"
        if not s_index.exists():
            s_episodes.mkdir(parents=True, exist_ok=True)
            empty_index = {
                "schema_version": "3.0",
                "show_id": show_id,
                "podcast_title": show.get("title", ""),
                "rss_url": show.get("rss_url", ""),
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "episodes": [],
            }
            tmp_s_index = s_index.with_suffix(".json.tmp")
            with open(tmp_s_index, "w", encoding="utf-8") as f:
                json.dump(empty_index, f, ensure_ascii=False, indent=2)
            os.replace(tmp_s_index, s_index)
            log("MIGRATE", f"Initialized empty index and episodes dir for '{show_id}' at {s_dir}.")
            migrated_anything = True

    # 4. Generate/sync docs/data/shows.json manifest
    sync_shows_manifest()

    return migrated_anything


def fetch_rss(url: str, show_id: str = ""):
    """[1. FETCH_RSS] 抓取 RSS feed."""
    log("RSS", f"Fetching RSS feed from {url}...", show_id=show_id)
    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log("ERROR", f"Failed to fetch RSS: {e}", show_id=show_id)
        sys.exit(1)

    feed = feedparser.parse(resp.content)
    if feed.bozo and not feed.entries:
        log("ERROR", f"Failed to parse RSS feed: {feed.get('bozo_exception', 'Unknown error')}", show_id=show_id)
        sys.exit(1)

    log("RSS", f"Successfully fetched feed with {len(feed.entries)} episode(s).", show_id=show_id)
    return feed


def get_entry_guid(entry) -> str:
    """Extract unique GUID from an RSS entry."""
    return str(entry.get("id") or entry.get("guid") or "").strip()


def get_entry_pub_date(entry) -> str:
    """Extract ISO-8601 UTC publication timestamp (YYYY-MM-DDTHH:MM:SSZ)."""
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        ts = calendar.timegm(entry.published_parsed)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if "published" in entry:
        try:
            dt = datetime.fromisoformat(entry.published.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_entry_audio_url(entry) -> str:
    """Extract direct MP3 enclosure URL from an RSS entry."""
    for enc in entry.get("enclosures", []):
        href = enc.get("href")
        enc_type = enc.get("type", "")
        if href and ("audio" in enc_type or ".mp3" in href):
            return href
    for link in entry.get("links", []):
        href = link.get("href")
        link_type = link.get("type", "")
        if href and ("audio" in link_type or ".mp3" in href):
            return href
    if entry.get("enclosures"):
        return entry["enclosures"][0].get("href", "")
    return ""


def filter_new_episodes(
    feed_entries,
    show: dict,
    limit: int = 1,
    max_age_days: int | None = None,
) -> list:
    """[2. FILTER_NEW] 对比 docs/data/<show_id>/index.json 已有 guid，筛选最新未处理的 N 集."""
    show_id = show["id"]
    index_file = DOCS_DATA_DIR / show_id / "index.json"
    log("FILTER", f"Checking existing episodes in {index_file}...", show_id=show_id)

    existing_guids = set()
    if index_file.exists():
        try:
            with open(index_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                existing_guids = {ep["guid"] for ep in data.get("episodes", []) if "guid" in ep}
        except Exception as e:
            log("ERROR", f"Failed to read existing {index_file}: {e}", show_id=show_id)
            sys.exit(1)

    new_entries = [e for e in feed_entries if get_entry_guid(e) not in existing_guids]
    log("FILTER", f"Found {len(new_entries)} unhandled episode(s) (existing in DB: {len(existing_guids)}).", show_id=show_id)

    if not new_entries:
        return []

    effective_max_age = max_age_days if max_age_days is not None else show.get("default_max_age_days", 7)
    if effective_max_age > 0:
        now_utc = datetime.now(timezone.utc)
        cutoff_dt = now_utc - timedelta(days=effective_max_age)
        log("FILTER", f"Applying max-age-days filter: {effective_max_age} day(s) (cutoff UTC: {cutoff_dt.strftime('%Y-%m-%dT%H:%M:%SZ')})...", show_id=show_id)
        filtered_entries = []
        for e in new_entries:
            pub_date_str = get_entry_pub_date(e)
            try:
                pub_dt = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
            except Exception:
                pub_dt = now_utc
            if pub_dt >= cutoff_dt:
                filtered_entries.append(e)
            else:
                log("FILTER", f"Filtered out episode '{e.get('title', 'Unknown')}' ({get_entry_guid(e)}, published {pub_date_str}) older than {effective_max_age} day(s).", show_id=show_id)

        new_entries = filtered_entries
        log("FILTER", f"{len(new_entries)} unhandled episode(s) remaining within the {effective_max_age}-day limit.", show_id=show_id)

    selected = new_entries[:limit]
    log("FILTER", f"Selected {len(selected)} episode(s) to process (limit: {limit}).", show_id=show_id)
    return selected


def download_audio_file(audio_url: str, dest_path: Path, show_id: str = "") -> int:
    """[3. DOWNLOAD_AUDIO] 流式下载 MP3 至系统临时目录（四级策略自动降级）."""
    log("DOWNLOAD", f"Downloading audio from {audio_url}...", show_id=show_id)

    def _cleanup_partial() -> None:
        if dest_path.exists():
            try:
                dest_path.unlink()
            except Exception:
                pass

    def _run_strategy_1():
        headers = {
            "User-Agent": "AppleCoreMedia/1.0.0.21F79 (iPhone; U; CPU OS 17_5 like Mac OS X; en_us)",
            "Accept": "*/*",
        }
        with requests.get(audio_url, headers=headers, stream=True, timeout=180) as resp:
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)

    def _run_strategy_2():
        headers = {
            "User-Agent": "Overcast/3.0 (+http://overcast.fm/; iOS podcast app)",
            "Accept": "*/*",
        }
        with requests.get(audio_url, headers=headers, stream=True, timeout=180) as resp:
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)

    def _run_strategy_3():
        from curl_cffi import requests as cffi_requests

        resp = cffi_requests.get(
            audio_url,
            impersonate="chrome",
            stream=True,
            timeout=180,
            allow_redirects=True,
        )
        try:
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        finally:
            resp.close()

    def _run_strategy_4():
        subprocess.run(
            [
                "curl",
                "-fSL",
                "--max-time",
                "180",
                "-A",
                "AppleCoreMedia/1.0.0.21F79 (iPhone; U; CPU OS 17_5 like Mac OS X; en_us)",
                "-o",
                str(dest_path),
                audio_url,
            ],
            check=True,
        )

    strategies = [
        ("Strategy 1 (requests + Apple Podcasts UA)", _run_strategy_1),
        ("Strategy 2 (requests + Overcast UA)", _run_strategy_2),
        ("Strategy 3 (curl_cffi impersonate chrome)", _run_strategy_3),
        ("Strategy 4 (system curl fallback)", _run_strategy_4),
    ]

    last_exception = None
    for name, runner in strategies:
        _cleanup_partial()
        log("DOWNLOAD", f"Trying download strategy: {name}...", show_id=show_id)
        try:
            runner()
            if dest_path.exists() and dest_path.stat().st_size > 0:
                downloaded = dest_path.stat().st_size
                size_mb = downloaded / (1024 * 1024)
                log(
                    "DOWNLOAD",
                    f"Audio downloaded successfully via {name} ({size_mb:.2f} MB, {downloaded} bytes).",
                    show_id=show_id,
                )
                return downloaded
            else:
                raise RuntimeError(
                    f"{name} completed but output file is missing or empty."
                )
        except Exception as e:
            last_exception = e
            log("DOWNLOAD", f"{name} failed: {e}", show_id=show_id)
            _cleanup_partial()

    log("ERROR", f"Audio download failed across all strategies: {last_exception}", show_id=show_id)
    if last_exception:
        raise last_exception
    raise RuntimeError("Audio download failed across all strategies.")


def get_audio_duration_sec(audio_path: Path, show_id: str = "") -> int:
    """ffprobe 获取 duration_sec (整数秒)."""
    log("DOWNLOAD", "Probing audio duration with ffprobe...", show_id=show_id)
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        val = res.stdout.strip()
        if val and val != "N/A":
            dur = int(round(float(val)))
            log("DOWNLOAD", f"Audio duration via ffprobe: {dur}s ({dur // 60}m {dur % 60}s).", show_id=show_id)
            return dur
    except Exception as e:
        log("ERROR", f"ffprobe format duration check failed: {e}", show_id=show_id)

    # Fallback to stream duration
    cmd_stream = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    res_stream = subprocess.run(cmd_stream, capture_output=True, text=True, check=True)
    val_stream = res_stream.stdout.strip()
    if val_stream and val_stream != "N/A":
        dur = int(round(float(val_stream)))
        log("DOWNLOAD", f"Audio duration via stream ffprobe: {dur}s ({dur // 60}m {dur % 60}s).", show_id=show_id)
        return dur

    raise RuntimeError(f"Unable to probe duration for {audio_path}")


def parse_mmss_to_sec(ts: str, fallback_sec: int = 0) -> int:
    """Parse 'MM:SS' (or 'HH:MM:SS') timestamp string to integer seconds."""
    if not ts or not isinstance(ts, str):
        return fallback_sec
    parts = ts.strip().split(":")
    try:
        if len(parts) == 2:
            mins = int(float(parts[0]))
            secs = int(round(float(parts[1])))
            return mins * 60 + secs
        elif len(parts) == 3:
            hours = int(float(parts[0]))
            mins = int(float(parts[1]))
            secs = int(round(float(parts[2])))
            return hours * 3600 + mins * 60 + secs
        elif len(parts) == 1:
            return int(round(float(parts[0])))
    except Exception:
        pass
    return fallback_sec


def chunk_audio(audio_path: Path, output_dir: Path, chunk_sec: int, show_id: str = "") -> list[Path]:
    """[4. CHUNK_AUDIO] ffmpeg 转为单声道 64k mp3 并按 --chunk-sec（默认 180s）切片."""
    output_pattern = output_dir / "chunk_%03d.mp3"
    log("FFMPEG", f"Transcoding and slicing audio: mono, 64kbps, segment_time={chunk_sec}s...", show_id=show_id)
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(audio_path),
        "-vn",
        "-c:a", "libmp3lame",
        "-ac", "1",
        "-b:a", "64k",
        "-f", "segment",
        "-segment_time", str(chunk_sec),
        "-reset_timestamps", "1",
        str(output_pattern),
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        log("ERROR", f"ffmpeg audio slicing failed: {e.stderr}", show_id=show_id)
        raise

    chunks = sorted(output_dir.glob("chunk_*.mp3"))
    log("FFMPEG", f"Audio slicing completed. Generated {len(chunks)} chunk(s).", show_id=show_id)
    return chunks


def resolve_ch_a_thinking_level(model_name: str, env_override: str = "") -> str | None:
    """Resolve thinking level for Channel A based on env var override or model defaults."""
    override = env_override.strip() if env_override and env_override.strip() else get_env_stripped("GEMINI_THINKING_CH_A")
    if override:
        val = override.lower()
        if val in ("default", "none"):
            return None
        return val

    m = model_name.lower().strip()
    if "gemini-4-neon" in m:
        return "xhigh"
    elif "gemini-4-carbon" in m:
        return "high"
    else:
        return "high"


def get_next_fallback_thinking_level(current_level: str | None) -> str | None:
    """Return next fallback level in chain: xhigh -> high -> None."""
    if not current_level:
        return None
    lvl = current_level.strip().lower()
    if lvl == "xhigh":
        return "high"
    return None


def _format_level(lvl: str | None) -> str:
    """Format thinking level for logging."""
    return f"'{lvl}'" if lvl else "None"


def generate_structured_with_retry(
    client: genai.Client,
    model_pool: list[str],
    contents: list,
    system_instruction: str,
    response_schema: type[BaseModel],
    stage: str = "GENAI",
    temperature: float = 0.2,
    max_retries: int = 5,
    thinking_level: str | None = None,
    thinking_level_resolver: Callable[[str], str | None] | None = None,
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

    default_ch_b_pool = ["gemini-3.8-flash", "gemini-4-hydrogen", "gemini-3.5-flash-lite"]
    env_ch_b = get_env_stripped("GEMINI_MODEL_CH_B") or get_env_stripped("GEMINI_MODEL")
    if env_ch_b:
        model_pool = [env_ch_b] + [m for m in default_ch_b_pool if m != env_ch_b]
    else:
        model_pool = list(default_ch_b_pool)
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

            prompt = (
                "Please transcribe and translate this podcast audio chunk into structured bilingual segments "
                "according to the rules and schema."
            )
            contents = [file_info, prompt]

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

    default_ch_a_pool = ["gemini-4-carbon", "gemini-4-neon", "gemini-3.8-flash"]
    env_ch_a = get_env_stripped("GEMINI_MODEL_CH_A")
    if env_ch_a:
        model_pool = [env_ch_a] + [m for m in default_ch_a_pool if m != env_ch_a]
    else:
        model_pool = list(default_ch_a_pool)
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

    user_prompt = (
        f"Podcast Episode Title: {title}\n"
        f"Podcast Show: {show_title}\n"
        f"Total Duration (seconds): {duration_sec}\n\n"
        f"Timestamped Transcript:\n"
        f"{transcript_text}\n\n"
        "Please provide the structured summary_zh, key_takeaways_zh, highlights, vocab_notes, and references according to the schema."
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

    highlights = []
    for h in parsed_resp.highlights:
        clamped_sec = max(0, min(int(h.start_sec), duration_sec))
        highlights.append({
            "start_sec": clamped_sec,
            "title_zh": h.title_zh.strip(),
            "summary_zh": h.summary_zh.strip(),
            "quote_en": h.quote_en.strip(),
        })
    highlights.sort(key=lambda x: x["start_sec"])
    if len(highlights) > 10:
        highlights = highlights[:10]

    key_takeaways = [k.strip() for k in parsed_resp.key_takeaways_zh]
    if len(key_takeaways) > 5:
        key_takeaways = key_takeaways[:5]

    vocab_notes = []
    for v in parsed_resp.vocab_notes:
        clamped_sec = max(0, min(int(v.start_sec), duration_sec))
        vocab_notes.append({
            "term": v.term.strip(),
            "meaning_zh": v.meaning_zh.strip(),
            "note_zh": v.note_zh.strip(),
            "start_sec": clamped_sec,
        })
    if len(vocab_notes) > 10:
        vocab_notes = vocab_notes[:10]
    vocab_notes.sort(key=lambda x: x["start_sec"])

    references = []
    for r in parsed_resp.references:
        st_sec = None
        if r.start_sec is not None:
            try:
                st_sec = max(0, min(int(r.start_sec), duration_sec))
            except (ValueError, TypeError):
                st_sec = None
        category = r.category if r.category in ("book", "article", "media", "other") else "other"
        references.append({
            "title": r.title.strip(),
            "creator": r.creator.strip() if r.creator else "",
            "category": category,
            "note_zh": r.note_zh.strip(),
            "start_sec": st_sec,
        })
    if len(references) > 10:
        references = references[:10]
    references.sort(key=lambda x: (x["start_sec"] is None, x["start_sec"] if x["start_sec"] is not None else 0))

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


def atomic_save_episode(show: dict, episode_record: dict) -> None:
    """[7. ATOMIC_SAVE] Save single episode and update show's index.json, then sync shows.json."""
    show_id = show["id"]
    show_dir = DOCS_DATA_DIR / show_id
    episodes_dir = show_dir / "episodes"
    index_file = show_dir / "index.json"

    episodes_dir.mkdir(parents=True, exist_ok=True)

    guid = episode_record["guid"]
    safe_guid = make_safe_guid(guid)
    ep_file = episodes_dir / f"{safe_guid}.json"

    ep_doc = {
        "schema_version": "3.0",
        "show_id": show_id,
        "guid": guid,
        "title": episode_record.get("title", ""),
        "pub_date": episode_record.get("pub_date", ""),
        "duration_sec": int(episode_record.get("duration_sec", 0)),
        "audio_url": episode_record.get("audio_url", ""),
        "summary_zh": episode_record.get("summary_zh", ""),
        "key_takeaways_zh": episode_record.get("key_takeaways_zh", []),
        "highlights": episode_record.get("highlights", []),
        "vocab_notes": episode_record.get("vocab_notes", []),
        "references": episode_record.get("references", []),
        "transcript": episode_record.get("transcript", []),
    }

    tmp_ep_file = ep_file.with_suffix(".json.tmp")
    with open(tmp_ep_file, "w", encoding="utf-8") as f:
        json.dump(ep_doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp_ep_file, ep_file)

    index_data = {
        "schema_version": "3.0",
        "show_id": show_id,
        "podcast_title": show.get("title", ""),
        "rss_url": show.get("rss_url", ""),
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "episodes": [],
    }
    if index_file.exists():
        try:
            with open(index_file, "r", encoding="utf-8") as f:
                index_data = json.load(f)
        except Exception as e:
            log("ERROR", f"Failed to read existing {index_file}: {e}", show_id=show_id)
            sys.exit(1)

    existing_map = {ep["guid"]: ep for ep in index_data.get("episodes", []) if "guid" in ep}
    existing_map[guid] = {
        "guid": guid,
        "title": episode_record.get("title", ""),
        "pub_date": episode_record.get("pub_date", ""),
        "duration_sec": int(episode_record.get("duration_sec", 0)),
        "audio_url": episode_record.get("audio_url", ""),
        "file": f"episodes/{safe_guid}.json",
        "summary_zh": episode_record.get("summary_zh", ""),
        "key_takeaways_zh": episode_record.get("key_takeaways_zh", []),
        "highlights_count": len(episode_record.get("highlights", [])),
        "transcript_count": len(episode_record.get("transcript", [])),
    }

    sorted_episodes = sorted(
        existing_map.values(),
        key=lambda x: x.get("pub_date", ""),
        reverse=True,
    )

    index_data["schema_version"] = "3.0"
    index_data["show_id"] = show_id
    index_data["podcast_title"] = show.get("title", "")
    index_data["rss_url"] = show.get("rss_url", "")
    index_data["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    index_data["episodes"] = sorted_episodes

    tmp_index_file = index_file.with_suffix(".json.tmp")
    with open(tmp_index_file, "w", encoding="utf-8") as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_index_file, index_file)

    log("SAVE", f"Saved episode '{guid}' to {ep_file} and updated {index_file} (total {len(sorted_episodes)} episodes).", show_id=show_id)

    sync_shows_manifest()


def save_smoke_preview(show: dict, episode_record: dict) -> None:
    """Save smoke test preview to docs/data/<show_id>/_smoke_preview.json without touching formal library."""
    show_id = show["id"]
    show_dir = DOCS_DATA_DIR / show_id
    show_dir.mkdir(parents=True, exist_ok=True)
    smoke_file = show_dir / "_smoke_preview.json"

    preview_data = {
        "schema_version": "3.0",
        "show_id": show_id,
        "podcast_title": show.get("title", ""),
        "rss_url": show.get("rss_url", ""),
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "episodes": [episode_record],
    }
    tmp_smoke = smoke_file.with_suffix(".json.tmp")
    with open(tmp_smoke, "w", encoding="utf-8") as f:
        json.dump(preview_data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_smoke, smoke_file)
    log("SAVE", f"[SMOKE] Smoke preview written to {smoke_file}. Main index and episodes untouched.", show_id=show_id)


def process_episode(entry, args, show: dict, glossary_text: str) -> None:
    """Process a single podcast episode through the pipeline."""
    show_id = show["id"]
    guid = get_entry_guid(entry)
    title = entry.get("title", "Untitled Episode")
    pub_date = get_entry_pub_date(entry)
    audio_url = get_entry_audio_url(entry)

    if not audio_url:
        log("ERROR", f"Episode '{title}' ({guid}) has no valid audio URL.", show_id=show_id)
        raise ValueError(f"Episode '{title}' ({guid}) has no valid audio URL.")

    show_notes = get_entry_show_notes(entry, max_chars=3000)
    log("SHOW-NOTES", f"Extracted show notes ({len(show_notes)} chars):\n{show_notes[:500]}...", show_id=show_id)

    temp_dir = None
    try:
        temp_dir = Path(tempfile.mkdtemp(prefix=f"{show_id}_"))

        # [3. DOWNLOAD_AUDIO]
        original_audio = temp_dir / "original.mp3"
        download_audio_file(audio_url, original_audio, show_id=show_id)
        duration_sec = get_audio_duration_sec(original_audio, show_id=show_id)

        # [4. CHUNK_AUDIO]
        chunks = chunk_audio(original_audio, temp_dir, args.chunk_sec, show_id=show_id)

        for chunk in chunks:
            size_bytes = chunk.stat().st_size
            size_mb = size_bytes / (1024 * 1024)
            log("FFMPEG", f"  - {chunk.name}: {size_mb:.2f} MB ({size_bytes} bytes)", show_id=show_id)

        log("FFMPEG", "[EPISODE_SUMMARY]", show_id=show_id)
        log("FFMPEG", f"  guid: {guid}", show_id=show_id)
        log("FFMPEG", f"  title: {title}", show_id=show_id)
        log("FFMPEG", f"  pub_date: {pub_date}", show_id=show_id)
        log("FFMPEG", f"  duration_sec: {duration_sec}", show_id=show_id)
        log("FFMPEG", f"  audio_url: {audio_url}", show_id=show_id)
        log("FFMPEG", f"  chunks_count: {len(chunks)}", show_id=show_id)

        if args.dry_run:
            log("FFMPEG", "[DRY-RUN] Dry run enabled. Slicing verified. Skipping Gemini processing and atomic save.", show_id=show_id)
            return

        # [5. CHANNEL_B]
        log("CH-B", "Calling Channel B for transcription and translation...", show_id=show_id)
        transcript = run_channel_b(
            chunks=chunks,
            glossary_text=glossary_text,
            duration_sec=duration_sec,
            max_chunks=args.max_chunks,
            show_notes=show_notes,
            show_title=show.get("title", ""),
            show_subtitle=show.get("subtitle", ""),
            show_id=show_id,
        )

        # [6. CHANNEL_A]
        log("CH-A", "Calling Channel A for summarization and highlights...", show_id=show_id)
        summary_info = run_channel_a(
            transcript=transcript,
            glossary_text=glossary_text,
            title=title,
            duration_sec=duration_sec,
            show_notes=show_notes,
            show_title=show.get("title", ""),
            show_subtitle=show.get("subtitle", ""),
            show_id=show_id,
        )

        episode_record = {
            "guid": guid,
            "title": title,
            "pub_date": pub_date,
            "duration_sec": duration_sec,
            "audio_url": audio_url,
            "summary_zh": summary_info.get("summary_zh", ""),
            "key_takeaways_zh": summary_info.get("key_takeaways_zh", []),
            "highlights": summary_info.get("highlights", []),
            "vocab_notes": summary_info.get("vocab_notes", []),
            "references": summary_info.get("references", []),
            "transcript": transcript,
        }

        if args.max_chunks > 0:
            save_smoke_preview(show, episode_record)
        else:
            log("SAVE", "Calling atomic save...", show_id=show_id)
            smoke_file = DOCS_DATA_DIR / show_id / "_smoke_preview.json"
            if smoke_file.exists():
                try:
                    smoke_file.unlink()
                    log("SAVE", f"Removed temporary smoke preview file: {smoke_file}", show_id=show_id)
                except Exception:
                    pass
            atomic_save_episode(show, episode_record)
            log("SAVE", f"Model usage statistics: {MODEL_USAGE_STATS}", show_id=show_id)

    finally:
        # [8. CLEANUP]
        if temp_dir and temp_dir.exists():
            log("CLEANUP", f"Cleaning up temporary directory: {temp_dir}", show_id=show_id)
            shutil.rmtree(temp_dir, ignore_errors=True)
            log("CLEANUP", "Temporary directory cleaned up successfully.", show_id=show_id)


def fetch_rss_show_notes_map(url: str, show_id: str = "") -> dict[str, str]:
    """Fetch RSS feed and build memory mapping of {guid: show_notes}.

    If fetch or parse fails, logs a WARN and returns an empty dict without crashing.
    """
    notes_map = {}
    if not url:
        return notes_map
    log("ENRICH", f"Fetching RSS feed to extract show notes map from {url}...", show_id=show_id)
    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=30)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        for entry in feed.entries:
            guid = get_entry_guid(entry)
            if guid:
                notes = get_entry_show_notes(entry, max_chars=3000)
                if notes:
                    notes_map[guid] = notes
        log("ENRICH", f"RSS cached show notes for {len(notes_map)} episode(s).", show_id=show_id)
    except Exception as e:
        log("WARN", f"Failed to fetch RSS show notes from {url}: {e}. Proceeding with empty show notes.", show_id=show_id)
    return notes_map


def run_enrichment_pipeline(args, config: dict) -> None:
    """[ENRICH] Incremental backfill mode for vocab_notes and references using lightweight model pool."""
    all_shows = config.get("shows", [])
    valid_show_ids = {s["id"] for s in all_shows}

    if args.show != "all" and args.show not in valid_show_ids:
        log("ERROR", f"Unknown show_id: '{args.show}'. Valid configured shows: {sorted(list(valid_show_ids))}")
        sys.exit(1)

    if not args.dry_run:
        api_key = get_env_stripped("GEMINI_API_KEY")
        if not api_key:
            log("ERROR", "GEMINI_API_KEY environment variable is required when not in --dry-run mode.")
            sys.exit(1)
        client = genai.Client(api_key=api_key)
    else:
        client = None

    if args.show == "all":
        target_shows = [s for s in all_shows if s.get("enabled", True)]
    else:
        target_shows = [s for s in all_shows if s["id"] == args.show]

    if not target_shows:
        log("FILTER", "No enabled shows found to process. Exiting cleanly.")
        sys.exit(0)

    # Resolve enrichment model pool and thinking level
    default_enrich_pool = ["gemini-3.8-flash", "gemini-4-hydrogen", "gemini-3.5-flash-lite"]
    env_enrich = get_env_stripped("GEMINI_MODEL_ENRICH")
    if env_enrich:
        enrich_model_pool = [env_enrich] + [m for m in default_enrich_pool if m != env_enrich]
    else:
        enrich_model_pool = list(default_enrich_pool)

    enrich_thinking_level = get_env_stripped("GEMINI_THINKING_ENRICH", "low")

    log("ENRICH", f"Enrichment model pool: {enrich_model_pool}")
    log("ENRICH", f"Enrichment thinking level: '{enrich_thinking_level}'")
    if args.dry_run:
        log("ENRICH", "[DRY-RUN] Dry run enabled. No Gemini API calls will be made and no files will be written.")

    total_attempted = 0
    total_failed = 0
    total_skipped = 0

    for show in target_shows:
        show_id = show["id"]
        show_dir = DOCS_DATA_DIR / show_id
        index_file = show_dir / "index.json"

        if not index_file.exists():
            log("ENRICH", f"No index.json found at {index_file}. Skipping show.", show_id=show_id)
            continue

        try:
            with open(index_file, "r", encoding="utf-8") as f:
                index_data = json.load(f)
        except Exception as e:
            log("ERROR", f"Failed to read index.json for '{show_id}': {e}", show_id=show_id)
            continue

        raw_episodes = index_data.get("episodes", [])
        sorted_episodes = sorted(raw_episodes, key=lambda x: x.get("pub_date", ""), reverse=True)

        candidates = []
        for ep_meta in sorted_episodes:
            guid = ep_meta.get("guid")
            if not guid:
                continue
            safe_guid = make_safe_guid(guid)
            ep_file_rel = ep_meta.get("file", f"episodes/{safe_guid}.json")
            ep_file = show_dir / ep_file_rel
            if not ep_file.exists():
                log("WARN", f"Episode file {ep_file} referenced in index does not exist. Skipping.", show_id=show_id)
                continue

            try:
                with open(ep_file, "r", encoding="utf-8") as f:
                    ep_data = json.load(f)
            except Exception as e:
                log("ERROR", f"Failed to read {ep_file}: {e}", show_id=show_id)
                continue

            # Idempotency check: both keys must exist to skip
            if "vocab_notes" in ep_data and "references" in ep_data:
                total_skipped += 1
                continue

            candidates.append((ep_meta, ep_file, ep_data))

        log("ENRICH", f"Found {len(candidates)} episode(s) needing enrichment (already enriched/skipped: {total_skipped}).", show_id=show_id)

        if not candidates:
            log("ENRICH", f"No episodes to enrich for show '{show_id}'.", show_id=show_id)
            continue

        if args.limit > 0:
            candidates = candidates[:args.limit]
            log("ENRICH", f"Selected {len(candidates)} episode(s) to enrich per --limit {args.limit}.", show_id=show_id)

        # Fetch show notes from RSS once for this show
        show_notes_map = fetch_rss_show_notes_map(show.get("rss_url", ""), show_id=show_id)
        glossary_text = load_glossary_for_show(show)

        for ep_meta, ep_file, ep_data in candidates:
            total_attempted += 1
            guid = ep_data.get("guid", ep_meta.get("guid", ""))
            title = ep_data.get("title", ep_meta.get("title", "Untitled Episode"))
            duration_sec = int(ep_data.get("duration_sec", ep_meta.get("duration_sec", 0)))
            show_notes = show_notes_map.get(guid, "")
            hit_show_notes = bool(show_notes)

            transcript = ep_data.get("transcript", [])
            transcript_lines = [
                f"[{format_sec_to_mmss(item.get('start_sec', 0))}] {item.get('speaker', 'Unknown')}: {item.get('en', '')}"
                for item in transcript
            ]
            transcript_text = "\n".join(transcript_lines)

            est_chars = len(title) + len(show_notes) + len(glossary_text) + len(transcript_text)

            log("ENRICH", f"--- Candidate: '{title}' ({guid}) ---", show_id=show_id)
            log("ENRICH", f"  File: {ep_file.name}", show_id=show_id)
            log("ENRICH", f"  Hit RSS Show Notes: {hit_show_notes} ({len(show_notes)} chars)", show_id=show_id)
            log("ENRICH", f"  Transcript items: {len(transcript)} (formatted text: {len(transcript_text)} chars)", show_id=show_id)
            log("ENRICH", f"  Estimated prompt text: ~{est_chars} chars", show_id=show_id)
            log("ENRICH", f"  Target model pool: {enrich_model_pool}", show_id=show_id)
            log("ENRICH", f"  Effective thinking level: '{enrich_thinking_level}'", show_id=show_id)

            if args.dry_run:
                log("ENRICH", f"[DRY-RUN] Would call Gemini API and enrich {ep_file.name}. Skipped.", show_id=show_id)
                continue

            # Non-dry-run: execute API call
            try:
                system_instruction = (
                    f"You are an expert podcast knowledge curator and terminologist for '{show.get('title', '')}' ({show.get('subtitle', '')}).\n"
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

                user_prompt = (
                    f"Podcast Episode Title: {title}\n"
                    f"Podcast Show: {show.get('title', '')}\n"
                    f"Total Duration (seconds): {duration_sec}\n\n"
                    f"Timestamped Transcript:\n"
                    f"{transcript_text}\n\n"
                    "Please provide the structured vocab_notes and references according to the schema."
                )

                parsed_resp: EnrichmentResponse = generate_structured_with_retry(
                    client=client,
                    model_pool=enrich_model_pool,
                    contents=[user_prompt],
                    system_instruction=system_instruction,
                    response_schema=EnrichmentResponse,
                    stage="ENRICH",
                    temperature=0.2,
                    thinking_level=enrich_thinking_level,
                    show_id=show_id,
                )

                vocab_notes = []
                for v in parsed_resp.vocab_notes:
                    clamped_sec = max(0, min(int(v.start_sec), duration_sec))
                    vocab_notes.append({
                        "term": v.term.strip(),
                        "meaning_zh": v.meaning_zh.strip(),
                        "note_zh": v.note_zh.strip(),
                        "start_sec": clamped_sec,
                    })
                if len(vocab_notes) > 10:
                    vocab_notes = vocab_notes[:10]
                vocab_notes.sort(key=lambda x: x["start_sec"])

                references = []
                for r in parsed_resp.references:
                    st_sec = None
                    if r.start_sec is not None:
                        try:
                            st_sec = max(0, min(int(r.start_sec), duration_sec))
                        except (ValueError, TypeError):
                            st_sec = None
                    category = r.category if r.category in ("book", "article", "media", "other") else "other"
                    references.append({
                        "title": r.title.strip(),
                        "creator": r.creator.strip() if r.creator else "",
                        "category": category,
                        "note_zh": r.note_zh.strip(),
                        "start_sec": st_sec,
                    })
                if len(references) > 10:
                    references = references[:10]
                references.sort(key=lambda x: (x["start_sec"] is None, x["start_sec"] if x["start_sec"] is not None else 0))

                # Merge ONLY vocab_notes and references into ep_data, leaving all existing fields untouched
                ep_data["vocab_notes"] = vocab_notes
                ep_data["references"] = references

                tmp_ep = ep_file.with_suffix(".json.tmp")
                with open(tmp_ep, "w", encoding="utf-8") as f:
                    json.dump(ep_data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_ep, ep_file)

                log("ENRICH", f"Successfully enriched {ep_file.name} (vocab_notes: {len(vocab_notes)}, references: {len(references)}).", show_id=show_id)

            except Exception as e:
                total_failed += 1
                log("ERROR", f"Failed to enrich episode '{title}' ({guid}): {e}", show_id=show_id)

    if total_attempted > 0 and total_failed == total_attempted and not args.dry_run:
        log("ERROR", f"All {total_attempted} attempted enrichment(s) failed in this run.")
        sys.exit(1)

    log("ENRICH", f"Enrichment process finished. Attempted: {total_attempted}, Failed: {total_failed}, Skipped: {total_skipped}.")


def main():
    parser = argparse.ArgumentParser(description="Podcast bilingual reading processor (v3.0).")
    parser.add_argument("--show", type=str, default="all", help="Target show ID to process, or 'all' (default: all)")
    parser.add_argument("--limit", type=int, default=1, help="单次最多处理的新单集数量 (default: 1)")
    parser.add_argument("--max-age-days", type=int, default=None, help="仅处理发布时间距当前 UTC 不超过 N 天的单集（0 表示不限制；未设置时使用各节目配置的 default_max_age_days）")
    parser.add_argument("--chunk-sec", type=int, default=180, help="音频切片时长（秒） (default: 180)")
    parser.add_argument("--dry-run", action="store_true", default=False, help="演习模式，不调用 Gemini API、不修改 JSON")
    parser.add_argument("--max-chunks", type=int, default=0, help="仅处理前 M 个切片（0 表示全部，default: 0）")
    parser.add_argument("--migrate-only", action="store_true", default=False, help="仅执行 v2→v3 本地数据迁移，不访问网络、不调用 Gemini API，完成后退出 0")
    parser.add_argument("--enrich", action="store_true", default=False, help="增量补跑模式：仅对存量单集补跑知识卡片（vocab_notes 与 references），低成本轻量模型")
    args = parser.parse_args()

    # Load registry
    config = load_podcasts_config()
    all_shows = config.get("shows", [])
    valid_show_ids = {s["id"] for s in all_shows}

    # Validate --show argument
    if args.show != "all" and args.show not in valid_show_ids:
        log("ERROR", f"Unknown show_id: '{args.show}'. Valid configured shows: {sorted(list(valid_show_ids))}")
        sys.exit(1)

    # [0. MIGRATE_IF_NEEDED]
    if args.migrate_only:
        log("MIGRATE", "Running in --migrate-only mode...")
        migrated = migrate_to_v3_if_needed()
        if migrated:
            log("MIGRATE", "Migration completed successfully. Exiting 0 as requested by --migrate-only.")
        else:
            log("MIGRATE", "Data already in v3 format or nothing to migrate. Exiting 0.")
        sys.exit(0)

    # [ENRICH INCREMENTAL BACKFILL]
    if args.enrich:
        run_enrichment_pipeline(args, config)
        sys.exit(0)

    # Automatic migration if needed before processing (skip in dry-run if manifest exists to keep formal JSONs untouched)
    if not args.dry_run or not SHOWS_MANIFEST_FILE.exists():
        migrate_to_v3_if_needed()

    if not args.dry_run:
        api_key = get_env_stripped("GEMINI_API_KEY")
        if not api_key:
            log("ERROR", "GEMINI_API_KEY environment variable is required when not in --dry-run mode.")
            sys.exit(1)

    ensure_tools_in_path()

    # Determine shows to process
    if args.show == "all":
        target_shows = [s for s in all_shows if s.get("enabled", True)]
    else:
        target_shows = [s for s in all_shows if s["id"] == args.show]

    if not target_shows:
        log("FILTER", "No enabled shows found to process. Exiting cleanly.")
        sys.exit(0)

    total_episodes_attempted = 0
    total_episodes_failed = 0

    for show in target_shows:
        show_id = show["id"]
        log("RSS", f"Processing show '{show.get('title')}' ({show_id})...", show_id=show_id)

        glossary_text = load_glossary_for_show(show)
        feed = fetch_rss(show["rss_url"], show_id=show_id)

        episodes_to_process = filter_new_episodes(
            feed.entries,
            show=show,
            limit=args.limit,
            max_age_days=args.max_age_days,
        )

        if not episodes_to_process:
            log("FILTER", f"No new episodes to process for '{show.get('title')}'.", show_id=show_id)
            continue

        log("FILTER", f"Loaded glossary for '{show_id}' ({len(glossary_text)} characters).", show_id=show_id)

        for entry in episodes_to_process:
            total_episodes_attempted += 1
            try:
                process_episode(entry, args, show, glossary_text)
            except Exception as e:
                total_episodes_failed += 1
                log("ERROR", f"Episode '{entry.get('title', 'Unknown')}' processing failed: {e}", show_id=show_id)

    if total_episodes_attempted > 0 and total_episodes_failed == total_episodes_attempted:
        log("ERROR", f"All {total_episodes_attempted} attempted episode(s) failed in this run.")
        sys.exit(1)

    if total_episodes_attempted == 0 and args.show != "all":
        log("FILTER", f"No new episodes to process for show '{args.show}'. Exiting cleanly.", show_id=args.show)


if __name__ == "__main__":
    main()
