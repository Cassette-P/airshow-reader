#!/usr/bin/env python3
"""
scripts/validate_data.py - Data Contract Validator for airshow-reader (v3.0 / v3.1)

Validates:
1. Global manifest (docs/data/shows.json)
2. Show indices (docs/data/<show_id>/index.json)
3. Episode files (docs/data/<show_id>/episodes/<safe_guid>.json)
4. Knowledge cards (vocab_notes, references) and speaker normalizations

Standard library only. Exits with 0 on success, 1 on validation error (or on warning in --strict mode).
"""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

# Project root and data directories
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCS_DATA_DIR = PROJECT_ROOT / "docs" / "data"
PODCASTS_CONFIG_FILE = PROJECT_ROOT / "podcasts.json"

# Schema version constant
EXPECTED_SCHEMA_VERSION = "3.0"

# Disallowed title prefixes for speakers
TITLE_PREFIX_PATTERN = re.compile(
    r"^(?:Dr\.?|Prof\.?|Professor|Ambassador|Mr\.?|Ms\.?|Mrs\.?|Sen\.?|Rep\.?)\s+",
    re.IGNORECASE,
)

# Reference categories allowed by v3.1 contract
VALID_REFERENCE_CATEGORIES = {"book", "article", "media", "other"}


def is_valid_iso8601_utc(dt_str: Any) -> bool:
    """Validate that dt_str is a valid ISO-8601 UTC timestamp string."""
    if not isinstance(dt_str, str) or not dt_str:
        return False
    # Pattern: YYYY-MM-DDTHH:MM:SSZ or with subseconds / +00:00
    if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$", dt_str):
        return False
    try:
        # Normalize trailing Z to +00:00 for fromisoformat compatibility across Python versions
        normalized = dt_str[:-1] + "+00:00" if dt_str.endswith("Z") else dt_str
        datetime.fromisoformat(normalized)
        return True
    except (ValueError, TypeError):
        return False


def is_int(val: Any) -> bool:
    """Return True if val is an integer and not a boolean."""
    return isinstance(val, int) and not isinstance(val, bool)


def make_safe_guid(guid: str) -> str:
    """Normalize GUID to safe filename matching SPEC and process.py."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", guid).strip("_")


def load_podcasts_config() -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Load podcasts.json with multi-encoding fallback."""
    if not PODCASTS_CONFIG_FILE.exists():
        return None, f"podcasts.json not found at {PODCASTS_CONFIG_FILE}"
    for enc in ["utf-8", "utf-8-sig", "gb18030", "gbk", "cp936"]:
        try:
            with open(PODCASTS_CONFIG_FILE, "r", encoding=enc) as f:
                return json.load(f), None
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return None, "Failed to decode podcasts.json with supported encodings (utf-8, gbk, etc.)"


def load_json_file(file_path: Path) -> Tuple[Optional[Any], Optional[str]]:
    """Load JSON file strictly using UTF-8."""
    if not file_path.exists():
        return None, f"File does not exist: {file_path}"
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except UnicodeDecodeError as e:
        return None, f"Encoding error reading {file_path} (expected UTF-8): {e}"
    except json.JSONDecodeError as e:
        return None, f"Malformed JSON in {file_path}: {e}"
    except Exception as e:
        return None, f"Error reading {file_path}: {e}"


class ValidationReporter:
    def __init__(self, strict: bool = False):
        self.strict = strict
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.shows_checked: List[str] = []
        self.episodes_checked_count = 0

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    @property
    def has_failures(self) -> bool:
        if self.errors:
            return True
        if self.strict and self.warnings:
            return True
        return False


