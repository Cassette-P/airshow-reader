#!/usr/bin/env python3
"""airshow-reader: Podcast bilingual reading processor.

Implements the state machine defined in SPEC.md:
[1. FETCH_RSS] -> [2. FILTER_NEW] -> [3. DOWNLOAD_AUDIO] -> [4. CHUNK_AUDIO]
-> [5. CHANNEL_B] -> [6. CHANNEL_A] -> [7. ATOMIC_SAVE] -> [8. CLEANUP]
"""

import argparse
import calendar
from datetime import datetime, timezone
import json
import os
from pathlib import Path
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

# Constants & Paths
RSS_URL = "https://feeds.buzzsprout.com/2329718.rss"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
EPISODES_FILE = DATA_DIR / "episodes.json"
DOCS_DATA_DIR = PROJECT_ROOT / "docs" / "data"
DOCS_EPISODES_FILE = DOCS_DATA_DIR / "episodes.json"
GLOSSARY_FILE = PROJECT_ROOT / "GLOSSARY.md"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    )
}


# --- Pydantic Structured Output Schemas ---


class TranscriptSegment(BaseModel):
    offset_sec: int = Field(
        description="Segment start offset in seconds from the beginning of this audio chunk"
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


def filter_new_episodes(feed_entries, limit: int = 1) -> list:
    """[2. FILTER_NEW] 对比 data/episodes.json 已有 guid，筛选最新未处理的 N 集（--limit）."""
    log("FILTER", "Checking existing episodes in data/episodes.json...")
    existing_guids = set()
    if EPISODES_FILE.exists():
        try:
            with open(EPISODES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                existing_guids = {ep["guid"] for ep in data.get("episodes", []) if "guid" in ep}
        except Exception as e:
            log("ERROR", f"Failed to read existing {EPISODES_FILE}: {e}")
            sys.exit(1)

    new_entries = [e for e in feed_entries if get_entry_guid(e) not in existing_guids]
    log("FILTER", f"Found {len(new_entries)} unhandled episode(s) (existing in DB: {len(existing_guids)}).")

    if not new_entries:
        log("FILTER", "No new episodes to process. Exiting cleanly.")
        sys.exit(0)

    selected = new_entries[:limit]
    log("FILTER", f"Selected {len(selected)} episode(s) to process (limit: {limit}).")
    return selected


def download_audio_file(audio_url: str, dest_path: Path) -> int:
    """[3. DOWNLOAD_AUDIO] 流式下载 MP3 至系统临时目录."""
    log("DOWNLOAD", f"Downloading audio from {audio_url}...")
    try:
        with requests.get(audio_url, headers=DEFAULT_HEADERS, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            downloaded = 0
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
            size_mb = downloaded / (1024 * 1024)
            log("DOWNLOAD", f"Audio downloaded successfully ({size_mb:.2f} MB, {downloaded} bytes).")
            return downloaded
    except Exception as e:
        log("ERROR", f"Audio download failed: {e}")
        raise


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


def chunk_audio(audio_path: Path, output_dir: Path, chunk_sec: int) -> list[Path]:
    """[4. CHUNK_AUDIO] ffmpeg 转为单声道 64k mp3 并按 --chunk-sec（默认 600s）切片."""
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
    model_name: str,
    fallback_model: str | None,
    contents: list,
    system_instruction: str,
    response_schema: type[BaseModel],
    stage: str = "GENAI",
    temperature: float = 0.2,
    max_retries: int = 5,
) -> BaseModel:
    """Execute structured generation with exponential backoff on 429/503/timeout and optional fallback."""
    current_model = model_name
    last_exception = None

    for attempt in range(1, max_retries + 1):
        try:
            log(stage, f"Calling Gemini model '{current_model}' (attempt {attempt}/{max_retries})...")
            response = client.models.generate_content(
                model=current_model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    response_schema=response_schema,
                    temperature=temperature,
                ),
            )

            if response.parsed is not None:
                return response.parsed
            elif response.text:
                text = response.text.strip()
                if text.startswith("```json"):
                    text = text[7:]
                elif text.startswith("```"):
                    text = text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                return response_schema.model_validate_json(text.strip())
            else:
                raise ValueError("Received empty response from Gemini model.")

        except Exception as e:
            last_exception = e
            log("ERROR", f"[{stage}] Attempt {attempt}/{max_retries} with model '{current_model}' failed: {e}")

            if attempt < max_retries:
                if attempt >= 3 and fallback_model and current_model != fallback_model:
                    log(stage, f"Switching to fallback model '{fallback_model}' after repeated failures.")
                    current_model = fallback_model

                backoff_sec = min(60, (2 ** attempt) * 2)
                log(stage, f"Backing off for {backoff_sec}s before retry...")
                time.sleep(backoff_sec)
            else:
                if fallback_model and current_model != fallback_model:
                    log(
                        stage,
                        f"Primary model retries exhausted. Attempting final try with fallback model '{fallback_model}'...",
                    )
                    try:
                        response = client.models.generate_content(
                            model=fallback_model,
                            contents=contents,
                            config=types.GenerateContentConfig(
                                system_instruction=system_instruction,
                                response_mime_type="application/json",
                                response_schema=response_schema,
                                temperature=temperature,
                            ),
                        )
                        if response.parsed is not None:
                            return response.parsed
                        elif response.text:
                            text = response.text.strip()
                            if text.startswith("```json"):
                                text = text[7:]
                            elif text.startswith("```"):
                                text = text[3:]
                            if text.endswith("```"):
                                text = text[:-3]
                            return response_schema.model_validate_json(text.strip())
                    except Exception as fb_err:
                        log("ERROR", f"[{stage}] Fallback model also failed: {fb_err}")
                        raise fb_err from e

    raise last_exception


def run_channel_b(
    chunks: list[Path],
    glossary_text: str,
    duration_sec: int,
    max_chunks: int = 0,
) -> list[dict]:
    """[5. CHANNEL_B] 逐片上传 Gemini File API → 注入 GLOSSARY.md 转写+翻译 → 立即删除云端文件.

    受 --max-chunks 限制；遇 429/503 指数退避最多 5 次；片间休眠 10s。
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)
    model_name = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
    fallback_model = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash")

    chunks_to_process = chunks[:max_chunks] if max_chunks > 0 else chunks
    log("CH-B", f"Starting Channel B processing for {len(chunks_to_process)} chunk(s) (total chunks: {len(chunks)})...")

    system_instruction = (
        "You are an expert bilingual aviation podcast transcriptionist and translator.\n"
        "Your task is to transcribe and translate the provided podcast audio chunk into Chinese and English bilingual segments.\n\n"
        "Rules:\n"
        "1. Segment into natural semantic blocks of 2-4 sentences each.\n"
        "2. For each block, provide:\n"
        "   - offset_sec: integer seconds relative to the beginning of this audio chunk (>= 0).\n"
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
                model_name=model_name,
                fallback_model=fallback_model,
                contents=contents,
                system_instruction=system_instruction,
                response_schema=ChannelBOutput,
                stage="CH-B",
                temperature=0.2,
            )

            chunk_segments = parsed_resp.segments
            log("CH-B", f"Chunk [{idx + 1}/{len(chunks_to_process)}] produced {len(chunk_segments)} segment(s).")
            for seg in chunk_segments:
                offset = max(0, int(seg.offset_sec))
                global_start = accumulated_chunk_start_sec + offset
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
            log("CH-B", "Sleeping 10s between chunks as per rate-limiting policy...")
            time.sleep(10)

    all_raw_segments.sort(key=lambda s: s["start_sec"])
    final_transcript = []
    for i, seg in enumerate(all_raw_segments):
        final_transcript.append({
            "id": i + 1,
            "start_sec": seg["start_sec"],
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
    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)
    model_name = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
    fallback_model = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash")

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
        model_name=model_name,
        fallback_model=fallback_model,
        contents=[user_prompt],
        system_instruction=system_instruction,
        response_schema=ChannelAOutput,
        stage="CH-A",
        temperature=0.2,
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
    """[7. ATOMIC_SAVE] 合并数据、按 pub_date 降序 → 写 .tmp → os.replace 覆盖 data/episodes.json → 同步复制到 docs/data/episodes.json."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DATA_DIR.mkdir(parents=True, exist_ok=True)

    data = {
        "schema_version": "1.0",
        "podcast_title": "The Air Show",
        "rss_url": RSS_URL,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "episodes": [],
    }

    if EPISODES_FILE.exists():
        with open(EPISODES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

    existing_map = {ep["guid"]: ep for ep in data.get("episodes", []) if "guid" in ep}
    for ep in new_episodes:
        existing_map[ep["guid"]] = ep

    sorted_episodes = sorted(
        existing_map.values(),
        key=lambda x: x.get("pub_date", ""),
        reverse=True,
    )

    data["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["episodes"] = sorted_episodes

    tmp_file = DATA_DIR / "episodes.json.tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    os.replace(tmp_file, EPISODES_FILE)
    shutil.copy2(EPISODES_FILE, DOCS_EPISODES_FILE)
    log("SAVE", f"Saved {len(sorted_episodes)} episode(s) to {EPISODES_FILE} and synced to {DOCS_EPISODES_FILE}.")


def process_episode(entry, args, glossary_text: str) -> None:
    """Process a single podcast episode through the pipeline."""
    guid = get_entry_guid(entry)
    title = entry.get("title", "Untitled Episode")
    pub_date = get_entry_pub_date(entry)
    audio_url = get_entry_audio_url(entry)

    if not audio_url:
        log("ERROR", f"Episode '{title}' ({guid}) has no valid audio URL.")
        return

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
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            smoke_preview_file = DATA_DIR / "_smoke_preview.json"
            preview_data = {
                "schema_version": "1.0",
                "podcast_title": "The Air Show",
                "rss_url": RSS_URL,
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "episodes": [episode_record],
            }
            with open(smoke_preview_file, "w", encoding="utf-8") as f:
                json.dump(preview_data, f, ensure_ascii=False, indent=2)
            log(
                "SAVE",
                f"[SMOKE] Smoke preview written to {smoke_preview_file}. "
                f"Neither {EPISODES_FILE} nor {DOCS_EPISODES_FILE} was modified.",
            )
        else:
            log("SAVE", "Calling atomic save...")
            smoke_preview_file = DATA_DIR / "_smoke_preview.json"
            if smoke_preview_file.exists():
                smoke_preview_file.unlink()
                log("SAVE", f"Removed temporary smoke preview file: {smoke_preview_file}")
            atomic_save([episode_record])

    finally:
        # [8. CLEANUP]
        if temp_dir and temp_dir.exists():
            log("CLEANUP", f"Cleaning up temporary directory: {temp_dir}")
            shutil.rmtree(temp_dir, ignore_errors=True)
            log("CLEANUP", "Temporary directory cleaned up successfully.")


def main():
    parser = argparse.ArgumentParser(description="airshow-reader: Podcast bilingual reading processor.")
    parser.add_argument("--limit", type=int, default=1, help="单次最多处理的新单集数量 (default: 1)")
    parser.add_argument("--chunk-sec", type=int, default=600, help="音频切片时长（秒） (default: 600)")
    parser.add_argument("--dry-run", action="store_true", default=False, help="演习模式，不调用 Gemini API、不修改 JSON")
    parser.add_argument("--max-chunks", type=int, default=0, help="仅处理前 M 个切片（0 表示全部，default: 0）")
    args = parser.parse_args()

    # M1 Audit Fix: GEMINI_API_KEY check moved to the start of main() (only when not dry_run)
    if not args.dry_run:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            log("ERROR", "GEMINI_API_KEY environment variable is required when not in --dry-run mode.")
            sys.exit(1)

    ensure_tools_in_path()

    glossary_text = load_glossary()

    # [1. FETCH_RSS]
    feed = fetch_rss(RSS_URL)

    # [2. FILTER_NEW]
    episodes_to_process = filter_new_episodes(feed.entries, limit=args.limit)

    log("FILTER", f"Loaded GLOSSARY.md successfully ({len(glossary_text)} characters).")

    for entry in episodes_to_process:
        try:
            process_episode(entry, args, glossary_text)
        except Exception as e:
            log("ERROR", f"Episode '{entry.get('title', 'Unknown')}' processing failed: {e}")


if __name__ == "__main__":
    main()
