"""Optional W&B Weave tracing for the engine stages.

  import wb_trace as trace
  trace.init()                      # once, after the env is loaded
  @trace.op(postprocess_inputs=...) # a no-op decorator when weave or WANDB_API_KEY is missing

Needs WANDB_API_KEY and WANDB_PROJECT (team/project, or with WANDB_TEAM), from the repo .env or the shell. Without them, or without the
weave package, every stage still runs on the stdlib alone and just logs nothing.
"""
import os

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


def init():
    """Start Weave once; returns the client, or None when tracing is off.

    The repo .env wins over the shell here: the VM's /etc/environment sets the organisers' W&B account
    (vastdata/team-1), which Stage E still uses for Inference, but traces go to the team's own project.
    """
    global client
    cfg = {**{k: v for k, v in os.environ.items() if k.startswith('WANDB_')}, **dotenv()}
    if client is None and weave and cfg.get('WANDB_API_KEY') and project(cfg):
        shell_key = os.environ.get('WANDB_API_KEY')
        os.environ['WANDB_API_KEY'] = cfg['WANDB_API_KEY']  # weave logs in from the environment
        try:
            client = weave.init(project(cfg))
        finally:  # put the shell's key back: Stage E's Inference calls use it with the shell's project
            if shell_key is None:
                os.environ.pop('WANDB_API_KEY', None)
            else:
                os.environ['WANDB_API_KEY'] = shell_key
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
