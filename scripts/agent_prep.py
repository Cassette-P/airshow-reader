#!/usr/bin/env python3
"""Zero-Gemini-API bridge CLI for Antigravity Agent episode ingestion.

Handles the deterministic bookends around Antigravity Agent's native multimodal
transcription (Channel B) and executive/narrative curation (Channel A):

Subcommands:
  1. list        Scan live RSS feeds and list unhandled episodes (latest / gap / archive).
  2. prep        Download MP3, slice chunks with ffmpeg, and export prompts + schemas + manifest.
  3. prompt-ch-b Generate per-chunk Channel B user prompt with cross-chunk continuity context.
  4. merge-ch-b  Validate & stitch per-chunk JSONs into .cache/ch_b/ and export ch_a_user_prompt.txt.
  5. finalize    Validate ch_a.json, resolve verified direct links, atomically save, and run --strict gate.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Optional

from bs4 import BeautifulSoup
import requests

# Guarantee scripts/ directory is in sys.path
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pipeline.audio import (
    chunk_audio,
    download_audio_file,
    get_audio_duration_sec,
)
from pipeline.config import (
    DEFAULT_HEADERS,
    DOCS_DATA_DIR,
    PROJECT_ROOT,
    ensure_tools_in_path,
    get_ch_b_chunk_sec,
    load_podcasts_config,
    log,
)
from pipeline.links import WIKI_HTTP_HEADERS, resolve_references_links
from pipeline.models import (
    ChannelAOutput,
    ChannelBOutput,
    clamp_and_sort_highlights,
    clamp_and_sort_references,
    clamp_and_sort_vocab_notes,
    clamp_key_takeaways,
)
from pipeline.prompts import (
    build_channel_a_system_instruction,
    build_channel_a_user_prompt,
    build_channel_b_system_instruction,
    build_channel_b_user_prompt,
    load_glossary_for_show,
)
from pipeline.rss import (
    fetch_rss,
    filter_new_episodes,
    get_entry_audio_url,
    get_entry_guid,
    get_entry_pub_date,
    get_entry_show_notes,
)
from pipeline.storage import (
    atomic_save_episode,
    delete_ch_b_cache,
    format_sec_to_mmss,
    get_ch_b_cache_path,
    load_ch_b_cache,
    make_safe_guid,
    normalize_speaker,
    parse_mmss_to_sec,
    save_ch_b_cache,
)
from validate_data import run_validation

AGENT_PREP_ROOT = PROJECT_ROOT / ".cache" / "agent_prep"

SLOT_LABELS_ZH = {
    "latest": "最新未入库",
    "gap": "中间断档",
    "archive": "更早往期",
}


def classify_episode_slot(pub_date: str, existing_pub_dates: list[str]) -> str:
    """Classify an unhandled episode relative to existing DB pub_date bounds."""
    valid_dates = [d for d in existing_pub_dates if isinstance(d, str) and d.strip()]
    if not valid_dates:
        return "latest"
    latest = max(valid_dates)
    earliest = min(valid_dates)
    if pub_date > latest:
        return "latest"
    if pub_date >= earliest:
        return "gap"
    return "archive"


def get_work_dir(show_id: str, guid: str) -> Path:
    """Return canonical agent_prep workspace path for (show_id, guid)."""
    safe_guid = make_safe_guid(guid)
    return AGENT_PREP_ROOT / f"{show_id}__{safe_guid}"


def resolve_work_dir_from_args(args) -> Path:
    """Resolve work_dir from --work-dir or (--show + --guid)."""
    if getattr(args, "work_dir", None):
        wd = Path(args.work_dir).resolve()
        if not wd.exists():
            log("ERROR", f"Work directory does not exist: {wd}")
            sys.exit(1)
        return wd

    show_id = getattr(args, "show", "") or ""
    guid_query = getattr(args, "guid", "") or ""
    if not show_id or not guid_query:
        log("ERROR", "Must provide either --work-dir or both --show and --guid.")
        sys.exit(1)

    exact_dir = get_work_dir(show_id, guid_query)
    if exact_dir.exists():
        return exact_dir

    if AGENT_PREP_ROOT.exists():
        q_lower = guid_query.lower()
        safe_q = make_safe_guid(guid_query).lower()
        matches = [
            d for d in sorted(AGENT_PREP_ROOT.iterdir())
            if d.is_dir()
            and d.name.startswith(f"{show_id}__")
            and (q_lower in d.name.lower() or (safe_q and safe_q in d.name.lower()))
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            log("ERROR", f"Ambiguous --guid '{guid_query}'; matched multiple prep dirs: {[m.name for m in matches]}")
            sys.exit(1)

    log("ERROR", f"Could not find prep work directory for show='{show_id}', guid='{guid_query}'. Run 'prep' first.")
    sys.exit(1)


def get_show_config(config: dict, show_id: str) -> dict:
    """Lookup a single show dict by ID from podcasts.json."""
    for s in config.get("shows", []):
        if s.get("id") == show_id:
            return s
    valid_ids = sorted([s.get("id", "") for s in config.get("shows", [])])
    log("ERROR", f"Unknown show_id '{show_id}'. Valid shows: {valid_ids}")
    sys.exit(1)


def load_existing_show_index(show_id: str) -> list[dict]:
    """Load episodes list from docs/data/<show_id>/index.json."""
    index_file = DOCS_DATA_DIR / show_id / "index.json"
    if not index_file.exists():
        return []
    try:
        with open(index_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("episodes", [])
    except Exception as e:
        log("WARN", f"Failed to read {index_file}: {e}", show_id=show_id)
        return []


def fetch_tal_full_transcript_text(episode_number: int, show_id: str = "tal") -> str:
    """Fetch clean full English reference transcript text from TAL official site (optional calibration aid)."""
    transcript_url = f"https://www.thisamericanlife.org/{episode_number}/transcript"
    try:
        resp = requests.get(transcript_url, headers=DEFAULT_HEADERS, timeout=20)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.content, "html.parser")
        root = soup.select_one("article.node-transcript, .transcript-content, #transcript, main, article") or soup
        lines: list[str] = []
        for elem in root.select("div.act, section, h3, h4, p"):
            if elem.name == "h3":
                txt = elem.get_text(" ", strip=True)
                if txt and "subscribe" not in txt.lower():
                    lines.append(f"\n=== {txt} ===")
            elif elem.name == "h4":
                spk = normalize_speaker(elem.get_text(" ", strip=True))
                if spk:
                    lines.append(f"\n[{spk}]")
            elif elem.name == "p":
                # Avoid duplicating nested <p> inside already traversed containers by only taking leaf p
                txt = elem.get_text(" ", strip=True)
                if txt:
                    ts_span = elem.find(attrs={"class": re.compile(r"timestamp|time", re.I)})
                    ts_prefix = f"({ts_span.get_text(strip=True)}) " if ts_span else ""
                    lines.append(f"{ts_prefix}{txt}")
        return "\n".join(lines).strip()
    except Exception as e:
        log("WARN", f"[TAL-HOOK] Optional full transcript text scrape skipped: {e}", show_id=show_id)
        return ""


def resolve_tal_episode_slug_from_transcript(ep_num: int, show_id: str = "tal") -> str:
    """Resolve canonical episode URL 'https://www.thisamericanlife.org/<ep_num>/<slug>' via transcript page."""
    tx_url = f"https://www.thisamericanlife.org/{ep_num}/transcript"
    resp = requests.get(tx_url, headers=DEFAULT_HEADERS, timeout=20, allow_redirects=True)
    if resp.status_code != 200:
        return ""
    soup = BeautifulSoup(resp.content, "html.parser")
    pattern = re.compile(rf"^(?:https?://(?:www\.)?thisamericanlife\.org)?/(?:{ep_num})/([A-Za-z0-9_-]+)(?:[/?#]|$)")
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        m = pattern.match(href)
        if m and m.group(1).lower() != "transcript":
            return f"https://www.thisamericanlife.org/{ep_num}/{m.group(1)}"
    return ""


def scrape_tal_archive_entry_by_number(ep_num: int, show_id: str = "tal") -> dict:
    """Synthesize an RSS-compatible entry dict directly from https://www.thisamericanlife.org/<ep_num>[/<slug>]."""
    base_url = f"https://www.thisamericanlife.org/{ep_num}"
    log("TAL-HOOK", f"Scraping TAL archive episode #{ep_num} directly from {base_url}...", show_id=show_id)
    resp = requests.get(base_url, headers=DEFAULT_HEADERS, timeout=25, allow_redirects=True)
    if resp.status_code == 404:
        slug_url = resolve_tal_episode_slug_from_transcript(ep_num, show_id=show_id)
        if slug_url:
            log("TAL-HOOK", f"Resolved TAL #{ep_num} canonical episode URL via transcript: {slug_url}", show_id=show_id)
            resp = requests.get(slug_url, headers=DEFAULT_HEADERS, timeout=25, allow_redirects=True)
    if resp.status_code != 200:
        raise RuntimeError(f"TAL episode #{ep_num} returned HTTP {resp.status_code} at {base_url}")

    final_url = resp.url or base_url
    soup = BeautifulSoup(resp.content, "html.parser")

    # 1. Extract Drupal node ID for canonical TAL GUID ('<nid> at https://www.thisamericanlife.org')
    nid = ""
    shortlink = soup.find("link", rel="shortlink")
    if shortlink and shortlink.get("href"):
        m = re.search(r"/node/(\d+)", shortlink["href"])
        if m:
            nid = m.group(1)
    if not nid:
        art = soup.find("article", id=re.compile(r"^node-\d+$"))
        if art and art.get("id"):
            nid = art["id"].split("-")[-1]
    if not nid:
        body = soup.find("body")
        if body and body.get("class"):
            for cls in body.get("class", []):
                m = re.match(r"page-node-(\d+)$", str(cls))
                if m:
                    nid = m.group(1)
                    break
    guid = f"{nid} at https://www.thisamericanlife.org" if nid else f"tal-{ep_num} at https://www.thisamericanlife.org"

    # 2. Extract episode title (collapse non-breaking spaces)
    h1 = soup.select_one("h1.node-title, header h1, h1")
    raw_title = re.sub(r"\s+", " ", h1.get_text(" ", strip=True)).strip() if h1 else f"Episode {ep_num}"
    if not re.match(rf"^\s*{ep_num}\s*:", raw_title):
        title = f"{ep_num}: {raw_title}"
    else:
        title = raw_title

    # 3. Extract publication / original air date
    pub_date = ""
    meta_pub = (
        soup.find("meta", property="article:published_time")
        or soup.select_one(".field-name-field-radio-air-date span.date-display-single")
        or soup.select_one(".field-name-field-radio-air-date")
        or soup.find("span", class_="date-display-single")
        or soup.find("time")
    )
    raw_dt = ""
    if meta_pub:
        raw_dt = (
            meta_pub.get("content")
            or meta_pub.get("datetime")
            or re.sub(r"\s+", " ", meta_pub.get_text(" ", strip=True)).strip()
        )
    if raw_dt:
        try:
            dt = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            pub_date = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(raw_dt.strip(), fmt).replace(tzinfo=timezone.utc)
                    pub_date = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                    break
                except ValueError:
                    continue
    if not pub_date:
        pub_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "id": guid,
        "guid": guid,
        "title": title,
        "link": final_url,
        "published": pub_date,
        "enclosures": [{"href": f"https://www.thisamericanlife.org/sites/default/files/audio/upload/archive/{ep_num}.mp3", "type": "audio/mpeg"}],
        "summary": "",
    }


