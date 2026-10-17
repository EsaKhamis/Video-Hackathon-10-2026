import json, urllib.request, urllib.parse, collections, re
from concurrent.futures import ThreadPoolExecutor
from vss_client import cfg, B, req, tok, GPU_HOST
def parents(loc):
    out={}; off=0
    while True:
        r=req(f'/api/v1/videos/explore?scope=all&limit=100&offset={off}&location={loc}',None,tok)
        items=r.get('chunks',[])
        for it in items: out[it['original_video']]=it
        off+=len(items)
        if len(items)<100: break
    return out
def segs(pv): return req('/api/v1/tools/segments?original_video='+urllib.parse.quote(pv,safe=''),None,tok).get('segments',[])
rows=[]
for loc in ['toronto','nashville']:
    P=parents(loc)
    with ThreadPoolExecutor(8) as ex:
        for ss in ex.map(segs,P): rows+=ss
    print(loc,'parents',len(P))
print(collections.Counter(r['camera_id'] for r in rows))
json.dump(rows,open('dash_all.json','w'))
for cam in ['pie_cam-3','i24_cam-1']:
    R=[r for r in rows if r['camera_id']==cam]
    files=collections.Counter(re.sub(r'_chunk.*','',r['filename'].split('_',2)[-1]) for r in R)
    print('\n==',cam,len(R),'segments; recordings:',dict(files))
    T=lambda r:(r.get('reasoning_content') or '')
    kws={'cyclist|bicycl|bike':0,'bike lane':0,'pedestrian':0,'brake light|braking|brakes':0,'tow truck|ambulance|police|fire truck':0,'chang\w* lanes?|merg|cut':0,'red light|traffic light':0,'parked.*(in|blocking)|double':0,'turn':0,'streetcar|tram':0}
    for k in kws: kws[k]=sum(bool(re.search(k,T(r),re.I)) for r in R)
    print('keywords',kws)
    oc=collections.Counter(); 
    for r in R:
        for k in (r.get('object_counts') or {}): oc[k]+=1
    print('segments with object:',dict(oc.most_common(10)))
    print('example:',T(R[0])[:300])
