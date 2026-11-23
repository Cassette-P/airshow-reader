"""Storage utilities: safe GUIDs, speaker normalization, atomic saves, and manifest sync."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Optional

from pipeline.config import (
    DOCS_DATA_DIR,
    PROJECT_ROOT,
    SHOWS_MANIFEST_FILE,
    load_podcasts_config,
    log,
)

CH_B_CACHE_DIR = PROJECT_ROOT / ".cache" / "ch_b"


def make_safe_guid(guid: str) -> str:
    """Normalize GUID to a safe filename string: [^A-Za-z0-9._-]+ -> _."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", guid).strip("_")


def get_ch_b_cache_path(show_id: str, guid: str) -> Path:
    """Return path to Channel B cache file: .cache/ch_b/<show_id>__<safe_guid>.json."""
    safe_guid = make_safe_guid(guid)
    return CH_B_CACHE_DIR / f"{show_id}__{safe_guid}.json"


def save_ch_b_cache(
    show_id: str,
    guid: str,
    chunk_sec: int,
    duration_sec: int,
    audio_url: str,
    transcript: list[dict],
) -> Path:
    """Atomically save Channel B intermediate result to cache using .tmp -> os.replace."""
    CH_B_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = get_ch_b_cache_path(show_id, guid)
    tmp_file = cache_file.with_suffix(".json.tmp")
    payload = {
        "show_id": show_id,
        "guid": guid,
        "chunk_sec": chunk_sec,
        "duration_sec": duration_sec,
        "audio_url": audio_url,
        "transcript": transcript,
    }
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_file, cache_file)
    log("CACHE", f"Saved Channel B cache to {cache_file} ({len(transcript)} segments).", show_id=show_id)
    return cache_file


def load_ch_b_cache(
    show_id: str,
    guid: str,
    chunk_sec: int,
) -> Optional[dict]:
    """Load Channel B cache if show_id, guid, chunk_sec match and transcript is non-empty."""
    cache_file = get_ch_b_cache_path(show_id, guid)
    if not cache_file.exists():
        return None
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if (
            data.get("show_id") == show_id
            and data.get("guid") == guid
            and data.get("chunk_sec") == chunk_sec
            and bool(data.get("transcript"))
        ):
            return data
    except Exception as e:
        log("WARN", f"Failed to read Channel B cache {cache_file}: {e}", show_id=show_id)
    return None


def delete_ch_b_cache(show_id: str, guid: str) -> bool:
    """Delete Channel B cache file upon successful formal atomic save."""
    cache_file = get_ch_b_cache_path(show_id, guid)
    if cache_file.exists():
        try:
            cache_file.unlink()
            log("CACHE", f"Cleaned up Channel B cache: {cache_file}", show_id=show_id)
            return True
        except Exception as e:
            log("WARN", f"Failed to remove Channel B cache {cache_file}: {e}", show_id=show_id)
    return False


def normalize_speaker(name: str) -> str:
    """Normalize speaker name by stripping whitespace and title prefixes (Dr., Prof., etc.)."""
    if not name:
        return "Guest"
    s = str(name).strip()
    title_pattern = r"^(?:Dr\.?|Prof\.?|Professor|Ambassador|Mr\.?|Ms\.?|Mrs\.?|Sen\.?|Rep\.?)\s+"
    cleaned = re.sub(title_pattern, "", s, flags=re.IGNORECASE).strip()
    return cleaned if cleaned else s


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
