"""Optional W&B Weave tracing for the engine stages.

  import wb_trace as trace
  trace.init()                      # once, after the env is loaded
  @trace.op(postprocess_inputs=...) # a no-op decorator when weave or WANDB_API_KEY is missing

Needs WANDB_API_KEY and WANDB_PROJECT (team/project, or with WANDB_TEAM), from the repo .env or the shell. Without them, or without the
weave package, every stage still runs on the stdlib alone and just logs nothing.
"""
import contextlib, os

try:
    import weave
except ImportError:
    weave = None

client = None


def project(env=os.environ):
    name = env.get('WANDB_PROJECT', '').strip()
    team = env.get('WANDB_TEAM', '').strip()
    return f'{team}/{name}' if team and name and '/' not in name else name


def dotenv():
    """WANDB_* from the repo .env."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env')
    out = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line.startswith('WANDB_') and '=' in line:
                k, v = line.split('=', 1)
                out[k.strip()] = v.strip().strip('"\'')
    return out


def settings():
    """WANDB_* for our own W&B project: the repo .env wins over the shell (see init)."""
    return {**{k: v for k, v in os.environ.items() if k.startswith('WANDB_')}, **dotenv()}


@contextlib.contextmanager
def own_key():
    """The team's own key, entity and project in the environment for the block, then the shell's put back:
    Stage E's Inference calls use the shell's key with the shell's project. The wandb SDK wants the project
    without the team (WANDB_ENTITY holds that), so "team/project" is split here."""
    cfg = settings()
    entity, _, name = project(cfg).rpartition('/')
    want = {'WANDB_API_KEY': cfg.get('WANDB_API_KEY', ''), 'WANDB_PROJECT': name, 'WANDB_ENTITY': entity}
    shell = {k: os.environ.get(k) for k in want}
    os.environ.update({k: v for k, v in want.items() if v})
    try:
        yield
    finally:
        for k, v in shell.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def init():
    """Start Weave once; returns the client, or None when tracing is off.

    The repo .env wins over the shell here: the VM's /etc/environment sets the organisers' W&B account
    (vastdata/team-1), which Stage E still uses for Inference, but traces go to the team's own project.
    """
    global client
    cfg = settings()
    if client is None and weave and cfg.get('WANDB_API_KEY') and project(cfg):
        with own_key():  # weave logs in from the environment
            client = weave.init(project(cfg))
    return client


def op(**kwargs):
    if weave is None:
        return lambda fn: fn
    return weave.op(**kwargs)


def strip_media(content):
    """Message content without base64 video or images, so a trace holds the prompt, not megabytes."""
    if not isinstance(content, list):
        return content
    return [{'type': p['type'], 'url': '<base64 omitted>'} if p.get('type') in ('video_url', 'image_url') else p
            for p in content]