def cmd_list(args, config: dict) -> None:
    """List unhandled episodes from RSS feeds across target shows."""
    all_shows = config.get("shows", [])
    if args.show == "all":
        target_shows = [s for s in all_shows if s.get("enabled", True)]
    else:
        target_shows = [get_show_config(config, args.show)]

    report: list[dict[str, Any]] = []

    for show in target_shows:
        show_id = show["id"]
        existing_eps = load_existing_show_index(show_id)
        existing_dates = [ep.get("pub_date", "") for ep in existing_eps if ep.get("pub_date")]
        latest_db = max(existing_dates) if existing_dates else ""
        earliest_db = min(existing_dates) if existing_dates else ""

        feed = fetch_rss(show["rss_url"], show_id=show_id)
        all_unhandled = filter_new_episodes(
            feed.entries,
            show=show,
            limit=999999,
            max_age_days=0,
        )
        selected = all_unhandled[: args.limit] if args.limit > 0 else all_unhandled

        items = []
        for idx, entry in enumerate(selected, 1):
            guid = get_entry_guid(entry)
            pub_date = get_entry_pub_date(entry)
            title = entry.get("title", "Untitled Episode")
            slot = classify_episode_slot(pub_date, existing_dates)
            has_ch_b_cache = get_ch_b_cache_path(show_id, guid).exists()
            has_prep = (get_work_dir(show_id, guid) / "manifest.json").exists()
            items.append({
                "rank": idx,
                "guid": guid,
                "safe_guid": make_safe_guid(guid),
                "title": title,
                "pub_date": pub_date,
                "slot": slot,
                "slot_zh": SLOT_LABELS_ZH[slot],
                "ch_b_cached": has_ch_b_cache,
                "prepped": has_prep,
            })

        report.append({
            "show_id": show_id,
            "show_title": show.get("title", show_id),
            "rss_total": len(feed.entries),
            "existing_count": len(existing_eps),
            "existing_earliest": earliest_db,
            "existing_latest": latest_db,
            "unhandled_total": len(all_unhandled),
            "shown_count": len(items),
            "candidates": items,
        })

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    for block in report:
        print("\n" + "=" * 78)
        print(
            f"[{block['show_id']}] {block['show_title']}  |  "
            f"已入库: {block['existing_count']} 期 "
            f"({block['existing_earliest'][:10] or '-'} ~ {block['existing_latest'][:10] or '-'})  |  "
            f"RSS 未入库: {block['unhandled_total']} 期（展示前 {block['shown_count']} 期）"
        )
        print("=" * 78)
        if not block["candidates"]:
            print("  (当前 RSS 中无未入库单集)")
            continue
        for c in block["candidates"]:
            badges = [c["slot_zh"]]
            if c["ch_b_cached"]:
                badges.append("CH-B已缓存")
            elif c["prepped"]:
                badges.append("已切片")
            badge_str = " / ".join(badges)
            print(f"  #{c['rank']:02d} [{badge_str}] {c['pub_date'][:10]} | {c['title']}")
            print(f"       guid: {c['guid']}")


