"""RSS feed fetching, new episode filtering, and Show Notes extraction/cleaning."""

import calendar
from datetime import datetime, timedelta, timezone
import html
import json
import re
import sys

import feedparser
import requests

from pipeline.config import (
    DEFAULT_HEADERS,
    DOCS_DATA_DIR,
    log,
)


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
    if show.get("scraper_hook") == "tal":
        from pipeline.hooks_tal import filter_tal_episodes
        return filter_tal_episodes(feed_entries, show=show, limit=limit, max_age_days=max_age_days)

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
