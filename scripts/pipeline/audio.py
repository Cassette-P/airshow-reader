"""Audio handling: four-tier anti-403 downloader, duration probing, and ffmpeg slicing."""

from pathlib import Path
import subprocess

import requests

from pipeline.config import log


def download_audio_file(audio_url: str, dest_path: Path, show_id: str = "") -> int:
    """[3. DOWNLOAD_AUDIO] 流式下载 MP3 至系统临时目录（四级策略自动降级）."""
    log("DOWNLOAD", f"Downloading audio from {audio_url}...", show_id=show_id)

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
        with requests.get(audio_url, headers=headers, stream=True, timeout=180) as resp:
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
        with requests.get(audio_url, headers=headers, stream=True, timeout=180) as resp:
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
            timeout=180,
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
                "180",
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
        log("DOWNLOAD", f"Trying download strategy: {name}...", show_id=show_id)
        try:
            runner()
            if dest_path.exists() and dest_path.stat().st_size > 0:
                downloaded = dest_path.stat().st_size
                size_mb = downloaded / (1024 * 1024)
                log(
                    "DOWNLOAD",
                    f"Audio downloaded successfully via {name} ({size_mb:.2f} MB, {downloaded} bytes).",
                    show_id=show_id,
                )
                return downloaded
            else:
                raise RuntimeError(
                    f"{name} completed but output file is missing or empty."
                )
        except Exception as e:
            last_exception = e
            log("DOWNLOAD", f"{name} failed: {e}", show_id=show_id)
            _cleanup_partial()

    log("ERROR", f"Audio download failed across all strategies: {last_exception}", show_id=show_id)
    if last_exception:
        raise last_exception
    raise RuntimeError("Audio download failed across all strategies.")


def get_audio_duration_sec(audio_path: Path, show_id: str = "") -> int:
    """ffprobe 获取 duration_sec (整数秒)."""
    log("DOWNLOAD", "Probing audio duration with ffprobe...", show_id=show_id)
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
            log("DOWNLOAD", f"Audio duration via ffprobe: {dur}s ({dur // 60}m {dur % 60}s).", show_id=show_id)
            return dur
    except Exception as e:
        log("ERROR", f"ffprobe format duration check failed: {e}", show_id=show_id)

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
        log("DOWNLOAD", f"Audio duration via stream ffprobe: {dur}s ({dur // 60}m {dur % 60}s).", show_id=show_id)
        return dur

    raise RuntimeError(f"Unable to probe duration for {audio_path}")


def chunk_audio(audio_path: Path, output_dir: Path, chunk_sec: int, show_id: str = "") -> list[Path]:
    """[4. CHUNK_AUDIO] ffmpeg 转为单声道 64k mp3 并按 --chunk-sec（默认 300s）切片."""
    output_pattern = output_dir / "chunk_%03d.mp3"
    log("FFMPEG", f"Transcoding and slicing audio: mono, 64kbps, segment_time={chunk_sec}s...", show_id=show_id)
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
        log("ERROR", f"ffmpeg audio slicing failed: {e.stderr}", show_id=show_id)
        raise

    chunks = sorted(output_dir.glob("chunk_*.mp3"))
    log("FFMPEG", f"Audio slicing completed. Generated {len(chunks)} chunk(s).", show_id=show_id)
    return chunks
