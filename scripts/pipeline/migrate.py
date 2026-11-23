"""v2 -> v3 idempotent migration and empty show initialization."""

from datetime import datetime, timezone
import json
import os
import shutil
import sys

from pipeline.config import (
    DOCS_DATA_DIR,
    GLOSSARIES_DIR,
    LEGACY_DATA_DIR,
    LEGACY_DOCS_EPISODES_FILE,
    LEGACY_EPISODES_FILE,
    LEGACY_GLOSSARY_FILE,
    LEGACY_V2_EPISODES_DIR,
    LEGACY_V2_INDEX_FILE,
    load_podcasts_config,
    log,
)
from pipeline.storage import make_safe_guid, sync_shows_manifest


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
