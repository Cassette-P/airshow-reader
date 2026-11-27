"""Main episode processing pipeline and incremental enrichment orchestrator."""

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from pipeline.audio import (
    chunk_audio,
    download_audio_file,
    get_audio_duration_sec,
)
from pipeline.config import (
    DOCS_DATA_DIR,
    MODEL_USAGE_STATS,
    get_enrich_model_pool,
    get_env_stripped,
    log,
)
from pipeline.gemini import (
    create_genai_client,
    generate_structured_with_retry,
    run_channel_a,
    run_channel_b,
)
from pipeline.links import resolve_references_links
from pipeline.models import (
    EnrichmentResponse,
    clamp_and_sort_references,
    clamp_and_sort_vocab_notes,
)
from pipeline.prompts import (
    build_enrich_system_instruction,
    build_enrich_user_prompt,
    load_glossary_for_show,
)
from pipeline.rss import (
    fetch_rss,
    fetch_rss_show_notes_and_links_map,
    fetch_rss_show_notes_map,
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
    load_ch_b_cache,
    make_safe_guid,
    save_ch_b_cache,
    save_smoke_preview,
)


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

    speaker_roster = []
    if show.get("scraper_hook") == "tal":
        from pipeline.hooks_tal import hook_tal_preprocess
        tal_data = hook_tal_preprocess(entry, show)
        if tal_data.get("audio_url"):
            audio_url = tal_data["audio_url"]
        if tal_data.get("show_notes"):
            show_notes = tal_data["show_notes"]
        speaker_roster = tal_data.get("speaker_roster", [])
        log("SHOW-NOTES", f"[TAL-HOOK] Injected clean acts show notes ({len(show_notes)} chars).", show_id=show_id)
        if speaker_roster:
            log("TAL-HOOK", f"Injected {len(speaker_roster)} speaker(s) into dynamic context.", show_id=show_id)

    can_use_cache = (
        not getattr(args, "no_cache", False)
        and not args.dry_run
        and args.max_chunks == 0
    )
    cached_data = None
    if can_use_cache:
        cached_data = load_ch_b_cache(show_id, guid, args.chunk_sec)

    temp_dir = None
    try:
        if cached_data:
            log("CACHE", "Hit Channel B cache, skipping download/chunk/CH-B", show_id=show_id)
            transcript = cached_data["transcript"]
            duration_sec = int(cached_data.get("duration_sec", 0))
            if not audio_url and cached_data.get("audio_url"):
                audio_url = cached_data["audio_url"]
        else:
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
                content_style=show.get("content_style", "analytical"),
                speaker_roster=speaker_roster,
            )

            if not transcript:
                raise RuntimeError(f"Channel B returned empty transcript for episode '{title}' ({guid}).")

            # Write cache ONLY in formal full mode (--max-chunks == 0 and not --dry-run)
            if not args.dry_run and args.max_chunks == 0:
                save_ch_b_cache(
                    show_id=show_id,
                    guid=guid,
                    chunk_sec=args.chunk_sec,
                    duration_sec=duration_sec,
                    audio_url=audio_url,
                    transcript=transcript,
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
            content_style=show.get("content_style", "analytical"),
        )

        # [6.5 RESOLVE_REFERENCE_LINKS]
        resolved_references = resolve_references_links(
            summary_info.get("references", []),
            show_notes=show_notes,
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
            "references": resolved_references,
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
            delete_ch_b_cache(show_id, guid)
            log("SAVE", f"Model usage statistics: {MODEL_USAGE_STATS}", show_id=show_id)

    finally:
        # [8. CLEANUP]
        if temp_dir and temp_dir.exists():
            log("CLEANUP", f"Cleaning up temporary directory: {temp_dir}", show_id=show_id)
            shutil.rmtree(temp_dir, ignore_errors=True)
            log("CLEANUP", "Temporary directory cleaned up successfully.", show_id=show_id)


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
        client = create_genai_client(api_key=api_key)
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
    enrich_model_pool = get_enrich_model_pool()
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
                system_instruction = build_enrich_system_instruction(
                    show_title=show.get("title", ""),
                    show_subtitle=show.get("subtitle", ""),
                    glossary_text=glossary_text,
                    show_notes=show_notes,
                    content_style=show.get("content_style", "analytical"),
                )
                user_prompt = build_enrich_user_prompt(
                    title=title,
                    show_title=show.get("title", ""),
                    duration_sec=duration_sec,
                    transcript_text=transcript_text,
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
                    client_factory=lambda: create_genai_client(api_key=api_key),
                )

                vocab_notes = clamp_and_sort_vocab_notes(parsed_resp.vocab_notes, duration_sec)
                references = clamp_and_sort_references(parsed_resp.references, duration_sec)
                references = resolve_references_links(
                    references,
                    show_notes=show_notes,
                    show_id=show_id,
                )

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

    if client and hasattr(client, "close"):
        try:
            client.close()
        except Exception:
            pass

    if total_attempted > 0 and total_failed == total_attempted and not args.dry_run:
        log("ERROR", f"All {total_attempted} attempted enrichment(s) failed in this run.")
        sys.exit(1)

    log("ENRICH", f"Enrichment process finished. Attempted: {total_attempted}, Failed: {total_failed}, Skipped: {total_skipped}.")


def run_enrich_links_pipeline(args, config: dict) -> None:
    """[ENRICH-LINKS] Deterministic, idempotent reference link backfill without calling Gemini API."""
    all_shows = config.get("shows", [])
    valid_show_ids = {s["id"] for s in all_shows}

    if args.show != "all" and args.show not in valid_show_ids:
        log("ERROR", f"Unknown show_id: '{args.show}'. Valid configured shows: {sorted(list(valid_show_ids))}")
        sys.exit(1)

    if args.show == "all":
        target_shows = [s for s in all_shows if s.get("enabled", True)]
    else:
        target_shows = [s for s in all_shows if s["id"] == args.show]

    if not target_shows:
        log("FILTER", "No enabled shows found to process. Exiting cleanly.")
        sys.exit(0)

    force_links = bool(getattr(args, "force_links", False))
    if args.dry_run:
        log("LINKS", "[DRY-RUN] Dry run enabled. No external link verification requests or file writes will be performed.")

    total_attempted = 0
    total_failed = 0
    total_skipped = 0

    for show in target_shows:
        show_id = show["id"]
        show_dir = DOCS_DATA_DIR / show_id
        index_file = show_dir / "index.json"

        if not index_file.exists():
            log("LINKS", f"No index.json found at {index_file}. Skipping show.", show_id=show_id)
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
        show_skipped = 0
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

            refs = ep_data.get("references")
            if not isinstance(refs, list) or len(refs) == 0:
                show_skipped += 1
                total_skipped += 1
                continue

            if not force_links and all(isinstance(r, dict) and isinstance(r.get("links"), list) for r in refs):
                show_skipped += 1
                total_skipped += 1
                continue

            candidates.append((ep_meta, ep_file, ep_data))

        log(
            "LINKS",
            f"Found {len(candidates)} episode(s) needing reference links (skipped: {show_skipped}, force={force_links}).",
            show_id=show_id,
        )

        if not candidates:
            continue

        if args.limit > 0:
            candidates = candidates[:args.limit]
            log("LINKS", f"Selected {len(candidates)} episode(s) per --limit {args.limit}.", show_id=show_id)

        show_notes_map: dict[str, str] = {}
        entry_links_map: dict[str, str] = {}
        if not args.dry_run:
            if show.get("scraper_hook") == "tal":
                show_notes_map, entry_links_map = fetch_rss_show_notes_and_links_map(
                    show.get("rss_url", ""), show_id=show_id, max_chars=8000
                )
            else:
                show_notes_map = fetch_rss_show_notes_map(show.get("rss_url", ""), show_id=show_id, max_chars=8000)

        for ep_meta, ep_file, ep_data in candidates:
            total_attempted += 1
            guid = ep_data.get("guid", ep_meta.get("guid", ""))
            title = ep_data.get("title", ep_meta.get("title", "Untitled Episode"))
            refs = ep_data.get("references", [])
            show_notes = show_notes_map.get(guid, "")

            log("LINKS", f"--- Candidate: '{title}' ({guid}) ---", show_id=show_id)
            log(
                "LINKS",
                f"  File: {ep_file.name} | References: {len(refs)} | Hit RSS Show Notes: {bool(show_notes)}",
                show_id=show_id,
            )

            if args.dry_run:
                log("LINKS", f"[DRY-RUN] Would resolve links for {len(refs)} reference(s) in {ep_file.name}. Skipped.", show_id=show_id)
                continue

            try:
                if show.get("scraper_hook") == "tal":
                    from pipeline.hooks_tal import fetch_tal_show_notes_by_episode_meta
                    tal_notes = fetch_tal_show_notes_by_episode_meta(
                        ep_data, show_id=show_id, entry_link=entry_links_map.get(guid, "")
                    )
                    if tal_notes:
                        show_notes = f"{tal_notes}\n\n{show_notes}".strip() if show_notes else tal_notes

                resolved_refs = resolve_references_links(
                    refs,
                    show_notes=show_notes,
                    show_id=show_id,
                )

                # Update ONLY the 'links' field in-place on each existing ReferenceItem
                for orig_ref, res_ref in zip(ep_data["references"], resolved_refs):
                    orig_ref["links"] = res_ref.get("links", [])

                tmp_ep = ep_file.with_suffix(".json.tmp")
                with open(tmp_ep, "w", encoding="utf-8") as f:
                    json.dump(ep_data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_ep, ep_file)

                link_count = sum(len(r.get("links", [])) for r in ep_data["references"])
                log("LINKS", f"Saved {ep_file.name} ({len(refs)} references, {link_count} total links).", show_id=show_id)
            except Exception as e:
                total_failed += 1
                log("ERROR", f"Failed to enrich links for episode '{title}' ({guid}): {e}", show_id=show_id)

    if total_attempted > 0 and total_failed == total_attempted and not args.dry_run:
        log("ERROR", f"All {total_attempted} attempted link-enrichment episode(s) failed in this run.")
        sys.exit(1)

    log("LINKS", f"Enrich-links process finished. Attempted: {total_attempted}, Failed: {total_failed}, Skipped: {total_skipped}.")


def run_main_pipeline(args, config: dict, target_shows: list[dict]) -> tuple[int, int]:
    """Execute main episode fetching and processing loop across target shows."""
    total_episodes_attempted = 0
    total_episodes_failed = 0

    for show in target_shows:
        show_id = show["id"]
        log("RSS", f"Processing show '{show.get('title')}' ({show_id})...", show_id=show_id)

        try:
            glossary_text = load_glossary_for_show(show)
            feed = fetch_rss(show["rss_url"], show_id=show_id)

            episodes_to_process = filter_new_episodes(
                feed.entries,
                show=show,
                limit=args.limit,
                max_age_days=args.max_age_days,
            )
        except Exception as e:
            total_episodes_attempted += 1
            total_episodes_failed += 1
            log("ERROR", f"Show '{show.get('title')}' ({show_id}) failed during RSS/filter stage: {e}", show_id=show_id)
            continue

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

    return total_episodes_attempted, total_episodes_failed
