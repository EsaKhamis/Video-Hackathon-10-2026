import json, urllib.request, urllib.parse, subprocess, base64, sys
from vss_client import cfg, B, req, tok, GPU_HOST
scores=json.load(open('/tmp/gopro_scores.json'))
# pick daytime clips with a big close vehicle
cands=sorted([x for x in scores if x[1]['fill']>=0.3 and 'GX05' not in x[0]], key=lambda x:-x[1]['fill'])[:3]
G=GPU_HOST; R=f"http://{G}:8001"
H={'Authorization':'Bearer '+cfg['GPU_BEARER_TOKEN'],'Content-Type':'application/json'}
model=json.load(urllib.request.urlopen(urllib.request.Request(R+'/v1/models',headers=H)))['data'][0]['id']
for i,(s,b) in enumerate(cands):
    url=B+'/api/v1/videos/stream?source='+urllib.parse.quote(s,safe='')+'&token='+tok
    open(f'c{i}.mp4','wb').write(urllib.request.urlopen(url,timeout=120).read())
    t=b.get('t') or 2.5
    subprocess.run(['ffmpeg','-loglevel','error','-y','-ss',str(t),'-i',f'c{i}.mp4','-frames:v','1','-q:v','2',f'f{i}.jpg'],check=True)
    img=base64.b64encode(open(f'f{i}.jpg','rb').read()).decode()
    body={'model':model,'temperature':0,'max_tokens':300,'messages':[{'role':'user','content':[
        {'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+img}},
        {'type':'text','text':'This is a frame from a cyclist helmet camera. For each motor vehicle that is close to the cyclist, give: vehicle type, colour, make if recognisable, and the licence plate text exactly as readable (write UNREADABLE if you cannot read it confidently). Then say whether the vehicle appears to be in the bike lane or passing dangerously close.'}]}]}
    try:
        r=json.load(urllib.request.urlopen(urllib.request.Request(R+'/v1/chat/completions',data=json.dumps(body).encode(),headers=H),timeout=180))
        ans=r['choices'][0]['message']['content']
    except urllib.error.HTTPError as e: ans='HTTP '+str(e.code)+' '+e.read().decode()[:300]
    print(f'\n=== f{i}.jpg fill={b["fill"]} {b.get("label")} {s.split("/")[-1]}\n{ans}')
