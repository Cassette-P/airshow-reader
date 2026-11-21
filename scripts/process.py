#!/usr/bin/env python3
"""airshow-reader: Podcast bilingual reading processor (v2.0).

Implements the state machine defined in SPEC.md:
[0. MIGRATE_IF_NEEDED] -> [1. FETCH_RSS] -> [2. FILTER_NEW] -> [3. DOWNLOAD_AUDIO] -> [4. CHUNK_AUDIO]
-> [5. CHANNEL_B] -> [6. CHANNEL_A] -> [7. ATOMIC_SAVE] -> [8. CLEANUP]
"""

import argparse
import calendar
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

import feedparser
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
import requests

# Constants & Paths (docs/data/ as single source of truth)
RSS_URL = "https://feeds.buzzsprout.com/2329718.rss"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCS_DATA_DIR = PROJECT_ROOT / "docs" / "data"
INDEX_FILE = DOCS_DATA_DIR / "index.json"
EPISODES_DIR = DOCS_DATA_DIR / "episodes"
SMOKE_PREVIEW_FILE = DOCS_DATA_DIR / "_smoke_preview.json"
GLOSSARY_FILE = PROJECT_ROOT / "GLOSSARY.md"

# Legacy v1 paths (only referenced for idempotent migration check & cleanup)
LEGACY_DATA_DIR = PROJECT_ROOT / "data"
LEGACY_EPISODES_FILE = LEGACY_DATA_DIR / "episodes.json"
LEGACY_DOCS_EPISODES_FILE = DOCS_DATA_DIR / "episodes.json"

