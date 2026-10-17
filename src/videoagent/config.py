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


def _wandb_project() -> str:
    # W&B Inference and Weave both want "team/project"; the VM sets them separately.
    project = _env("WANDB_PROJECT", "video-agent")
    team = _env("WANDB_TEAM")
    return f"{team}/{project}" if team and "/" not in project else project


@dataclass(frozen=True)
class Settings:
    wandb_api_key: str = _env("WANDB_API_KEY")
    wandb_project: str = _wandb_project()
    llm_base_url: str = _env("LLM_BASE_URL", "https://api.inference.wandb.ai/v1")
    llm_model: str = _env("LLM_MODEL", "openai/gpt-oss-120b")

    cosmos_base_url: str = _env("COSMOS_BASE_URL")
    cosmos_api_key: str = _env("COSMOS_API_KEY") or _env("GPU_BEARER_TOKEN", "EMPTY")
    cosmos_model: str = _env("COSMOS_MODEL", "nvidia/cosmos3-nano-reasoner")
    cosmos_max_frames: int = int(_env("COSMOS_MAX_FRAMES", "5"))

    # VSS retrieval backend; the team VM exports these.
    vast_base_url: str = _env("VAST_BASE_URL") or _env("INGRESS_URL")
    vast_username: str = _env("USERNAME")
    vast_password: str = _env("PASSWORD")

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
    # "yolo" flags on TRIGGER_CLASSES; "scoreboard" flags when the score overlay changes.
    trigger_mode: str = _env("TRIGGER_MODE", "yolo")
    scoreboard_roi: tuple[int, ...] = field(
        default_factory=lambda: tuple(int(v) for v in _env("SCOREBOARD_ROI", "150,8,258,72").split(","))
    )

    events_path: Path = DATA / "events.jsonl"
    clips_dir: Path = DATA / "clips"
    index_path: Path = DATA / "index.json"


settings = Settings()


def load_prompt(name: str) -> str:
    return (PROMPTS / f"{name}.txt").read_text()
