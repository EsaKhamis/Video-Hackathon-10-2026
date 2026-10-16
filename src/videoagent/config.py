import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
PROMPTS = Path(__file__).resolve().parent / "prompts"

load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    wandb_api_key: str = _env("WANDB_API_KEY")
    wandb_project: str = _env("WANDB_PROJECT", "video-agent")
    llm_base_url: str = _env("LLM_BASE_URL", "https://api.inference.wandb.ai/v1")
    llm_model: str = _env("LLM_MODEL", "meta-llama/Llama-3.1-8B-Instruct")

    cosmos_base_url: str = _env("COSMOS_BASE_URL")
    cosmos_api_key: str = _env("COSMOS_API_KEY", "EMPTY")
    cosmos_model: str = _env("COSMOS_MODEL", "nvidia/Cosmos-Reason2-2B")
    cosmos_max_frames: int = int(_env("COSMOS_MAX_FRAMES", "8"))

    vast_base_url: str = _env("VAST_BASE_URL")
    vast_api_key: str = _env("VAST_API_KEY")

    video_source: str = _env("VIDEO_SOURCE", str(DATA / "footage" / "sample.mp4"))
    yolo_model: str = _env("YOLO_MODEL", "yolo11n.pt")
    stream_fps: float = float(_env("STREAM_FPS", "3"))
    clip_seconds: float = float(_env("CLIP_SECONDS", "6"))
    trigger_classes: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            c.strip() for c in _env("TRIGGER_CLASSES", "person").split(",") if c.strip()
        )
    )
    trigger_min_frames: int = int(_env("TRIGGER_MIN_FRAMES", "3"))
    trigger_cooldown_s: float = float(_env("TRIGGER_COOLDOWN_S", "10"))

    events_path: Path = DATA / "events.jsonl"
    clips_dir: Path = DATA / "clips"
    index_path: Path = DATA / "index.json"


settings = Settings()


def load_prompt(name: str) -> str:
    return (PROMPTS / f"{name}.txt").read_text()
