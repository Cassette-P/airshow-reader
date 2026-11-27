"""RSS feed fetching, new episode filtering, and Show Notes extraction/cleaning."""

import calendar
from datetime import datetime, timedelta, timezone
import html
import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import feedparser
import requests

from pipeline.config import (
    DEFAULT_HEADERS,
    DOCS_DATA_DIR,
    log,
)
from pipeline.links import extract_show_notes_links, sanitize_url


def html_to_markdown_show_notes(
    raw_html: str,
    max_chars: int = 3000,
    base_url: str = "",
) -> str:
    """Sanitize HTML Show Notes while preserving valid <a href> as Markdown [anchor](url)."""
    if not raw_html:
        return ""

    soup = BeautifulSoup(html.unescape(raw_html), "html.parser")

    # Remove non-content elements
    for tag in soup(["script", "style"]):
        tag.decompose()

    # Convert <a href="...">anchor</a> to Markdown [anchor](clean_url) when safe & non-sponsored
    for a_tag in soup.find_all("a"):
        href = (a_tag.get("href") or "").strip()
        anchor_text = re.sub(r"\s+", " ", a_tag.get_text(" ", strip=True)).strip()
        if href and base_url and href.startswith("/"):
            href = urljoin(base_url, href)
        clean_url = sanitize_url(href) if href.startswith(("http://", "https://")) else ""
        if clean_url:
            label = anchor_text or clean_url
            # Escape brackets in anchor text so Markdown syntax remains well-formed
            safe_label = label.replace("[", "(").replace("]", ")")
            a_tag.replace_with(f"[{safe_label}]({clean_url})")
        else:
            a_tag.replace_with(anchor_text)

    # Preserve paragraph and line breaks
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "li", "div", "h1", "h2", "h3", "h4", "h5", "h6"]):
        block.insert_before("\n")
        block.insert_after("\n")

    text = soup.get_text(" ")
    # Clean whitespace line by line
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    if max_chars > 0 and len(text) > max_chars:
        cut = text[:max_chars]
        # Avoid cutting in the middle of a Markdown link [label](url)
        last_open_bracket = cut.rfind("[")
        last_close_paren = cut.rfind(")")
        if last_open_bracket > last_close_paren and last_open_bracket > int(max_chars * 0.7):
            text = cut[:last_open_bracket].rstrip() + "..."
        else:
            last_space = cut.rfind(" ")
            if last_space > int(max_chars * 0.8):
                text = cut[:last_space] + "..."
            else:
                text = cut + "..."
    return text


def get_entry_show_notes(entry, max_chars: int = 3000) -> str:
    """Extract and sanitize show notes from RSS entry (content/summary/description), preserving valid links."""
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

    return html_to_markdown_show_notes(raw_text, max_chars=max_chars)


def get_entry_show_notes_links(entry, max_chars: int = 8000) -> list[dict[str, str]]:
    """Extract structured clean links from an RSS entry's Show Notes."""
    return extract_show_notes_links(get_entry_show_notes(entry, max_chars=max_chars))


def fetch_rss(url: str, show_id: str = ""):
    """[1. FETCH_RSS] 抓取 RSS feed."""
    log("RSS", f"Fetching RSS feed from {url}...", show_id=show_id)
    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log("ERROR", f"Failed to fetch RSS: {e}", show_id=show_id)
        raise RuntimeError(f"Failed to fetch RSS from {url}: {e}") from e

    feed = feedparser.parse(resp.content)
    if feed.bozo and not feed.entries:
        bozo_exc = feed.get("bozo_exception", "Unknown error")
        log("ERROR", f"Failed to parse RSS feed: {bozo_exc}", show_id=show_id)
        raise RuntimeError(f"Failed to parse RSS feed from {url}: {bozo_exc}")

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
            raise

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


def fetch_rss_show_notes_map(url: str, show_id: str = "", max_chars: int = 3000) -> dict[str, str]:
    """Fetch RSS feed and build memory mapping of {guid: show_notes}.

    If fetch or parse fails, logs a WARN and returns an empty dict without crashing.
    """
    notes_map, _ = fetch_rss_show_notes_and_links_map(url, show_id=show_id, max_chars=max_chars)
    return notes_map


def fetch_rss_show_notes_and_links_map(
    url: str,
    show_id: str = "",
    max_chars: int = 3000,
) -> tuple[dict[str, str], dict[str, str]]:
    """Fetch RSS feed and return ({guid: show_notes}, {guid: entry_link})."""
    notes_map: dict[str, str] = {}
    links_map: dict[str, str] = {}
    if not url:
        return notes_map, links_map
    log("ENRICH", f"Fetching RSS feed to extract show notes map from {url}...", show_id=show_id)
    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=30)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        for entry in feed.entries:
            guid = get_entry_guid(entry)
            if guid:
                notes = get_entry_show_notes(entry, max_chars=max_chars)
                if notes:
                    notes_map[guid] = notes
                entry_link = str(entry.get("link") or "").strip()
                if entry_link.startswith(("http://", "https://")):
                    links_map[guid] = entry_link
        log("ENRICH", f"RSS cached show notes for {len(notes_map)} episode(s).", show_id=show_id)
    except Exception as e:
        log("WARN", f"Failed to fetch RSS show notes from {url}: {e}. Proceeding with empty show notes.", show_id=show_id)
    return notes_map, links_map
