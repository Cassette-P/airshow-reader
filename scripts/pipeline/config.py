"""Pipeline configuration, constants, environment variables, model routing, and logging."""

import json
import os
from pathlib import Path
import shutil
import sys
from typing import Optional

# Project root and core configuration paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PODCASTS_CONFIG_FILE = PROJECT_ROOT / "podcasts.json"
GLOSSARIES_DIR = PROJECT_ROOT / "glossaries"
LEGACY_GLOSSARY_FILE = PROJECT_ROOT / "GLOSSARY.md"

DOCS_DATA_DIR = PROJECT_ROOT / "docs" / "data"
SHOWS_MANIFEST_FILE = DOCS_DATA_DIR / "shows.json"

# Legacy v2 paths (for migration)
LEGACY_V2_INDEX_FILE = DOCS_DATA_DIR / "index.json"
LEGACY_V2_EPISODES_DIR = DOCS_DATA_DIR / "episodes"

# Legacy v1 paths (for migration)
LEGACY_DATA_DIR = PROJECT_ROOT / "data"
LEGACY_EPISODES_FILE = LEGACY_DATA_DIR / "episodes.json"
LEGACY_DOCS_EPISODES_FILE = DOCS_DATA_DIR / "episodes.json"

MODEL_USAGE_STATS: dict[str, dict[str, int]] = {
    "CH-B": {},
    "CH-A": {},
    "ENRICH": {},
}

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    )
}

# Default model pools
DEFAULT_CH_B_POOL = ["gemini-4-hydrogen", "gemini-3.8-flash", "gemini-3.5-flash-lite"]
DEFAULT_CH_A_POOL = ["gemini-4-carbon", "gemini-4-neon", "gemini-3.8-flash"]
DEFAULT_ENRICH_POOL = ["gemini-3.8-flash", "gemini-4-hydrogen", "gemini-3.5-flash-lite"]


def get_env_stripped(key: str, default: str = "") -> str:
    """Read environment variable with .strip() and empty-string fallback."""
    val = os.environ.get(key)
    if val is not None:
        val = val.strip()
        if val:
            return val
    return default


def get_ch_b_model_pool() -> list[str]:
    """Get Channel B candidate model pool with environment variable override."""
    env_ch_b = get_env_stripped("GEMINI_MODEL_CH_B") or get_env_stripped("GEMINI_MODEL")
    if env_ch_b:
        return [env_ch_b] + [m for m in DEFAULT_CH_B_POOL if m != env_ch_b]
    return list(DEFAULT_CH_B_POOL)


def get_ch_a_model_pool() -> list[str]:
    """Get Channel A candidate model pool with environment variable override."""
    env_ch_a = get_env_stripped("GEMINI_MODEL_CH_A")
    if env_ch_a:
        return [env_ch_a] + [m for m in DEFAULT_CH_A_POOL if m != env_ch_a]
    return list(DEFAULT_CH_A_POOL)


def get_enrich_model_pool() -> list[str]:
    """Get Enrichment candidate model pool with environment variable override."""
    env_enrich = get_env_stripped("GEMINI_MODEL_ENRICH")
    if env_enrich:
        return [env_enrich] + [m for m in DEFAULT_ENRICH_POOL if m != env_enrich]
    return list(DEFAULT_ENRICH_POOL)


def resolve_ch_a_thinking_level(model_name: str, env_override: str = "") -> Optional[str]:
    """Resolve thinking level for Channel A based on env var override or model defaults."""
    override = env_override.strip() if env_override and env_override.strip() else get_env_stripped("GEMINI_THINKING_CH_A")
    if override:
        val = override.lower()
        if val in ("default", "none"):
            return None
        return val

    m = model_name.lower().strip()
    if "gemini-4-neon" in m:
        return "high"
    elif "gemini-4-carbon" in m:
        return "high"
    else:
        return "high"


def get_next_fallback_thinking_level(current_level: Optional[str]) -> Optional[str]:
    """Return next fallback level in chain: xhigh -> high -> low -> None."""
    if not current_level:
        return None
    lvl = current_level.strip().lower()
    if lvl == "xhigh":
        return "high"
    elif lvl == "high":
        return "low"
    return None


def _format_level(lvl: Optional[str]) -> str:
    """Format thinking level for logging."""
    return f"'{lvl}'" if lvl else "None"


def log(stage: str, message: str, show_id: str = "") -> None:
    """Print formatted log message conforming to [TAG][STAGE] standard."""
    tag = show_id.upper() if show_id else "AIRSHOW"
    print(f"[{tag}][{stage}] {message}", flush=True)


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


def load_podcasts_config() -> dict:
    """Load podcasts.json registry from project root."""
    if not PODCASTS_CONFIG_FILE.exists():
        log("ERROR", f"podcasts.json not found at {PODCASTS_CONFIG_FILE}")
        sys.exit(1)
    for enc in ["utf-8", "utf-8-sig", "gbk", "gb18030", "cp936"]:
        try:
            with open(PODCASTS_CONFIG_FILE, "r", encoding=enc) as f:
                return json.load(f)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    log("ERROR", "Failed to decode podcasts.json with supported encodings (utf-8, gbk, etc.).")
    sys.exit(1)
