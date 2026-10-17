import json, urllib.request, urllib.parse, subprocess, base64
from concurrent.futures import ThreadPoolExecutor
from vss_client import cfg, B, req, tok, GPU_HOST
S=json.load(open('dash_bike_scores.json'))
# 1) plate test, full-res frames from 3 dashcam clips
G=GPU_HOST; R=f"http://{G}:8001"
H={'Authorization':'Bearer '+cfg['GPU_BEARER_TOKEN'],'Content-Type':'application/json'}
model=json.load(urllib.request.urlopen(urllib.request.Request(R+'/v1/models',headers=H)))['data'][0]['id']
for w in ['set04_video_chunk_0016','set02_video_chunk_0006','set03_video_chunk_0021']:
    src,fn,b,_=next(x for x in S if w in x[1])
    open('dash/tmp.mp4','wb').write(urllib.request.urlopen(B+'/api/v1/videos/stream?source='+urllib.parse.quote(src,safe='')+'&token='+tok,timeout=120).read())
    subprocess.run(['ffmpeg','-loglevel','error','-y','-ss',str(b['t']),'-i','dash/tmp.mp4','-frames:v','1','-q:v','2',f'dash/{w}_full.jpg'],check=True)
    img=base64.b64encode(open(f'dash/{w}_full.jpg','rb').read()).decode()
    body={'model':model,'temperature':0,'max_tokens':200,'messages':[{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+img}},{'type':'text','text':'List every licence plate you can read in this dashcam frame. For each: the vehicle (type, colour, make) and the plate text exactly. Only include plates you can read with confidence; otherwise write NONE.'}]}]}
    r=json.load(urllib.request.urlopen(urllib.request.Request(R+'/v1/chat/completions',data=json.dumps(body).encode(),headers=H),timeout=180))
    print('PLATE',w,'->',r['choices'][0]['message']['content'].replace('\n',' | '))
# 2) ridden-and-moving cyclist filter over all Toronto bicycle segments
def iou(a,b):
    x1,y1=max(a[0],b[0]),max(a[1],b[1]); x2,y2=min(a[2],b[2]),min(a[3],b[3])
    i=max(0,x2-x1)*max(0,y2-y1); return i/((a[2]-a[0])*(a[3]-a[1])+1e-9)
def ridden(src):
    try: d=req('/api/v1/videos/detections?source='+urllib.parse.quote(src,safe=''),None,tok)
    except Exception: return src,None
    Hh,W=d['video_shape']; n=0; best=None
    for f in d['frames']:
        ps=[x['bbox'] for x in f['detections'] if x['label']=='person' and x['confidence']>0.4]
        bs=[x['bbox'] for x in f['detections'] if x['label']=='bicycle' and x['confidence']>0.4]
        vs=[x['bbox'] for x in f['detections'] if x['label'] in ('car','truck','bus') and x['confidence']>0.5]
        for b in bs:
            # rider: person box whose lower half overlaps the bike
            if not any(p[1]<b[1] and iou(b,p)>0.3 for p in ps): continue
            n+=1; bw=b[2]-b[0]; bh=b[3]-b[1]
            for v in vs:
                if abs(v[3]-b[3])>0.35*bh: continue
                gap=max(v[0]-b[2],b[0]-v[2])/bw
                if -0.8<gap and (best is None or gap<best[0]): best=(round(gap,2),round(f['time_sec'],1),round(bh/Hh,2))
    return src,(n,best)
srcs=list({x[0] for x in S})
with ThreadPoolExecutor(8) as ex: res=[x for x in ex.map(ridden,srcs) if x[1]]
rid=[x for x in res if x[1][0]>=10]
close=[x for x in rid if x[1][1] and x[1][1][0]<0.5]
print('\nToronto segments with a ridden cyclist (>=10 frames):',len(rid),'| of those, a vehicle within half a bike-width alongside:',len(close))
json.dump(close,open('dash_close.json','w'))
fn={x[0]:x[1] for x in S}
for s,(n,b) in sorted(close,key=lambda x:x[1][1][0])[:10]: print(' ',b,fn[s][:48])