def select_rss_entry(entries: list, show: dict, args) -> dict:
    """Resolve a single target episode entry from RSS or TAL archive."""
    show_id = show["id"]

    if getattr(args, "tal_ep", None) is not None:
        if show.get("scraper_hook") != "tal":
            log("ERROR", "--tal-ep is only supported for shows with scraper_hook='tal'.", show_id=show_id)
            sys.exit(1)
        from pipeline.hooks_tal import extract_tal_episode_number
        target_num = int(args.tal_ep)
        for e in entries:
            ep_num = extract_tal_episode_number(title=e.get("title", ""), link=e.get("link", ""))
            if ep_num == target_num:
                log("PREP", f"Matched TAL episode #{target_num} in live RSS feed.", show_id=show_id)
                return e
        log("PREP", f"TAL episode #{target_num} not in rolling RSS feed; falling back to official archive page...", show_id=show_id)
        return scrape_tal_archive_entry_by_number(target_num, show_id=show_id)

    if getattr(args, "latest", False):
        unhandled = filter_new_episodes(entries, show=show, limit=1, max_age_days=0)
        if not unhandled:
            log("ERROR", f"No unhandled episodes found in RSS for '{show_id}'.", show_id=show_id)
            sys.exit(1)
        return unhandled[0]

    guid_query = (getattr(args, "guid", "") or "").strip()
    if not guid_query:
        log("ERROR", "Must specify one of --guid <query>, --latest, or --tal-ep <num>.", show_id=show_id)
        sys.exit(1)

    # 1. Exact GUID or safe_guid match
    safe_query = make_safe_guid(guid_query)
    for e in entries:
        g = get_entry_guid(e)
        if g == guid_query or make_safe_guid(g) == safe_query:
            return e

    # 2. Substring match on GUID or title
    q_lower = guid_query.lower()
    sub_matches = [
        e for e in entries
        if q_lower in get_entry_guid(e).lower() or q_lower in str(e.get("title", "")).lower()
    ]
    if len(sub_matches) == 1:
        return sub_matches[0]
    if len(sub_matches) > 1:
        titles = [f"{get_entry_guid(m)} ({m.get('title', '')})" for m in sub_matches[:5]]
        log("ERROR", f"Ambiguous query '{guid_query}' matched {len(sub_matches)} episodes: {titles}", show_id=show_id)
        sys.exit(1)

    log("ERROR", f"No episode matching '{guid_query}' found in RSS feed for '{show_id}'.", show_id=show_id)
    sys.exit(1)


