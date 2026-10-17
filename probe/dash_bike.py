import json, urllib.request, urllib.parse, collections, re
from concurrent.futures import ThreadPoolExecutor
from vss_client import cfg, B, req, tok, GPU_HOST
rows=json.load(open('dash_all.json'))
NEG=re.compile(r"\b(no|not|without|absence of|nor|neither)\b[^.]{0,60}\b(cyclist|bicycl|bike)",re.I)
POS=re.compile(r"(cyclist|bicyclist|person (riding|on) a bicycle|riding a bicycle|bike rider)",re.I)
def oc(r):
    v=r.get('object_counts') or {}
    if isinstance(v,str):
        try: v=json.loads(v)
        except: v={}
    return v
for cam in ['pie_cam-3','i24_cam-1']:
    R=[r for r in rows if r['camera_id']==cam]
    pos=[r for r in R if POS.search(r.get('reasoning_content') or '') and not NEG.search(r.get('reasoning_content') or '')]
    det=[r for r in R if oc(r).get('bicycle')]
    print(cam,'desc mentions an actual cyclist:',len(pos),'| YOLO bicycle detected:',len(det),'| both:',len({r['source'] for r in pos}&{r['source'] for r in det}))
T=[r for r in rows if r['camera_id']=='pie_cam-3' and oc(r).get('bicycle')]
VEH=('car','truck','bus','motorcycle')
def side_by_side(r):
    try: d=req('/api/v1/videos/detections?source='+urllib.parse.quote(r['source'],safe=''),None,tok)
    except Exception: return r,None
    H,W=d['video_shape']; best=None
    for f in d['frames']:
        bikes=[x['bbox'] for x in f['detections'] if x['label']=='bicycle' and x['confidence']>0.4]
        vehs=[x for x in f['detections'] if x['label'] in VEH and x['confidence']>0.5]
        for b in bikes:
            bw=b[2]-b[0]; bh=b[3]-b[1]
            if bh<H*0.06: continue
            for v in vehs:
                vb=v['bbox']
                # similar depth: bottoms within 25% of bike height; horizontal gap small vs bike width
                if abs(vb[3]-b[3])>0.35*bh: continue
                gap=max(vb[0]-b[2], b[0]-vb[2])
                g=gap/bw
                sc=(g, -bh)
                if best is None or sc<best['sc']: best={'sc':sc,'gap_bikewidths':round(g,2),'t':f['time_sec'],'veh':v['label'],'bike_h_frac':round(bh/H,2)}
    return r,best
with ThreadPoolExecutor(8) as ex: res=list(ex.map(side_by_side,T))
res=[(r,b) for r,b in res if b]
res.sort(key=lambda x:x[1]['sc'])
print('\nToronto segments with a vehicle beside a cyclist at same depth:',len(res))
print('gap < 0.5 bike widths:',sum(b['gap_bikewidths']<0.5 for _,b in res),'| overlapping (<=0):',sum(b['gap_bikewidths']<=0 for _,b in res))
json.dump([(r['source'],r['filename'],b,r.get('reasoning_content')) for r,b in res],open('dash_bike_scores.json','w'))
for r,b in [x for x in res if x[1]['bike_h_frac']>=0.12][:12]:
    print(f"gap={b['gap_bikewidths']:>5} bikeH={b['bike_h_frac']} {b['veh']:6s} t={b['t']:.1f} {r['filename'][:48]} | {(r.get('reasoning_content') or '')[:150]}")