MODEL_USAGE_STATS: dict[str, dict[str, int]] = {
    "CH-B": {},
    "CH-A": {},
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


# --- Pydantic Structured Output Schemas ---


class TranscriptSegment(BaseModel):
    timestamp: str = Field(
        description="Segment start timestamp strictly formatted as 'MM:SS' (e.g. '00:02', '01:06', '02:55') relative to the beginning of this audio chunk. Colon must NOT be omitted."
    )
    speaker: str = Field(
        description="Speaker full name normalized according to glossary: 'Jon Ostrower', 'Brian Sumers', 'Brett Snyder', or guest name ('Guest' if unknown)"
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


class ChannelAOutput(BaseModel):
    summary_zh: str = Field(
        description="200-350 character Chinese core summary covering main issues, perspectives, and conclusions"
    )
    key_takeaways_zh: list[str] = Field(
        description="3-5 key takeaways as one-sentence conclusions in Chinese"
    )
    highlights: list[HighlightItem] = Field(
        description="4-8 highlights chronologically ordered"
    )


# --- Observability & Utilities ---


def log(stage: str, message: str) -> None:
    """Print formatted log message conforming to [AIRSHOW][STAGE] standard."""
    print(f"[AIRSHOW][{stage}] {message}", flush=True)


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


def load_glossary() -> str:
    """Load GLOSSARY.md from project root."""
    if not GLOSSARY_FILE.exists():
        log("ERROR", f"GLOSSARY.md not found at {GLOSSARY_FILE}")
        sys.exit(1)
    with open(GLOSSARY_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    return content


def migrate_v1_to_v2_if_needed() -> bool:
    """[0. MIGRATE_IF_NEEDED] Migrate legacy v1 episodes.json to v2.0 split architecture if needed.

    Returns True if migration was performed, False if already up to date or nothing to migrate.
    """
    if INDEX_FILE.exists():
        log("MIGRATE", f"v2.0 index file already exists at {INDEX_FILE}. Migration not required.")
        return False

    legacy_file = None
    if LEGACY_DOCS_EPISODES_FILE.exists():
        legacy_file = LEGACY_DOCS_EPISODES_FILE
    elif LEGACY_EPISODES_FILE.exists():
        legacy_file = LEGACY_EPISODES_FILE

    if not legacy_file:
        log("MIGRATE", "No legacy v1 episodes.json found and no v2 index.json exists. Clean state.")
        return False

    log("MIGRATE", f"Starting v1->v2 migration from legacy file: {legacy_file}...")
    try:
        with open(legacy_file, "r", encoding="utf-8") as f:
            v1_data = json.load(f)
    except Exception as e:
        log("ERROR", f"Failed to read legacy episodes file {legacy_file}: {e}")
        sys.exit(1)

    v1_episodes = v1_data.get("episodes", [])
    log("MIGRATE", f"Found {len(v1_episodes)} episode(s) to migrate.")

    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DATA_DIR.mkdir(parents=True, exist_ok=True)

    index_episodes = []
    written_files: list[Path] = []

    for ep in v1_episodes:
        guid = ep.get("guid", "")
        if not guid:
            log("ERROR", f"Encountered episode without guid during migration: {ep.get('title', '')}")
            continue
        safe_guid = make_safe_guid(guid)
        ep_file_rel = f"episodes/{safe_guid}.json"
        ep_file_abs = DOCS_DATA_DIR / ep_file_rel

        # Single episode v2 document
        ep_v2 = {
            "schema_version": "2.0",
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

        # Atomic write for single episode (.tmp -> os.replace)
        tmp_ep_file = ep_file_abs.with_suffix(".json.tmp")
        with open(tmp_ep_file, "w", encoding="utf-8") as f:
            json.dump(ep_v2, f, ensure_ascii=False, indent=2)
        os.replace(tmp_ep_file, ep_file_abs)
        written_files.append(ep_file_abs)

        # Index entry
        index_episodes.append({
            "guid": guid,
            "title": ep.get("title", ""),
            "pub_date": ep.get("pub_date", ""),
            "duration_sec": int(ep.get("duration_sec", 0)),
            "audio_url": ep.get("audio_url", ""),
            "file": ep_file_rel,
            "summary_zh": ep.get("summary_zh", ""),
            "key_takeaways_zh": ep.get("key_takeaways_zh", []),
            "highlights_count": len(ep.get("highlights", [])),
            "transcript_count": len(ep.get("transcript", [])),
        })

    # Sort index episodes by pub_date descending
    index_episodes.sort(key=lambda x: x.get("pub_date", ""), reverse=True)

    index_v2 = {
        "schema_version": "2.0",
        "podcast_title": v1_data.get("podcast_title", "The Air Show"),
        "rss_url": v1_data.get("rss_url", RSS_URL),
        "updated_at": v1_data.get("updated_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        "episodes": index_episodes,
    }

    tmp_index_file = INDEX_FILE.with_suffix(".json.tmp")
    with open(tmp_index_file, "w", encoding="utf-8") as f:
        json.dump(index_v2, f, ensure_ascii=False, indent=2)
    os.replace(tmp_index_file, INDEX_FILE)
    written_files.append(INDEX_FILE)

    # Verification of written files
    for p in written_files:
        if not p.exists() or p.stat().st_size == 0:
            log("ERROR", f"Migration verification failed: {p} is missing or empty.")
            sys.exit(1)

    log("MIGRATE", f"Verified {len(written_files)} files written successfully.")

    # Remove legacy files
    for legacy_path in [LEGACY_DOCS_EPISODES_FILE, LEGACY_EPISODES_FILE]:
        if legacy_path.exists():
            legacy_path.unlink()
            log("MIGRATE", f"Removed legacy file: {legacy_path}")

    # Remove legacy data/ directory if empty
    if LEGACY_DATA_DIR.exists():
        try:
            if not any(LEGACY_DATA_DIR.iterdir()):
                LEGACY_DATA_DIR.rmdir()
                log("MIGRATE", f"Removed empty legacy directory: {LEGACY_DATA_DIR}")
        except Exception:
            pass

    log("MIGRATE", f"v1->v2 migration completed successfully. {len(index_episodes)} episode(s) migrated to {EPISODES_DIR} and {INDEX_FILE}.")
    return True


def fetch_rss(url: str = RSS_URL):
    """[1. FETCH_RSS] 抓取 RSS feed."""
    log("RSS", f"Fetching RSS feed from {url}...")
    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log("ERROR", f"Failed to fetch RSS: {e}")
        sys.exit(1)

    feed = feedparser.parse(resp.content)
    if feed.bozo and not feed.entries:
        log("ERROR", f"Failed to parse RSS feed: {feed.get('bozo_exception', 'Unknown error')}")
        sys.exit(1)

    log("RSS", f"Successfully fetched feed with {len(feed.entries)} episode(s).")
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
        if href and ("audio" in enc_type or href.endswith(".mp3")):
            return href
    for link in entry.get("links", []):
        href = link.get("href")
        link_type = link.get("type", "")
        if href and ("audio" in link_type or href.endswith(".mp3")):
            return href
    if entry.get("enclosures"):
        return entry["enclosures"][0].get("href", "")
    return ""


def filter_new_episodes(feed_entries, limit: int = 1, max_age_days: int = 0) -> list:
    """[2. FILTER_NEW] 对比 docs/data/index.json 已有 guid，筛选最新未处理的 N 集（--limit），可选按发布时间过滤（--max-age-days）."""
    log("FILTER", "Checking existing episodes in docs/data/index.json...")
    existing_guids = set()
    if INDEX_FILE.exists():
        try:
            with open(INDEX_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                existing_guids = {ep["guid"] for ep in data.get("episodes", []) if "guid" in ep}
        except Exception as e:
            log("ERROR", f"Failed to read existing {INDEX_FILE}: {e}")
            sys.exit(1)

    new_entries = [e for e in feed_entries if get_entry_guid(e) not in existing_guids]
    log("FILTER", f"Found {len(new_entries)} unhandled episode(s) (existing in DB: {len(existing_guids)}).")

    if not new_entries:
        log("FILTER", "No new episodes to process. Exiting cleanly.")
        sys.exit(0)

    if max_age_days > 0:
        now_utc = datetime.now(timezone.utc)
        cutoff_dt = now_utc - timedelta(days=max_age_days)
        log("FILTER", f"Applying max-age-days filter: {max_age_days} day(s) (cutoff UTC: {cutoff_dt.strftime('%Y-%m-%dT%H:%M:%SZ')})...")
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
                log("FILTER", f"Filtered out legacy episode '{e.get('title', 'Unknown')}' ({get_entry_guid(e)}, published {pub_date_str}) older than {max_age_days} day(s).")

        new_entries = filtered_entries
        log("FILTER", f"{len(new_entries)} unhandled episode(s) remaining within the {max_age_days}-day limit.")

        if not new_entries:
            log("FILTER", f"No unhandled episodes match the criteria (within {max_age_days} day(s)). Exiting cleanly.")
            sys.exit(0)

    selected = new_entries[:limit]
    log("FILTER", f"Selected {len(selected)} episode(s) to process (limit: {limit}).")
    return selected


def download_audio_file(audio_url: str, dest_path: Path) -> int:
    """[3. DOWNLOAD_AUDIO] 流式下载 MP3 至系统临时目录（四级策略自动降级）."""
    log("DOWNLOAD", f"Downloading audio from {audio_url}...")

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
        with requests.get(audio_url, headers=headers, stream=True, timeout=120) as resp:
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
        with requests.get(audio_url, headers=headers, stream=True, timeout=120) as resp:
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
            timeout=120,
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
                "120",
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
        log("DOWNLOAD", f"Trying download strategy: {name}...")
        try:
            runner()
            if dest_path.exists() and dest_path.stat().st_size > 0:
                downloaded = dest_path.stat().st_size
                size_mb = downloaded / (1024 * 1024)
                log(
                    "DOWNLOAD",
                    f"Audio downloaded successfully via {name} ({size_mb:.2f} MB, {downloaded} bytes).",
                )
                return downloaded
            else:
                raise RuntimeError(
                    f"{name} completed but output file is missing or empty."
                )
        except Exception as e:
            last_exception = e
            log("DOWNLOAD", f"{name} failed: {e}")
            _cleanup_partial()

    log("ERROR", f"Audio download failed across all strategies: {last_exception}")
    if last_exception:
        raise last_exception
    raise RuntimeError("Audio download failed across all strategies.")


def get_audio_duration_sec(audio_path: Path) -> int:
    """ffprobe 获取 duration_sec (整数秒)."""
    log("DOWNLOAD", "Probing audio duration with ffprobe...")
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
            log("DOWNLOAD", f"Audio duration via ffprobe: {dur}s ({dur // 60}m {dur % 60}s).")
            return dur
    except Exception as e:
        log("ERROR", f"ffprobe format duration check failed: {e}")

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
        log("DOWNLOAD", f"Audio duration via stream ffprobe: {dur}s ({dur // 60}m {dur % 60}s).")
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


def chunk_audio(audio_path: Path, output_dir: Path, chunk_sec: int) -> list[Path]:
    """[4. CHUNK_AUDIO] ffmpeg 转为单声道 64k mp3 并按 --chunk-sec（默认 180s）切片."""
    output_pattern = output_dir / "chunk_%03d.mp3"
    log("FFMPEG", f"Transcoding and slicing audio: mono, 64kbps, segment_time={chunk_sec}s...")
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
        log("ERROR", f"ffmpeg audio slicing failed: {e.stderr}")
        raise

    chunks = sorted(output_dir.glob("chunk_*.mp3"))
    log("FFMPEG", f"Audio slicing completed. Generated {len(chunks)} chunk(s).")
    return chunks


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
) -> BaseModel:
    """Execute structured generation using streaming response with 5-retry backoff across model_pool."""
    backoff_delays = [15, 20, 25, 30]
    last_exception = None

    for m_idx, current_model in enumerate(model_pool):
        log(stage, f"Using model candidate [{m_idx + 1}/{len(model_pool)}]: '{current_model}'...")
        skipped_404 = False
        current_thinking_level = thinking_level

        for attempt in range(1, max_retries + 1):
            try:
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
                        log("ERROR", f"[{stage}] Failed to instantiate ThinkingConfig({current_thinking_level}): {t_init_err}. Disabling thinking_config.")
                        current_thinking_level = None

                thinking_desc = f"thinking={current_thinking_level}" if current_thinking_level else "thinking=default"
                log(stage, f"Calling Gemini model '{current_model}' via streaming (attempt {attempt}/{max_retries}, {thinking_desc})...")

                try:
                    response = client.models.generate_content_stream(
                        model=current_model,
                        contents=contents,
                        config=types.GenerateContentConfig(**config_kwargs),
                    )
                except Exception as api_err:
                    if current_thinking_level:
                        err_lower = str(api_err).lower()
                        if any(kw in err_lower for kw in ["thinking", "unsupported", "unknown field", "invalid argument", "not supported"]):
                            log("ERROR", f"[{stage}] Model '{current_model}' does not support thinking_config ({api_err}). Retrying immediately without thinking_config...")
                            current_thinking_level = None
                            config_kwargs.pop("thinking_config", None)
                            response = client.models.generate_content_stream(
                                model=current_model,
                                contents=contents,
                                config=types.GenerateContentConfig(**config_kwargs),
                            )
                        else:
                            raise api_err
                    else:
                        raise api_err

                chunks_text = []
                for chunk in response:
                    if chunk.text:
                        chunks_text.append(chunk.text)
                full_text = "".join(chunks_text).strip()

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
                log(stage, f"Successfully received and parsed response using model '{current_model}' on attempt {attempt}.")
                if stage in MODEL_USAGE_STATS:
                    MODEL_USAGE_STATS[stage][current_model] = MODEL_USAGE_STATS[stage].get(current_model, 0) + 1
                return parsed

            except Exception as e:
                last_exception = e
                err_str = str(e)
                log("ERROR", f"[{stage}] Attempt {attempt}/{max_retries} with model '{current_model}' failed: {e}")

                is_404 = "404" in err_str or "NOT_FOUND" in err_str or "NotFound" in type(e).__name__
                if is_404:
                    log("ERROR", f"[{stage}] Model '{current_model}' returned 404/NOT_FOUND. Skipping model immediately without waiting.")
                    skipped_404 = True
                    break

                if attempt < max_retries:
                    delay = backoff_delays[attempt - 1] if (attempt - 1) < len(backoff_delays) else 30
                    log(stage, f"Retrying model '{current_model}' after {delay}s backoff (attempt {attempt}/{max_retries})...")
                    time.sleep(delay)
                else:
                    log("ERROR", f"[{stage}] All {max_retries} attempts exhausted for model '{current_model}'.")

        if m_idx < len(model_pool) - 1:
            if not skipped_404:
                log(stage, f"Model '{current_model}' exhausted. Sleeping 10s before switching to next candidate model...")
                time.sleep(10)

    if last_exception is not None:
        raise last_exception
    raise RuntimeError(f"All models in pool {model_pool} exhausted without returning a valid response.")


def run_channel_b(
    chunks: list[Path],
    glossary_text: str,
    duration_sec: int,
    max_chunks: int = 0,
) -> list[dict]:
    """[5. CHANNEL_B] 逐片上传 Gemini File API → 注入 GLOSSARY.md 转写+翻译 → 立即删除云端文件.

    受 --max-chunks 限制；遇 429/503 指数退避最多 5 次；片间休眠 CHUNK_SLEEP_SEC（默认 2.0s）。
    """
    api_key = get_env_stripped("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    default_ch_b_pool = ["gemini-3.8-flash", "gemini-4-hydrogen", "gemini-3.5-flash-lite"]
    env_ch_b = get_env_stripped("GEMINI_MODEL_CH_B") or get_env_stripped("GEMINI_MODEL")
    if env_ch_b:
        model_pool = [env_ch_b] + [m for m in default_ch_b_pool if m != env_ch_b]
    else:
        model_pool = list(default_ch_b_pool)
    log("CH-B", f"Channel B model pool configured: {model_pool}")

    thinking_level = get_env_stripped("GEMINI_THINKING_CH_B", "low")
    log("CH-B", f"Channel B thinking level configured: '{thinking_level}'")

    chunk_sleep_raw = get_env_stripped("CHUNK_SLEEP_SEC", "2.0")
    try:
        chunk_sleep_sec = float(chunk_sleep_raw)
    except ValueError:
        chunk_sleep_sec = 2.0
    log("CH-B", f"Channel B chunk sleep configured: {chunk_sleep_sec}s")

    chunks_to_process = chunks[:max_chunks] if max_chunks > 0 else chunks
    log("CH-B", f"Starting Channel B processing for {len(chunks_to_process)} chunk(s) (total chunks: {len(chunks)})...")

    system_instruction = (
        "You are an expert bilingual aviation podcast transcriptionist and translator.\n"
        "Your task is to transcribe and translate the provided podcast audio chunk into Chinese and English bilingual segments.\n\n"
        "Rules:\n"
        "1. Segment into natural semantic blocks of 2-4 sentences each.\n"
        "2. For each block, provide:\n"
        "   - timestamp: string timestamp relative to the beginning of this audio chunk, strictly formatted as 'MM:SS' (e.g., '00:02', '01:06', '02:55'). Never omit the colon.\n"
        "   - speaker: strictly normalized to the hosts' full English names: 'Jon Ostrower', 'Brian Sumers', 'Brett Snyder', or the guest's full name (use 'Guest' if unknown).\n"
        "   - en: the original English spoken text (2-4 sentences per block).\n"
        "   - zh: natural, fluent, professional aviation Chinese translation adhering strictly to the glossary.\n"
        "3. Adhere strictly to the following speaker and terminology glossary:\n\n"
        f"{glossary_text}"
    )

    all_raw_segments = []
    accumulated_chunk_start_sec = 0

    for idx, chunk_path in enumerate(chunks_to_process):
        log("CH-B", f"--- Processing chunk [{idx + 1}/{len(chunks_to_process)}]: {chunk_path.name} ---")
        chunk_dur = get_audio_duration_sec(chunk_path)
        log("CH-B", f"Chunk duration via ffprobe: {chunk_dur}s (accumulated base: {accumulated_chunk_start_sec}s).")

        log("CH-B", f"Uploading {chunk_path.name} to Gemini File API...")
        uploaded = None
        for up_attempt in range(1, 4):
            try:
                uploaded = client.files.upload(file=str(chunk_path))
                break
            except Exception as up_err:
                if up_attempt == 3:
                    raise
                log("CH-B", f"Upload attempt {up_attempt} failed: {up_err}. Retrying in 5s...")
                time.sleep(5)

        log("CH-B", f"Uploaded cloud file: {uploaded.name}. Polling for ACTIVE state...")
        try:
            file_info = client.files.get(name=uploaded.name)
            while not str(file_info.state).endswith("ACTIVE"):
                if str(file_info.state).endswith("FAILED"):
                    raise RuntimeError(f"Cloud file {uploaded.name} entered FAILED state.")
                time.sleep(2)
                file_info = client.files.get(name=uploaded.name)
            log("CH-B", f"Cloud file {uploaded.name} is ACTIVE.")

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
            )

            chunk_segments = parsed_resp.segments
            log("CH-B", f"Chunk [{idx + 1}/{len(chunks_to_process)}] produced {len(chunk_segments)} segment(s).")
            for seg in chunk_segments:
                offset = parse_mmss_to_sec(seg.timestamp, fallback_sec=0)
                clamped_offset = max(0, min(offset, chunk_dur))
                global_start = accumulated_chunk_start_sec + clamped_offset
                clamped_start = max(0, min(global_start, duration_sec))
                all_raw_segments.append({
                    "start_sec": clamped_start,
                    "speaker": seg.speaker.strip(),
                    "en": seg.en.strip(),
                    "zh": seg.zh.strip(),
                })

        finally:
            try:
                client.files.delete(name=uploaded.name)
                log("CH-B", f"Deleted cloud file {uploaded.name}.")
            except Exception as del_err:
                log("CH-B", f"Warning: Failed to delete cloud file {uploaded.name}: {del_err}")

        accumulated_chunk_start_sec += chunk_dur

        if idx < len(chunks_to_process) - 1:
            log("CH-B", f"Sleeping {chunk_sleep_sec}s between chunks as per rate-limiting policy...")
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
            "speaker": seg["speaker"],
            "en": seg["en"],
            "zh": seg["zh"],
        })

    log("CH-B", f"Channel B completed successfully. Total transcript items: {len(final_transcript)}.")
    return final_transcript


def run_channel_a(
    transcript: list[dict],
    glossary_text: str,
    title: str = "",
    duration_sec: int = 0,
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
    log("CH-A", f"Channel A model pool configured: {model_pool}")

    log("CH-A", f"Starting Channel A summarization for episode '{title}' (duration: {duration_sec}s)...")

    lines = [f"[{item['start_sec']}] {item['speaker']}: {item['en']}" for item in transcript]
    transcript_text = "\n".join(lines)

    system_instruction = (
        "You are an executive aviation intelligence analyst and podcast editor.\n"
        "Analyze the provided timestamped podcast transcript and produce a structured Chinese executive summary.\n\n"
        "Requirements:\n"
        "1. summary_zh: A comprehensive core summary in Chinese (200-350 Chinese characters) covering main issues, perspectives, disagreements, and industry conclusions.\n"
        "2. key_takeaways_zh: Exactly 3 to 5 key takeaways in Chinese, each being a clear, sharp one-sentence conclusion.\n"
        "3. highlights: 4 to 8 key highlights/chapters from the episode, sorted chronologically by start_sec. Each highlight must include:\n"
        "   - start_sec: integer seconds where this key topic begins (must correspond to actual transcript timestamps and be within [0, duration_sec]).\n"
        "   - title_zh: concise Chinese topic title (strictly 15 Chinese characters or fewer).\n"
        "   - summary_zh: 2-3 sentence Chinese explanation of key arguments or data points.\n"
        "   - quote_en: short direct English quote from the speaker in that segment.\n\n"
        "Terminology and Glossary Reference:\n"
        f"{glossary_text}"
    )

    user_prompt = (
        f"Podcast Episode Title: {title}\n"
        f"Total Duration (seconds): {duration_sec}\n\n"
        f"Timestamped Transcript:\n"
        f"{transcript_text}\n\n"
        "Please provide the structured summary_zh, key_takeaways_zh, and highlights according to the schema."
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
    if len(highlights) > 8:
        highlights = highlights[:8]

    key_takeaways = [k.strip() for k in parsed_resp.key_takeaways_zh]
    if len(key_takeaways) > 5:
        key_takeaways = key_takeaways[:5]

    result = {
        "summary_zh": parsed_resp.summary_zh.strip(),
        "key_takeaways_zh": key_takeaways,
        "highlights": highlights,
    }

    log(
        "CH-A",
        f"Channel A completed. Summary length: {len(result['summary_zh'])} chars, "
        f"Key takeaways: {len(result['key_takeaways_zh'])}, Highlights: {len(result['highlights'])}.",
    )
    return result


def atomic_save(new_episodes: list[dict]) -> None:
    """[7. ATOMIC_SAVE] 原子写入 docs/data/episodes/<safe_guid>.json，再更新并按 pub_date 降序原子写入 docs/data/index.json."""
    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DATA_DIR.mkdir(parents=True, exist_ok=True)

    for ep in new_episodes:
        guid = ep["guid"]
        safe_guid = make_safe_guid(guid)
        ep_file = EPISODES_DIR / f"{safe_guid}.json"
        ep_data = {
            "schema_version": "2.0",
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
        tmp_ep_file = ep_file.with_suffix(".json.tmp")
        with open(tmp_ep_file, "w", encoding="utf-8") as f:
            json.dump(ep_data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_ep_file, ep_file)

    index_data = {
        "schema_version": "2.0",
        "podcast_title": "The Air Show",
        "rss_url": RSS_URL,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "episodes": [],
    }
    if INDEX_FILE.exists():
        try:
            with open(INDEX_FILE, "r", encoding="utf-8") as f:
                index_data = json.load(f)
        except Exception as e:
            log("ERROR", f"Failed to read existing {INDEX_FILE}: {e}")
            sys.exit(1)

    existing_map = {ep["guid"]: ep for ep in index_data.get("episodes", []) if "guid" in ep}
    for ep in new_episodes:
        guid = ep["guid"]
        safe_guid = make_safe_guid(guid)
        existing_map[guid] = {
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
        }

    sorted_episodes = sorted(
        existing_map.values(),
        key=lambda x: x.get("pub_date", ""),
        reverse=True,
    )

    index_data["schema_version"] = "2.0"
    index_data["podcast_title"] = "The Air Show"
    index_data["rss_url"] = RSS_URL
    index_data["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    index_data["episodes"] = sorted_episodes

    tmp_index_file = INDEX_FILE.with_suffix(".json.tmp")
    with open(tmp_index_file, "w", encoding="utf-8") as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_index_file, INDEX_FILE)

    log("SAVE", f"Saved {len(new_episodes)} episode(s) to {EPISODES_DIR} and updated {INDEX_FILE} (total {len(sorted_episodes)} episodes).")


def process_episode(entry, args, glossary_text: str) -> None:
    """Process a single podcast episode through the pipeline."""
    guid = get_entry_guid(entry)
    title = entry.get("title", "Untitled Episode")
    pub_date = get_entry_pub_date(entry)
    audio_url = get_entry_audio_url(entry)

    if not audio_url:
        log("ERROR", f"Episode '{title}' ({guid}) has no valid audio URL.")
        raise ValueError(f"Episode '{title}' ({guid}) has no valid audio URL.")

    temp_dir = None
    try:
        temp_dir = Path(tempfile.mkdtemp(prefix="airshow_"))

        # [3. DOWNLOAD_AUDIO]
        original_audio = temp_dir / "original.mp3"
        download_audio_file(audio_url, original_audio)
        duration_sec = get_audio_duration_sec(original_audio)

        # [4. CHUNK_AUDIO]
        chunks = chunk_audio(original_audio, temp_dir, args.chunk_sec)

        # Log chunk details
        for chunk in chunks:
            size_bytes = chunk.stat().st_size
            size_mb = size_bytes / (1024 * 1024)
            log("FFMPEG", f"  - {chunk.name}: {size_mb:.2f} MB ({size_bytes} bytes)")

        # Print episode summary
        log("FFMPEG", "[EPISODE_SUMMARY]")
        log("FFMPEG", f"  guid: {guid}")
        log("FFMPEG", f"  title: {title}")
        log("FFMPEG", f"  pub_date: {pub_date}")
        log("FFMPEG", f"  duration_sec: {duration_sec}")
        log("FFMPEG", f"  audio_url: {audio_url}")
        log("FFMPEG", f"  chunks_count: {len(chunks)}")

        if args.dry_run:
            log("FFMPEG", "[DRY-RUN] Dry run enabled. Slicing verified. Skipping Gemini processing and atomic save.")
            return

        # [5. CHANNEL_B]
        log("CH-B", "Calling Channel B for transcription and translation...")
        transcript = run_channel_b(
            chunks=chunks,
            glossary_text=glossary_text,
            duration_sec=duration_sec,
            max_chunks=args.max_chunks,
        )

        # [6. CHANNEL_A]
        log("CH-A", "Calling Channel A for summarization and highlights...")
        summary_info = run_channel_a(
            transcript=transcript,
            glossary_text=glossary_text,
            title=title,
            duration_sec=duration_sec,
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
            "transcript": transcript,
        }

        if args.max_chunks > 0:
            DOCS_DATA_DIR.mkdir(parents=True, exist_ok=True)
            preview_data = {
                "schema_version": "2.0",
                "podcast_title": "The Air Show",
                "rss_url": RSS_URL,
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "episodes": [episode_record],
            }
            tmp_smoke = SMOKE_PREVIEW_FILE.with_suffix(".json.tmp")
            with open(tmp_smoke, "w", encoding="utf-8") as f:
                json.dump(preview_data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_smoke, SMOKE_PREVIEW_FILE)
            log(
                "SAVE",
                f"[SMOKE] Smoke preview written to {SMOKE_PREVIEW_FILE}. "
                f"Neither {INDEX_FILE} nor {EPISODES_DIR} was modified.",
            )
        else:
            log("SAVE", "Calling atomic save...")
            if SMOKE_PREVIEW_FILE.exists():
                try:
                    SMOKE_PREVIEW_FILE.unlink()
                    log("SAVE", f"Removed temporary smoke preview file: {SMOKE_PREVIEW_FILE}")
                except Exception:
                    pass
            atomic_save([episode_record])
            log("SAVE", f"Model usage statistics: {MODEL_USAGE_STATS}")

    finally:
        # [8. CLEANUP]
        if temp_dir and temp_dir.exists():
            log("CLEANUP", f"Cleaning up temporary directory: {temp_dir}")
            shutil.rmtree(temp_dir, ignore_errors=True)
            log("CLEANUP", "Temporary directory cleaned up successfully.")


def main():
    parser = argparse.ArgumentParser(description="airshow-reader: Podcast bilingual reading processor (v2.0).")
    parser.add_argument("--limit", type=int, default=1, help="单次最多处理的新单集数量 (default: 1)")
    parser.add_argument("--max-age-days", type=int, default=0, help="仅处理发布时间距当前 UTC 不超过 N 天的单集（0 表示不限制，default: 0）")
    parser.add_argument("--chunk-sec", type=int, default=180, help="音频切片时长（秒） (default: 180)")
    parser.add_argument("--dry-run", action="store_true", default=False, help="演习模式，不调用 Gemini API、不修改 JSON")
    parser.add_argument("--max-chunks", type=int, default=0, help="仅处理前 M 个切片（0 表示全部，default: 0）")
    parser.add_argument("--migrate-only", action="store_true", default=False, help="仅执行 v1→v2 本地数据拆分迁移，不访问网络、不调用 Gemini API，完成后退出 0")
    args = parser.parse_args()

    # [0. MIGRATE_IF_NEEDED]
    if args.migrate_only:
        log("MIGRATE", "Running in --migrate-only mode...")
        migrated = migrate_v1_to_v2_if_needed()
        if migrated:
            log("MIGRATE", "Migration completed successfully. Exiting 0 as requested by --migrate-only.")
        else:
            log("MIGRATE", "Data already in v2 format or nothing to migrate. Exiting 0.")
        sys.exit(0)

    # Automatic migration if index.json does not exist yet but legacy files exist
    if not INDEX_FILE.exists() and (LEGACY_DOCS_EPISODES_FILE.exists() or LEGACY_EPISODES_FILE.exists()):
        migrate_v1_to_v2_if_needed()

    if not args.dry_run:
        api_key = get_env_stripped("GEMINI_API_KEY")
        if not api_key:
            log("ERROR", "GEMINI_API_KEY environment variable is required when not in --dry-run mode.")
            sys.exit(1)

    ensure_tools_in_path()

    glossary_text = load_glossary()

    # [1. FETCH_RSS]
    feed = fetch_rss(RSS_URL)

    # [2. FILTER_NEW]
    episodes_to_process = filter_new_episodes(feed.entries, limit=args.limit, max_age_days=args.max_age_days)

    log("FILTER", f"Loaded GLOSSARY.md successfully ({len(glossary_text)} characters).")

    total_episodes = len(episodes_to_process)
    failed_count = 0

    for entry in episodes_to_process:
        try:
            process_episode(entry, args, glossary_text)
        except Exception as e:
            failed_count += 1
            log("ERROR", f"Episode '{entry.get('title', 'Unknown')}' processing failed: {e}")

    if total_episodes > 0 and failed_count == total_episodes:
        log("ERROR", f"All {total_episodes} episode(s) failed to process in this run.")
        sys.exit(1)


if __name__ == "__main__":
    main()