def cmd_prep(args, config: dict) -> Path:
    """Download MP3, slice into chunks, and export full Agent context & schemas."""
    ensure_tools_in_path()
    show = get_show_config(config, args.show)
    show_id = show["id"]
    glossary_text = load_glossary_for_show(show)

    feed = fetch_rss(show["rss_url"], show_id=show_id)
    entry = select_rss_entry(feed.entries, show=show, args=args)

    guid = get_entry_guid(entry)
    safe_guid = make_safe_guid(guid)
    title = entry.get("title", "Untitled Episode")
    pub_date = get_entry_pub_date(entry)
    audio_url = get_entry_audio_url(entry)
    show_notes = get_entry_show_notes(entry, max_chars=3000)

    work_dir = get_work_dir(show_id, guid)
    manifest_path = work_dir / "manifest.json"
    chunks_dir = work_dir / "chunks"
    ch_b_out_dir = work_dir / "ch_b_chunks"

    if manifest_path.exists() and not args.force:
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                existing_manifest = json.load(f)
            expected_chunks = existing_manifest.get("chunks", [])
            if (
                existing_manifest.get("chunk_sec") == args.chunk_sec
                and expected_chunks
                and all(Path(c["path"]).exists() for c in expected_chunks)
            ):
                log("PREP", f"Reusing existing prepared workspace at {work_dir} ({len(expected_chunks)} chunks).", show_id=show_id)
                print(f"WORK_DIR={work_dir}")
                return work_dir
        except Exception:
            pass

    speaker_roster: list[str] = []
    tal_ep_num: Optional[int] = None
    if show.get("scraper_hook") == "tal":
        from pipeline.hooks_tal import hook_tal_preprocess
        tal_data = hook_tal_preprocess(entry, show)
        if tal_data.get("audio_url"):
            audio_url = tal_data["audio_url"]
        if tal_data.get("show_notes"):
            show_notes = tal_data["show_notes"]
        speaker_roster = tal_data.get("speaker_roster", [])
        tal_ep_num = tal_data.get("episode_number")

    if not audio_url:
        raise RuntimeError(f"Episode '{title}' ({guid}) has no valid audio URL.")

    work_dir.mkdir(parents=True, exist_ok=True)
    if args.force and chunks_dir.exists():
        shutil.rmtree(chunks_dir, ignore_errors=True)
    chunks_dir.mkdir(parents=True, exist_ok=True)
    ch_b_out_dir.mkdir(parents=True, exist_ok=True)

    original_audio = work_dir / "original.mp3"
    if not original_audio.exists() or original_audio.stat().st_size == 0 or args.force:
        download_audio_file(audio_url, original_audio, show_id=show_id)

    duration_sec = get_audio_duration_sec(original_audio, show_id=show_id)
    chunk_paths = chunk_audio(original_audio, chunks_dir, args.chunk_sec, show_id=show_id)

    chunks_meta = []
    acc_offset = 0
    for idx, cp in enumerate(chunk_paths):
        c_dur = get_audio_duration_sec(cp, show_id=show_id)
        out_json = ch_b_out_dir / f"{cp.stem}.json"
        chunks_meta.append({
            "index": idx,
            "name": cp.name,
            "path": str(cp.resolve()),
            "output_json_path": str(out_json.resolve()),
            "duration_sec": c_dur,
            "start_offset_sec": acc_offset,
            "size_bytes": cp.stat().st_size,
        })
        acc_offset += c_dur

    content_style = show.get("content_style", "analytical")
    ch_b_sys = build_channel_b_system_instruction(
        show_title=show.get("title", ""),
        show_subtitle=show.get("subtitle", ""),
        glossary_text=glossary_text,
        show_notes=show_notes,
        content_style=content_style,
        speaker_roster=speaker_roster,
    )
    ch_a_sys = build_channel_a_system_instruction(
        show_title=show.get("title", ""),
        show_subtitle=show.get("subtitle", ""),
        glossary_text=glossary_text,
        show_notes=show_notes,
        content_style=content_style,
    )

    ch_b_sys_path = work_dir / "ch_b_system_prompt.txt"
    ch_a_sys_path = work_dir / "ch_a_system_prompt.txt"
    ch_b_sys_path.write_text(ch_b_sys, encoding="utf-8")
    ch_a_sys_path.write_text(ch_a_sys, encoding="utf-8")

    official_transcript_path_str = ""
    if tal_ep_num is not None:
        full_tx = fetch_tal_full_transcript_text(tal_ep_num, show_id=show_id)
        if full_tx:
            tx_path = work_dir / "official_transcript.txt"
            tx_path.write_text(full_tx, encoding="utf-8")
            official_transcript_path_str = str(tx_path.resolve())
            log("PREP", f"Saved official TAL English transcript reference ({len(full_tx)} chars) to {tx_path.name}.", show_id=show_id)

    manifest = {
        "schema_version": "1.0",
        "show_id": show_id,
        "show_title": show.get("title", ""),
        "show_subtitle": show.get("subtitle", ""),
        "content_style": content_style,
        "guid": guid,
        "safe_guid": safe_guid,
        "title": title,
        "pub_date": pub_date,
        "duration_sec": duration_sec,
        "audio_url": audio_url,
        "chunk_sec": args.chunk_sec,
        "show_notes": show_notes,
        "speaker_roster": speaker_roster,
        "official_transcript_path": official_transcript_path_str,
        "ch_b_system_prompt_path": str(ch_b_sys_path.resolve()),
        "ch_a_system_prompt_path": str(ch_a_sys_path.resolve()),
        "ch_b_chunks_dir": str(ch_b_out_dir.resolve()),
        "ch_a_output_path": str((work_dir / "ch_a.json").resolve()),
        "ch_a_user_prompt_path": str((work_dir / "ch_a_user_prompt.txt").resolve()),
        "ch_b_json_schema": ChannelBOutput.model_json_schema(),
        "ch_a_json_schema": ChannelAOutput.model_json_schema(),
        "chunks": chunks_meta,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    log(
        "PREP",
        f"Prepared '{title}' ({duration_sec}s, {len(chunks_meta)} chunks) at {work_dir}",
        show_id=show_id,
    )
    print(f"WORK_DIR={work_dir}")
    return work_dir


def build_next_ch_b_user_prompt(work_dir: Path, chunk_index: int) -> str:
    """Build per-chunk Channel B user prompt using completed earlier chunks in work_dir."""
    manifest_path = work_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    chunks = manifest.get("chunks", [])
    total_chunks = len(chunks)
    if chunk_index < 0 or chunk_index >= total_chunks:
        raise ValueError(f"chunk_index {chunk_index} out of range [0, {total_chunks - 1}]")

    observed_speakers: list[str] = []
    seen_spk: set[str] = set()
    prev_tail: list[dict] = []

    for i in range(chunk_index):
        out_p = Path(chunks[i]["output_json_path"])
        if not out_p.exists():
            continue
        data = json.loads(out_p.read_text(encoding="utf-8"))
        segs = data.get("segments", [])
        for s in segs:
            norm_s = normalize_speaker(s.get("speaker", ""))
            if norm_s and norm_s not in seen_spk:
                seen_spk.add(norm_s)
                observed_speakers.append(norm_s)
        if i == chunk_index - 1 and segs:
            prev_tail = [
                {
                    "speaker": normalize_speaker(s.get("speaker", "")),
                    "en": str(s.get("en", "")).strip(),
                    "zh": str(s.get("zh", "")).strip(),
                }
                for s in segs[-2:]
            ]

    return build_channel_b_user_prompt(
        chunk_index=chunk_index,
        total_chunks=total_chunks,
        prev_tail_segments=prev_tail or None,
        observed_speakers=observed_speakers or None,
    )


def cmd_prompt_ch_b(args) -> None:
    """Output Channel B user prompt for a given chunk index."""
    work_dir = resolve_work_dir_from_args(args)
    prompt_text = build_next_ch_b_user_prompt(work_dir, args.chunk_index)
    if args.out:
        Path(args.out).write_text(prompt_text, encoding="utf-8")
    else:
        print(prompt_text)


def merge_ch_b_chunks(work_dir: Path) -> list[dict]:
    """Validate and merge all ch_b_chunks/chunk_XXX.json into .cache/ch_b/ and write ch_a_user_prompt.txt."""
    manifest_path = work_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"manifest.json not found in {work_dir}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    show_id = manifest["show_id"]
    guid = manifest["guid"]
    title = manifest["title"]
    show_title = manifest["show_title"]
    duration_sec = int(manifest["duration_sec"])
    chunk_sec = int(manifest["chunk_sec"])
    audio_url = manifest["audio_url"]
    chunks = manifest.get("chunks", [])

    if not chunks:
        raise RuntimeError(f"No chunks listed in {manifest_path}")

    missing_files = [c["output_json_path"] for c in chunks if not Path(c["output_json_path"]).exists()]
    if missing_files:
        raise FileNotFoundError(
            f"Missing {len(missing_files)}/{len(chunks)} Channel B chunk JSON(s), first missing: {missing_files[0]}"
        )

    all_raw_segments: list[dict] = []
    for c in chunks:
        out_path = Path(c["output_json_path"])
        raw_obj = json.loads(out_path.read_text(encoding="utf-8"))
        parsed = ChannelBOutput.model_validate(raw_obj)
        chunk_dur = int(c["duration_sec"])
        base_sec = int(c["start_offset_sec"])

        for seg in parsed.segments:
            offset = parse_mmss_to_sec(seg.timestamp, fallback_sec=0)
            clamped_offset = max(0, min(offset, chunk_dur))
            global_start = max(0, min(base_sec + clamped_offset, duration_sec))
            all_raw_segments.append({
                "start_sec": global_start,
                "speaker": normalize_speaker(seg.speaker),
                "en": seg.en.strip(),
                "zh": seg.zh.strip(),
            })

    if not all_raw_segments:
        raise ValueError(f"Merged transcript for {work_dir.name} has 0 total segments.")

    final_transcript: list[dict] = []
    prev_start_sec = 0
    for idx, seg in enumerate(all_raw_segments, 1):
        st = seg["start_sec"]
        if st < prev_start_sec:
            st = prev_start_sec
        else:
            prev_start_sec = st
        final_transcript.append({
            "id": idx,
            "start_sec": st,
            "speaker": seg["speaker"],
            "en": seg["en"],
            "zh": seg["zh"],
        })

    save_ch_b_cache(
        show_id=show_id,
        guid=guid,
        chunk_sec=chunk_sec,
        duration_sec=duration_sec,
        audio_url=audio_url,
        transcript=final_transcript,
    )

    tx_lines = [
        f"[{format_sec_to_mmss(item['start_sec'])}] {item['speaker']}: {item['en']}"
        for item in final_transcript
    ]
    ch_a_user_prompt = build_channel_a_user_prompt(
        title=title,
        show_title=show_title,
        duration_sec=duration_sec,
        transcript_text="\n".join(tx_lines),
    )
    ch_a_prompt_path = Path(manifest["ch_a_user_prompt_path"])
    ch_a_prompt_path.write_text(ch_a_user_prompt, encoding="utf-8")

    log(
        "MERGE-CH-B",
        f"Merged {len(chunks)} chunks -> {len(final_transcript)} segments; "
        f"wrote {ch_a_prompt_path.name} ({len(ch_a_user_prompt)} chars).",
        show_id=show_id,
    )
    return final_transcript


def cmd_merge_ch_b(args) -> None:
    """CLI handler for merge-ch-b."""
    work_dir = resolve_work_dir_from_args(args)
    merge_ch_b_chunks(work_dir)


def finalize_episode(
    work_dir: Path,
    config: dict,
    ch_a_file: Optional[Path] = None,
    skip_links: bool = False,
    keep_audio: bool = False,
    skip_validate: bool = False,
) -> Path:
    """Validate Channel A JSON, resolve links, atomically save episode, clean audio, and run strict validation."""
    manifest_path = work_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"manifest.json not found in {work_dir}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    show_id = manifest["show_id"]
    show = get_show_config(config, show_id)
    guid = manifest["guid"]
    safe_guid = manifest["safe_guid"]
    duration_sec = int(manifest["duration_sec"])
    chunk_sec = int(manifest["chunk_sec"])
    show_notes = manifest.get("show_notes", "")

    cached_ch_b = load_ch_b_cache(show_id, guid, chunk_sec)
    if cached_ch_b:
        transcript = cached_ch_b["transcript"]
    else:
        log("FINALIZE", "Channel B cache not found; auto-merging ch_b_chunks first...", show_id=show_id)
        transcript = merge_ch_b_chunks(work_dir)

    target_ch_a = ch_a_file or Path(manifest["ch_a_output_path"])
    if not target_ch_a.exists():
        raise FileNotFoundError(f"Channel A JSON file not found at {target_ch_a}")

    raw_ch_a = json.loads(target_ch_a.read_text(encoding="utf-8"))
    parsed_ch_a = ChannelAOutput.model_validate(raw_ch_a)

    highlights = clamp_and_sort_highlights(parsed_ch_a.highlights, duration_sec)
    key_takeaways = clamp_key_takeaways(parsed_ch_a.key_takeaways_zh)
    vocab_notes = clamp_and_sort_vocab_notes(parsed_ch_a.vocab_notes, duration_sec)
    references = clamp_and_sort_references(parsed_ch_a.references, duration_sec)

    if not skip_links:
        references = resolve_references_links(
            references,
            show_notes=show_notes,
            show_id=show_id,
        )

    episode_record = {
        "guid": guid,
        "title": manifest["title"],
        "pub_date": manifest["pub_date"],
        "duration_sec": duration_sec,
        "audio_url": manifest["audio_url"],
        "summary_zh": parsed_ch_a.summary_zh.strip(),
        "key_takeaways_zh": key_takeaways,
        "highlights": highlights,
        "vocab_notes": vocab_notes,
        "references": references,
        "transcript": transcript,
    }

    atomic_save_episode(show, episode_record)
    delete_ch_b_cache(show_id, guid)

    ep_path = DOCS_DATA_DIR / show_id / "episodes" / f"{safe_guid}.json"

    if not skip_validate:
        exit_code = run_validation(target_show=show_id, strict=True)
        if exit_code != 0:
            raise RuntimeError(f"validate_data.py --strict failed for show '{show_id}' (exit code {exit_code}).")

    if not keep_audio:
        orig_mp3 = work_dir / "original.mp3"
        if orig_mp3.exists():
            orig_mp3.unlink(missing_ok=True)
        chunks_dir = work_dir / "chunks"
        if chunks_dir.exists():
            shutil.rmtree(chunks_dir, ignore_errors=True)
        log("CLEANUP", f"Removed temporary audio binaries under {work_dir}.", show_id=show_id)

    log("FINALIZE", f"Episode '{manifest['title']}' finalized and verified at {ep_path}", show_id=show_id)
    return ep_path


def cmd_finalize(args, config: dict) -> None:
    """CLI handler for finalize."""
    work_dir = resolve_work_dir_from_args(args)
    ch_a_file = Path(args.ch_a_file).resolve() if args.ch_a_file else None
    finalize_episode(
        work_dir=work_dir,
        config=config,
        ch_a_file=ch_a_file,
        skip_links=args.skip_links,
        keep_audio=args.keep_audio,
        skip_validate=args.skip_validate,
    )


def parse_batch_spec(spec: str, config: dict) -> argparse.Namespace:
    """Parse '<show_id>:<target>' (e.g. 'sinica:latest', 'airshow:Buzzsprout-19674810', 'tal:585')."""
    if ":" not in spec:
        raise ValueError(f"Invalid episode spec '{spec}'. Expected '<show_id>:<latest|guid|ep_num>'.")
    show_id, target = spec.split(":", 1)
    show_id = show_id.strip()
    target = target.strip()
    show = get_show_config(config, show_id)
    ns = argparse.Namespace(
        show=show_id,
        guid="",
        tal_ep=None,
        latest=False,
        chunk_sec=get_ch_b_chunk_sec(),
        force=False,
        work_dir="",
        ch_a_file="",
        skip_links=False,
        keep_audio=False,
        skip_validate=False,
    )
    if target.lower() == "latest":
        ns.latest = True
    elif show.get("scraper_hook") == "tal" and re.fullmatch(r"(?:ep)?\d+", target, flags=re.I):
        ns.tal_ep = int(re.sub(r"^ep", "", target, flags=re.I))
    else:
        ns.guid = target
    return ns


def cmd_prep_many(args, config: dict) -> None:
    """Prepare multiple episodes sequentially in a single CLI invocation."""
    work_dirs: list[str] = []
    for idx, spec in enumerate(args.specs, 1):
        log("PREP-MANY", f"=== [{idx}/{len(args.specs)}] Preparing {spec} ===")
        sub_args = parse_batch_spec(spec, config)
        sub_args.chunk_sec = args.chunk_sec
        sub_args.force = args.force
        wd = cmd_prep(sub_args, config)
        work_dirs.append(str(wd))
    print("PREPARED_WORK_DIRS_JSON=" + json.dumps(work_dirs, ensure_ascii=False))


def cmd_finalize_many(args, config: dict) -> None:
    """Finalize multiple prepared work directories sequentially in a single CLI invocation."""
    for idx, wd_str in enumerate(args.work_dirs, 1):
        wd = Path(wd_str).resolve()
        log("FINALIZE-MANY", f"=== [{idx}/{len(args.work_dirs)}] Finalizing {wd.name} ===")
        finalize_episode(
            work_dir=wd,
            config=config,
            ch_a_file=None,
            skip_links=args.skip_links,
            keep_audio=args.keep_audio,
            skip_validate=args.skip_validate,
        )


def cmd_resolve_links(args, config: dict) -> None:
    """Resolve verified external links for saved episodes corresponding to given work directories."""
    # Probe outbound API reachability first so sandbox network blocks fail fast instead of silently writing []
    probe = requests.get(
        "https://en.wikipedia.org/w/api.php?action=query&meta=siteinfo&format=json",
        headers=WIKI_HTTP_HEADERS,
        timeout=10,
    )
    if probe.status_code == 403:
        probe.raise_for_status()

    touched_shows: set[str] = set()
    for idx, wd_str in enumerate(args.work_dirs, 1):
        wd = Path(wd_str).resolve()
        manifest = json.loads((wd / "manifest.json").read_text(encoding="utf-8"))
        show_id = manifest["show_id"]
        safe_guid = manifest["safe_guid"]
        show_notes = manifest.get("show_notes", "")
        ep_file = DOCS_DATA_DIR / show_id / "episodes" / f"{safe_guid}.json"
        if not ep_file.exists():
            raise FileNotFoundError(f"Saved episode file not found: {ep_file}")

        ep_data = json.loads(ep_file.read_text(encoding="utf-8"))
        refs = ep_data.get("references", [])
        log("LINKS", f"=== [{idx}/{len(args.work_dirs)}] Resolving links for '{manifest['title']}' ({len(refs)} refs) ===", show_id=show_id)
        if refs:
            resolved = resolve_references_links(refs, show_notes=show_notes, show_id=show_id)
            for orig_ref, res_ref in zip(ep_data["references"], resolved):
                orig_ref["links"] = res_ref.get("links", [])
            tmp_ep = ep_file.with_suffix(".json.tmp")
            tmp_ep.write_text(json.dumps(ep_data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_ep.replace(ep_file)
        touched_shows.add(show_id)

    for sid in sorted(touched_shows):
        rc = run_validation(target_show=sid, strict=True)
        if rc != 0:
            raise RuntimeError(f"Strict validation failed after link resolution for '{sid}'.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Zero-Gemini-API bridge CLI for Antigravity Agent episode ingestion."
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # 1. list
    p_list = sub.add_parser("list", help="Scan live RSS feeds and list unhandled episodes")
    p_list.add_argument("--show", type=str, default="all", help="Target show ID or 'all' (default: all)")
    p_list.add_argument("--limit", type=int, default=20, help="Max unhandled episodes per show (0 = all, default: 20)")
    p_list.add_argument("--json", action="store_true", default=False, help="Output machine-readable JSON")

    # 2. prep
    p_prep = sub.add_parser("prep", help="Download audio, slice chunks, and export Agent prompts + schemas")
    p_prep.add_argument("--show", type=str, required=True, help="Target show ID (e.g. airshow, sinica, tal)")
    p_prep.add_argument("--guid", type=str, default="", help="Episode GUID, safe_guid, or unique title/GUID substring")
    p_prep.add_argument("--tal-ep", type=int, default=None, help="TAL episode number (falls back to official archive if not in RSS)")
    p_prep.add_argument("--latest", action="store_true", default=False, help="Select the newest unhandled episode in RSS")
    p_prep.add_argument("--chunk-sec", type=int, default=get_ch_b_chunk_sec(), help="Chunk duration in seconds (default: 300)")
    p_prep.add_argument("--force", action="store_true", default=False, help="Force re-download and re-slice")

    # 3. prompt-ch-b
    p_pcb = sub.add_parser("prompt-ch-b", help="Print per-chunk Channel B user prompt with continuity context")
    p_pcb.add_argument("--work-dir", type=str, default="", help="Path to .cache/agent_prep/<show>__<guid>")
    p_pcb.add_argument("--show", type=str, default="", help="Show ID (used with --guid if --work-dir omitted)")
    p_pcb.add_argument("--guid", type=str, default="", help="Episode GUID or substring (used with --show)")
    p_pcb.add_argument("--chunk-index", type=int, required=True, help="0-based chunk index")
    p_pcb.add_argument("--out", type=str, default="", help="Optional file path to write prompt text")

    # 4. merge-ch-b
    p_merge = sub.add_parser("merge-ch-b", help="Validate & merge per-chunk JSONs into .cache/ch_b/ and ch_a_user_prompt.txt")
    p_merge.add_argument("--work-dir", type=str, default="", help="Path to .cache/agent_prep/<show>__<guid>")
    p_merge.add_argument("--show", type=str, default="", help="Show ID (used with --guid if --work-dir omitted)")
    p_merge.add_argument("--guid", type=str, default="", help="Episode GUID or substring (used with --show)")

    # 5. finalize
    p_fin = sub.add_parser("finalize", help="Validate ch_a.json, resolve links, save episode, and run --strict gate")
    p_fin.add_argument("--work-dir", type=str, default="", help="Path to .cache/agent_prep/<show>__<guid>")
    p_fin.add_argument("--show", type=str, default="", help="Show ID (used with --guid if --work-dir omitted)")
    p_fin.add_argument("--guid", type=str, default="", help="Episode GUID or substring (used with --show)")
    p_fin.add_argument("--ch-a-file", type=str, default="", help="Override path to Channel A JSON (default: <work_dir>/ch_a.json)")
    p_fin.add_argument("--skip-links", action="store_true", default=False, help="Skip external reference link resolution")
    p_fin.add_argument("--keep-audio", action="store_true", default=False, help="Keep original.mp3 and chunks/*.mp3 after finalize")
    p_fin.add_argument("--skip-validate", action="store_true", default=False, help="Skip automatic validate_data.py --strict check")

    # 6. prep-many
    p_pm = sub.add_parser("prep-many", help="Download and slice multiple '<show>:<target>' specs in one run")
    p_pm.add_argument("specs", nargs="+", help="Episode specs, e.g. sinica:latest airshow:Buzzsprout-19674810 tal:585")
    p_pm.add_argument("--chunk-sec", type=int, default=get_ch_b_chunk_sec(), help="Chunk duration in seconds (default: 300)")
    p_pm.add_argument("--force", action="store_true", default=False, help="Force re-download and re-slice")

    # 7. finalize-many
    p_fm = sub.add_parser("finalize-many", help="Finalize multiple work directories in one run")
    p_fm.add_argument("work_dirs", nargs="+", help="Paths to prepared work directories")
    p_fm.add_argument("--skip-links", action="store_true", default=False, help="Skip external reference link resolution")
    p_fm.add_argument("--keep-audio", action="store_true", default=False, help="Keep original.mp3 and chunks/*.mp3 after finalize")
    p_fm.add_argument("--skip-validate", action="store_true", default=False, help="Skip automatic validate_data.py --strict check")

    # 8. resolve-links
    p_rl = sub.add_parser("resolve-links", help="Resolve external verified links in-place for finalized work_dirs")
    p_rl.add_argument("work_dirs", nargs="+", help="Paths to prepared work directories")

    return parser


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args()
    config = load_podcasts_config()

    if args.subcommand == "list":
        cmd_list(args, config)
    elif args.subcommand == "prep":
        cmd_prep(args, config)
    elif args.subcommand == "prep-many":
        cmd_prep_many(args, config)
    elif args.subcommand == "prompt-ch-b":
        cmd_prompt_ch_b(args)
    elif args.subcommand == "merge-ch-b":
        cmd_merge_ch_b(args)
    elif args.subcommand == "finalize":
        cmd_finalize(args, config)
    elif args.subcommand == "finalize-many":
        cmd_finalize_many(args, config)
    elif args.subcommand == "resolve-links":
        cmd_resolve_links(args, config)


if __name__ == "__main__":
    main()