def validate_shows_manifest(
    reporter: ValidationReporter,
    configured_show_ids: Set[str],
    target_show: str = "all",
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Validate docs/data/shows.json and return registered show dicts mapped by id."""
    manifest_path = DOCS_DATA_DIR / "shows.json"
    manifest_data, err = load_json_file(manifest_path)
    if err:
        reporter.error(f"[shows.json] {err}")
        return None, {}

    if not isinstance(manifest_data, dict):
        reporter.error("[shows.json] Root structure must be a JSON object")
        return None, {}

    # Check top-level required fields
    required_top = ["schema_version", "site_title", "updated_at", "shows"]
    for field in required_top:
        if field not in manifest_data:
            reporter.error(f"[shows.json] Missing required top-level field: '{field}'")

    if manifest_data.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        reporter.error(
            f"[shows.json] schema_version must be '{EXPECTED_SCHEMA_VERSION}', "
            f"got {repr(manifest_data.get('schema_version'))}"
        )

    updated_at = manifest_data.get("updated_at")
    if not is_valid_iso8601_utc(updated_at):
        reporter.error(f"[shows.json] updated_at must be valid ISO-8601 UTC string, got {repr(updated_at)}")

    shows_list = manifest_data.get("shows")
    if not isinstance(shows_list, list):
        reporter.error("[shows.json] 'shows' must be a list of show objects")
        return manifest_data, {}

    shows_map: Dict[str, Dict[str, Any]] = {}
    show_required_fields = [
        "id",
        "title",
        "subtitle",
        "accent_color",
        "index_file",
        "episode_count",
        "latest_pub_date",
    ]

    for show in shows_list:
        if not isinstance(show, dict):
            reporter.error("[shows.json] Each item in 'shows' must be an object")
            continue
        show_id = show.get("id")
        if not show_id or not isinstance(show_id, str):
            reporter.error("[shows.json] Show entry missing valid string 'id'")
            continue

        for req in show_required_fields:
            if req not in show:
                reporter.error(f"[shows.json] Show '{show_id}' missing required field: '{req}'")

        if show_id not in configured_show_ids:
            reporter.error(
                f"[shows.json] Show id '{show_id}' not found in podcasts.json registered shows: {sorted(list(configured_show_ids))}"
            )

        if not is_int(show.get("episode_count")) or show.get("episode_count", 0) < 0:
            reporter.error(f"[shows.json] Show '{show_id}' episode_count must be a non-negative integer")

        latest_pub = show.get("latest_pub_date")
        if show.get("episode_count", 0) > 0:
            if not is_valid_iso8601_utc(latest_pub):
                reporter.error(
                    f"[shows.json] Show '{show_id}' latest_pub_date must be valid ISO-8601 UTC string, got {repr(latest_pub)}"
                )
        else:
            if latest_pub != "":
                reporter.error(
                    f"[shows.json] Show '{show_id}' latest_pub_date must be empty string when episode_count is 0"
                )

        expected_index_file = f"{show_id}/index.json"
        if show.get("index_file") != expected_index_file:
            reporter.error(
                f"[shows.json] Show '{show_id}' index_file expected '{expected_index_file}', got {repr(show.get('index_file'))}"
            )

        shows_map[show_id] = show

    return manifest_data, shows_map


def validate_show_catalog_and_episodes(
    show_id: str,
    manifest_show_entry: Optional[Dict[str, Any]],
    reporter: ValidationReporter,
) -> None:
    """Validate docs/data/<show_id>/index.json and all referenced episodes."""
    show_dir = DOCS_DATA_DIR / show_id
    if not show_dir.exists() or not show_dir.is_dir():
        reporter.error(f"[{show_id}] Directory not found: {show_dir}")
        return

    index_path = show_dir / "index.json"
    index_data, err = load_json_file(index_path)
    if err:
        reporter.error(f"[{show_id}/index.json] {err}")
        return

    if not isinstance(index_data, dict):
        reporter.error(f"[{show_id}/index.json] Root structure must be a JSON object")
        return

    # 1. Top-level catalog fields
    required_index_fields = [
        "schema_version",
        "show_id",
        "podcast_title",
        "rss_url",
        "updated_at",
        "episodes",
    ]
    for req in required_index_fields:
        if req not in index_data:
            reporter.error(f"[{show_id}/index.json] Missing required field: '{req}'")

    if index_data.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        reporter.error(
            f"[{show_id}/index.json] schema_version must be '{EXPECTED_SCHEMA_VERSION}', "
            f"got {repr(index_data.get('schema_version'))}"
        )

    if index_data.get("show_id") != show_id:
        reporter.error(
            f"[{show_id}/index.json] show_id '{index_data.get('show_id')}' does not match directory '{show_id}'"
        )

    updated_at = index_data.get("updated_at")
    if not is_valid_iso8601_utc(updated_at):
        reporter.error(f"[{show_id}/index.json] updated_at must be valid ISO-8601 UTC string, got {repr(updated_at)}")

    episodes = index_data.get("episodes")
    if not isinstance(episodes, list):
        reporter.error(f"[{show_id}/index.json] 'episodes' must be a list")
        return

    # Check manifest consistency
    actual_episode_count = len(episodes)
    actual_latest_pub_date = episodes[0].get("pub_date", "") if actual_episode_count > 0 else ""

    if manifest_show_entry:
        manifest_count = manifest_show_entry.get("episode_count")
        manifest_latest_pub = manifest_show_entry.get("latest_pub_date")
        if manifest_count != actual_episode_count:
            reporter.error(
                f"[shows.json vs {show_id}/index.json] episode_count mismatch: shows.json has {manifest_count}, "
                f"index.json has {actual_episode_count}"
            )
        if manifest_latest_pub != actual_latest_pub_date:
            reporter.error(
                f"[shows.json vs {show_id}/index.json] latest_pub_date mismatch: shows.json has {repr(manifest_latest_pub)}, "
                f"index.json has {repr(actual_latest_pub_date)}"
            )

    # 2. Check episodes in index.json: pub_date ordering, duplicates, prohibited keys
    seen_guids: Set[str] = set()
    referenced_files: Set[str] = set()
    prev_pub_date: Optional[str] = None

    required_ep_meta_fields = [
        "guid",
        "title",
        "pub_date",
        "duration_sec",
        "audio_url",
        "file",
        "summary_zh",
        "key_takeaways_zh",
        "highlights_count",
        "transcript_count",
    ]

    for idx, ep_meta in enumerate(episodes):
        reporter.episodes_checked_count += 1
        if not isinstance(ep_meta, dict):
            reporter.error(f"[{show_id}/index.json] Episode #{idx} is not an object")
            continue

        guid = ep_meta.get("guid")
        if not guid or not isinstance(guid, str):
            reporter.error(f"[{show_id}/index.json] Episode #{idx} has missing or non-string 'guid'")
            continue

        ep_tag = f"[{show_id}][{guid}]"

        # Check required fields in index episode entry
        for req in required_ep_meta_fields:
            if req not in ep_meta:
                reporter.error(f"{ep_tag} index.json entry missing required field: '{req}'")

        # Hard prohibition: index.json must NEVER contain vocab_notes or references
        if "vocab_notes" in ep_meta:
            reporter.error(f"{ep_tag} index.json entry violates hard constraint: contains 'vocab_notes'")
        if "references" in ep_meta:
            reporter.error(f"{ep_tag} index.json entry violates hard constraint: contains 'references'")

        # Duplicate guid check
        if guid in seen_guids:
            reporter.error(f"{ep_tag} Duplicate guid detected in {show_id}/index.json")
        seen_guids.add(guid)

        # Strictly descending pub_date check
        pub_date = ep_meta.get("pub_date")
        if not is_valid_iso8601_utc(pub_date):
            reporter.error(f"{ep_tag} pub_date must be valid ISO-8601 UTC string, got {repr(pub_date)}")
        else:
            if prev_pub_date is not None and pub_date >= prev_pub_date:
                reporter.error(
                    f"{ep_tag} episodes not strictly sorted in descending pub_date order: "
                    f"'{pub_date}' occurs after '{prev_pub_date}'"
                )
            prev_pub_date = pub_date

        # File reference check
        ep_file_rel = ep_meta.get("file", "")
        if not ep_file_rel or not isinstance(ep_file_rel, str):
            reporter.error(f"{ep_tag} 'file' attribute must be a non-empty relative path string")
            continue

        referenced_files.add(os.path.basename(ep_file_rel))
        ep_full_path = show_dir / ep_file_rel

        if not ep_full_path.exists() or not ep_full_path.is_file():
            reporter.error(f"{ep_tag} Referenced episode file does not exist: {ep_full_path}")
            continue

        # 3. Validate Episode File Content
        validate_episode_file(
            show_id=show_id,
            guid=guid,
            ep_meta=ep_meta,
            ep_file_path=ep_full_path,
            reporter=reporter,
        )

    # 4. Check for orphan episode files in docs/data/<show_id>/episodes/
    episodes_dir = show_dir / "episodes"
    if episodes_dir.exists() and episodes_dir.is_dir():
        for disk_file in episodes_dir.iterdir():
            # Ignore smoke test files, temporary swap files, and hidden files
            if not disk_file.is_file():
                continue
            if disk_file.name == "_smoke_preview.json":
                continue
            if disk_file.name.endswith(".tmp") or disk_file.name.startswith("."):
                continue
            if disk_file.suffix == ".json":
                if disk_file.name not in referenced_files:
                    reporter.error(
                        f"[{show_id}] Orphan episode file detected not referenced in index.json: {disk_file}"
                    )


def validate_episode_file(
    show_id: str,
    guid: str,
    ep_meta: Dict[str, Any],
    ep_file_path: Path,
    reporter: ValidationReporter,
) -> None:
    """Validate single episode JSON file against schema and cross-check with index.json."""
    ep_tag = f"[{show_id}][{guid}]"

    # Filename match check
    expected_filename = make_safe_guid(guid) + ".json"
    if ep_file_path.name != expected_filename:
        reporter.error(
            f"{ep_tag} Episode filename mismatch: got '{ep_file_path.name}', "
            f"expected '{expected_filename}'"
        )

    ep_data, err = load_json_file(ep_file_path)
    if err:
        reporter.error(f"{ep_tag} {err}")
        return

    if not isinstance(ep_data, dict):
        reporter.error(f"{ep_tag} Episode file root structure must be a JSON object")
        return

    # Check schema_version and show_id
    if ep_data.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        reporter.error(
            f"{ep_tag} schema_version must be '{EXPECTED_SCHEMA_VERSION}', "
            f"got {repr(ep_data.get('schema_version'))}"
        )
    if "show_id" in ep_data and ep_data.get("show_id") != show_id:
        reporter.error(
            f"{ep_tag} show_id in episode file '{ep_data.get('show_id')}' does not match '{show_id}'"
        )

    # Cross-check field equality with index.json
    fields_to_match = [
        "guid",
        "title",
        "pub_date",
        "duration_sec",
        "audio_url",
        "summary_zh",
        "key_takeaways_zh",
    ]
    for field in fields_to_match:
        if ep_data.get(field) != ep_meta.get(field):
            reporter.error(
                f"{ep_tag} Inconsistency for '{field}': episode file has {repr(ep_data.get(field))}, "
                f"index.json has {repr(ep_meta.get(field))}"
            )

    # Check highlights_count and transcript_count
    highlights = ep_data.get("highlights")
    if not isinstance(highlights, list):
        reporter.error(f"{ep_tag} 'highlights' must be a list")
        highlights = []
    if len(highlights) != ep_meta.get("highlights_count"):
        reporter.error(
            f"{ep_tag} highlights_count mismatch: index.json has {ep_meta.get('highlights_count')}, "
            f"episode file has {len(highlights)}"
        )

    transcript = ep_data.get("transcript")
    if not isinstance(transcript, list):
        reporter.error(f"{ep_tag} 'transcript' must be a list")
        transcript = []
    if len(transcript) != ep_meta.get("transcript_count"):
        reporter.error(
            f"{ep_tag} transcript_count mismatch: index.json has {ep_meta.get('transcript_count')}, "
            f"episode file has {len(transcript)}"
        )

    # Check duration_sec
    duration_sec = ep_data.get("duration_sec")
    if not is_int(duration_sec) or duration_sec < 0:
        reporter.error(f"{ep_tag} duration_sec must be a non-negative integer, got {repr(duration_sec)}")
        duration_sec = 0  # fallback for bounds checks below

    # Soft bound: highlights count in [4, 10]
    if not (4 <= len(highlights) <= 10):
        reporter.warn(f"{ep_tag} highlights count {len(highlights)} is outside standard range [4, 10]")

    # Validate highlights
    prev_hl_sec: Optional[int] = None
    hl_required_fields = ["start_sec", "title_zh", "summary_zh", "quote_en"]
    for h_idx, hl in enumerate(highlights):
        if not isinstance(hl, dict):
            reporter.error(f"{ep_tag} Highlight #{h_idx} must be an object")
            continue
        for req in hl_required_fields:
            if req not in hl:
                reporter.error(f"{ep_tag} Highlight #{h_idx} missing required field '{req}'")

        sec = hl.get("start_sec")
        if not is_int(sec) or sec < 0 or sec > duration_sec:
            reporter.error(
                f"{ep_tag} Highlight #{h_idx} start_sec {repr(sec)} out of bounds [0, {duration_sec}]"
            )
        else:
            if prev_hl_sec is not None and sec < prev_hl_sec:
                reporter.error(
                    f"{ep_tag} highlights start_sec not monotonic non-decreasing: {sec} < {prev_hl_sec}"
                )
            prev_hl_sec = sec

    # Validate transcript
    prev_tr_sec: Optional[int] = None
    tr_required_fields = ["id", "start_sec", "speaker", "en", "zh"]
    has_speaker_prefix_warn = False

    for t_idx, seg in enumerate(transcript):
        expected_id = t_idx + 1
        if not isinstance(seg, dict):
            reporter.error(f"{ep_tag} Transcript segment #{t_idx} must be an object")
            continue
        for req in tr_required_fields:
            if req not in seg:
                reporter.error(f"{ep_tag} Transcript segment #{t_idx} missing required field '{req}'")

        # id must be 1-based and contiguously increasing
        seg_id = seg.get("id")
        if seg_id != expected_id:
            reporter.error(
                f"{ep_tag} Transcript segment #{t_idx} id expected {expected_id}, got {repr(seg_id)}"
            )

        # start_sec bounds and monotonic non-decreasing
        sec = seg.get("start_sec")
        if not is_int(sec) or sec < 0 or sec > duration_sec:
            reporter.error(
                f"{ep_tag} Transcript segment #{t_idx} start_sec {repr(sec)} out of bounds [0, {duration_sec}]"
            )
        else:
            if prev_tr_sec is not None and sec < prev_tr_sec:
                reporter.error(
                    f"{ep_tag} transcript start_sec not monotonic non-decreasing: {sec} < {prev_tr_sec}"
                )
            prev_tr_sec = sec

        # Speaker normalization: check for residual stripped prefixes
        spk = seg.get("speaker", "")
        if isinstance(spk, str) and TITLE_PREFIX_PATTERN.search(spk):
            if not has_speaker_prefix_warn:
                reporter.warn(f"{ep_tag} Speaker has residual title prefix: {repr(spk)}")
                has_speaker_prefix_warn = True

    # Validate vocab_notes if present
    if "vocab_notes" in ep_data:
        vn_list = ep_data["vocab_notes"]
        if not isinstance(vn_list, list):
            reporter.error(f"{ep_tag} 'vocab_notes' must be a list")
        else:
            # Soft bound: vocab_notes count in [6, 10]
            if not (6 <= len(vn_list) <= 10):
                reporter.warn(
                    f"{ep_tag} vocab_notes count {len(vn_list)} is outside recommended range [6, 10]"
                )

            vn_required = ["term", "meaning_zh", "note_zh", "start_sec"]
            prev_vn_sec: Optional[int] = None
            vn_sorted = True
            for v_idx, vn in enumerate(vn_list):
                if not isinstance(vn, dict):
                    reporter.error(f"{ep_tag} vocab_notes #{v_idx} must be an object")
                    continue
                for req in vn_required:
                    if req not in vn:
                        reporter.error(f"{ep_tag} vocab_notes #{v_idx} missing required field '{req}'")

                sec = vn.get("start_sec")
                if not is_int(sec) or sec < 0 or sec > duration_sec:
                    reporter.error(
                        f"{ep_tag} vocab_notes #{v_idx} start_sec {repr(sec)} out of bounds [0, {duration_sec}]"
                    )
                else:
                    if prev_vn_sec is not None and sec < prev_vn_sec:
                        vn_sorted = False
                    prev_vn_sec = sec

            if not vn_sorted:
                reporter.warn(f"{ep_tag} vocab_notes are not sorted in ascending start_sec order")

    # Validate references if present
    if "references" in ep_data:
        ref_list = ep_data["references"]
        if not isinstance(ref_list, list):
            reporter.error(f"{ep_tag} 'references' must be a list")
        else:
            # Soft bound: references count <= 10
            if len(ref_list) > 10:
                reporter.warn(f"{ep_tag} references count {len(ref_list)} exceeds maximum 10")

            ref_required = ["title", "creator", "category", "note_zh", "start_sec"]
            seen_none_sec = False
            ref_order_correct = True
            prev_ref_sec: Optional[int] = None

            for r_idx, ref in enumerate(ref_list):
                if not isinstance(ref, dict):
                    reporter.error(f"{ep_tag} references #{r_idx} must be an object")
                    continue
                for req in ref_required:
                    if req not in ref:
                        reporter.error(f"{ep_tag} references #{r_idx} missing required field '{req}'")

                cat = ref.get("category")
                if cat not in VALID_REFERENCE_CATEGORIES:
                    reporter.error(
                        f"{ep_tag} references #{r_idx} category '{cat}' invalid; must be one of {sorted(list(VALID_REFERENCE_CATEGORIES))}"
                    )

                sec = ref.get("start_sec")
                if sec is None:
                    seen_none_sec = True
                elif is_int(sec):
                    if sec < 0 or sec > duration_sec:
                        reporter.error(
                            f"{ep_tag} references #{r_idx} start_sec {repr(sec)} out of bounds [0, {duration_sec}]"
                        )
                    if seen_none_sec:
                        # None should stably be sorted at the end
                        ref_order_correct = False
                    if prev_ref_sec is not None and sec < prev_ref_sec:
                        ref_order_correct = False
                    prev_ref_sec = sec
                else:
                    reporter.error(
                        f"{ep_tag} references #{r_idx} start_sec must be non-negative integer or null, got {repr(sec)}"
                    )

            if not ref_order_correct:
                ref_secs = [r.get("start_sec") for r in ref_list if isinstance(r, dict)]
                reporter.warn(
                    f"{ep_tag} references start_sec not sorted in ascending order (with null at end): {ref_secs}"
                )


def run_validation(target_show: str = "all", strict: bool = False) -> int:
    """Run data contract validation across manifests, catalogs, and episodes."""
    reporter = ValidationReporter(strict=strict)

    print("=" * 68)
    print("           AIRSHOW-READER DATA CONTRACT VALIDATOR (v3.0 / v3.1)       ")
    print("=" * 68)
    print(f"Target show:  {target_show}")
    print(f"Strict mode:  {strict}")
    print("-" * 68)

    # 1. Load registered shows from podcasts.json
    podcasts_cfg, err = load_podcasts_config()
    if err:
        reporter.error(f"[podcasts.json] {err}")
        print(f"FATAL: {err}", file=sys.stderr)
        return 1

    configured_shows = podcasts_cfg.get("shows", [])
    configured_show_ids = {s.get("id") for s in configured_shows if s.get("id")}

    if target_show != "all" and target_show not in configured_show_ids:
        reporter.error(
            f"Unknown show_id: '{target_show}'. Valid configured shows: {sorted(list(configured_show_ids))}"
        )
        print(f"ERROR: Unknown show_id: '{target_show}'. Valid shows: {sorted(list(configured_show_ids))}")
        return 1

    # 2. Validate docs/data/shows.json manifest
    manifest_data, manifest_shows_map = validate_shows_manifest(
        reporter=reporter,
        configured_show_ids=configured_show_ids,
        target_show=target_show,
    )

    # 3. Determine shows to inspect
    if target_show == "all":
        shows_to_validate = sorted(list(configured_show_ids))
    else:
        shows_to_validate = [target_show]

    reporter.shows_checked = shows_to_validate

    # 4. Validate each show's index.json and episodes
    for sid in shows_to_validate:
        manifest_entry = manifest_shows_map.get(sid)
        validate_show_catalog_and_episodes(
            show_id=sid,
            manifest_show_entry=manifest_entry,
            reporter=reporter,
        )

    # 5. Output Summary Report
    print("-" * 68)
    print("VALIDATION SUMMARY:")
    print(f"  Shows inspected:    {len(reporter.shows_checked)} ({', '.join(reporter.shows_checked)})")
    print(f"  Episodes checked:   {reporter.episodes_checked_count}")
    print(f"  Total ERRORS:       {len(reporter.errors)}")
    print(f"  Total WARNINGS:     {len(reporter.warnings)}")
    print("-" * 68)

    if reporter.warnings:
        print(f"WARNINGS ({len(reporter.warnings)}):")
        for idx, w in enumerate(reporter.warnings, 1):
            print(f"  {idx}. [WARN] {w}")
    else:
        print("WARNINGS: 0 (No soft-boundary anomalies found)")

    print()
    if reporter.errors:
        print(f"ERRORS ({len(reporter.errors)}):")
        for idx, e in enumerate(reporter.errors, 1):
            print(f"  {idx}. [ERROR] {e}")
    else:
        print("ERRORS: 0 (No hard contract violations)")

    print("=" * 68)
    if reporter.has_failures:
        if reporter.errors:
            print("RESULT: FAILED (Contract hard violations detected)")
        else:
            print("RESULT: FAILED (--strict mode: warnings treated as fatal errors)")
        return 1
    else:
        if reporter.warnings:
            print("RESULT: PASSED (with historical warnings tolerated)")
        else:
            print("RESULT: PASSED (100% clean)")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate airshow-reader v3.0 / v3.1 data contract and integrity."
    )
    parser.add_argument(
        "--show",
        type=str,
        default="all",
        help="Target show ID to validate, or 'all' to validate all configured shows (default: 'all')",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=False,
        help="Strict mode: elevate all WARNs (soft boundaries) to fatal ERRORs (exit code 1)",
    )
    args = parser.parse_args()

    exit_code = run_validation(target_show=args.show, strict=args.strict)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
