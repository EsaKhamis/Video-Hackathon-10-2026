import json, urllib.request, urllib.parse, collections, re
from vss_client import cfg, B, req, tok, GPU_HOST
# all parents in new_york
parents={}
off=0
while True:
    r=req(f'/api/v1/videos/explore?scope=all&limit=100&offset={off}&location=new_york',None,tok)
    items=r.get('chunks',[])
    if off==0: print('explore keys', list(r.keys()) if isinstance(r,dict) else 'list', 'n', len(items))
    if not items: break
    for it in items: parents[it.get('original_video')]=it
    off+=len(items)
    if len(items)<100: break
print('new_york parents', len(parents))
segs={}
for pv,it in parents.items():
    r=req('/api/v1/tools/segments?original_video='+urllib.parse.quote(pv,safe=''),None,tok)
    rows=r.get('segments') or r.get('results') or [] if isinstance(r,dict) else r
    if pv==next(iter(parents)): print('segments resp keys', list(r.keys()) if isinstance(r,dict) else 'list')
    if isinstance(rows,dict): rows=[]
    for s in rows:
        if s.get('camera_id')=='nyc_bike_gopro-1': segs[s['source']]=s
print('gopro segments enumerated', len(segs))
rows=list(segs.values())
byfile=collections.Counter(re.search(r'_(GOPR\d+|GX\d+)_',s['source']).group(1) if re.search(r'_(GOPR\d+|GX\d+)_',s['source']) else 'other' for s in rows)
print('by source recording', byfile)
dur=sum((s.get('segment_end_sec') or 0)-(s.get('segment_start_sec') or 0) for s in rows); print('total minutes', round(dur/60,1))
night=sum(bool(re.search(r'night|dark',s.get('reasoning_content') or '',re.I)) for s in rows); print('night segs',night)
json.dump(rows,open('gopro_all.json','w'))
print('sample keys', list(rows[0].keys()) if rows else None)
