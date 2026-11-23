#!/usr/bin/env python3
"""Podcast bilingual reading processor (v3.0).

CLI entrypoint for multi-podcast configuration-driven engine.
Ensures sys.path robustness, parses CLI arguments, and delegates
execution to pipeline modules.
"""

import argparse
from pathlib import Path
import sys

# Guarantee scripts/ directory is in sys.path
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pipeline.config import (
    SHOWS_MANIFEST_FILE,
    ensure_tools_in_path,
    get_env_stripped,
    load_podcasts_config,
    log,
)
from pipeline.migrate import migrate_to_v3_if_needed
from pipeline.runner import (
    run_enrichment_pipeline,
    run_main_pipeline,
)


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

    total_episodes_attempted, total_episodes_failed = run_main_pipeline(args, config, target_shows)

    if total_episodes_attempted > 0 and total_episodes_failed == total_episodes_attempted:
        log("ERROR", f"All {total_episodes_attempted} attempted episode(s) failed in this run.")
        sys.exit(1)

    if total_episodes_attempted == 0 and args.show != "all":
        log("FILTER", f"No new episodes to process for show '{args.show}'. Exiting cleanly.", show_id=args.show)


if __name__ == "__main__":
    main()
