import json,glob,base64,time,urllib.request,sys
cfg={}
for l in open(glob.glob('/config/*.config')[0]):
    if '=' in l and not l.startswith('#'):
        k,v=l.strip().split('=',1); cfg[k]=v.strip('"\'')
R='http://166.19.38.112:8001'; H={'Authorization':'Bearer '+cfg['GPU_BEARER_TOKEN'],'Content-Type':'application/json'}
model=json.load(urllib.request.urlopen(urllib.request.Request(R+'/v1/models',headers=H)))['data'][0]['id']
PROMPT=open(__import__('os').path.join(__import__('os').path.dirname(__file__),'screen_prompt.txt')).read()
def ask(path):
    b=base64.b64encode(open(path,'rb').read()).decode()
    body={'model':model,'temperature':0,'max_tokens':500,'messages':[{'role':'user','content':[{'type':'video_url','video_url':{'url':'data:video/mp4;base64,'+b}},{'type':'text','text':PROMPT}]}]}
    t=time.time()
    try:
        r=json.load(urllib.request.urlopen(urllib.request.Request(R+'/v1/chat/completions',data=json.dumps(body).encode(),headers=H),timeout=240))
        return round(time.time()-t,1), r['choices'][0]['message']['content'], r.get('usage')
    except urllib.error.HTTPError as e: return round(time.time()-t,1),'HTTP %s %s'%(e.code,e.read()[:300]),None
for p in sys.argv[1:]:
    print('==',p, *ask(p))
