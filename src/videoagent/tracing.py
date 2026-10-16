import weave

from .config import settings

_started = False


def init() -> None:
    """Start Weave tracing once per process; no-op without a W&B key."""
    global _started
    if _started or not settings.wandb_api_key:
        return
    weave.init(settings.wandb_project)
    _started = True
