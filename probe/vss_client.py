import json, os, glob, urllib.request

cfg={}
for l in open(glob.glob('/config/*.config')[0]):
    if '=' in l and not l.startswith('#'):
        k,v=l.strip().split('=',1); cfg[k]=v.strip('"\'')
B=cfg['INGRESS_URL']
# Not in /config; set GPU_HOST in the environment to the Cosmos3-Reason host.
GPU_HOST=os.environ.get('GPU_HOST') or cfg.get('GPU_HOST')

def req(path, body=None, tok=None):
    r=urllib.request.Request(B+path, data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type':'application/json', **({'Authorization':'Bearer '+tok} if tok else {})})
    return json.load(urllib.request.urlopen(r, timeout=120))

tok=req('/api/v1/auth/login',{'username':cfg['USERNAME'],'password':cfg['PASSWORD']})['access_token']
