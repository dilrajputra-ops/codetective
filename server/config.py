import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

GOBROKER_PATH = Path(
    os.environ.get("GOBROKER_PATH", "/Users/dilraj.putra/Documents/alpaca/gobroker")
).resolve()
GH_REPO = os.environ.get("GH_REPO", "alpacahq/gobroker")

# Local LLM via Ollama. Nothing is sent off-machine.
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")
OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")

CACHE_DIR = Path("/tmp/codemap-cache")
CACHE_TTL_SECONDS = 600
# Merged PRs are immutable history — cache much longer than open PRs.
MERGED_PR_TTL_SECONDS = int(os.environ.get("MERGED_PR_TTL_SECONDS", str(3600)))
# Recently-investigated paths (for startup prewarm).
RECENT_PATHS_FILE = Path(os.environ.get("RECENT_PATHS_FILE", "/tmp/codemap-recent.json"))
PREWARM_TOP_N = int(os.environ.get("PREWARM_TOP_N", "10"))

# Vector index over gobroker file paths (for "similar files / who else might know").
VECTOR_DB = Path(os.environ.get("VECTOR_DB", "/tmp/codemap-vectors.sqlite"))

# Hand-curated routing + departures (project-local).
_REPO_ROOT = Path(__file__).resolve().parents[1]
OWNERS_FILE = Path(os.environ.get("OWNERS_FILE", _REPO_ROOT / "owners.json"))
DEPARTED_FILE = Path(os.environ.get("DEPARTED_FILE", _REPO_ROOT / "departed.txt"))
SLACK_CHANNELS_FILE = Path(os.environ.get("SLACK_CHANNELS_FILE", _REPO_ROOT / "slack_channels.json"))
GH_TEAMS_CACHE = Path(os.environ.get("GH_TEAMS_CACHE", "/tmp/codemap-gh-teams.json"))
GH_TEAMS_TTL_SECONDS = int(os.environ.get("GH_TEAMS_TTL_SECONDS", str(24 * 3600)))
GH_ORG = os.environ.get("GH_ORG", "alpacahq")

# Merged-PR lookback windows (days).
MERGED_WINDOWS_DAYS = (30, 90)
