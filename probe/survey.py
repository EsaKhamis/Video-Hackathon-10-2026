import json, urllib.request, urllib.parse, collections
from vss_client import cfg, B, req, tok, GPU_HOST
d=req('/api/v1/dashboard/stats?scope=all',None,tok)
print('overview',{k:d['overview'].get(k) for k in ['unique_videos','indexed_clips','segment_rows']})
md=d.get('metadata',{})
for k in md: print(k, json.dumps(md[k])[:700])
# first-person bike footage on OTHER cameras
hits=collections.Counter(); ex={}
for q in ["first-person view from a bicycle with handlebars visible","cyclist helmet camera point of view riding in traffic","riding a bike in a bike lane, camera mounted on bicycle"]:
    r=req('/api/v1/search',{'query':q,'top_k':100,'llm_top_n':1,'min_similarity':0.3},tok)
    for x in r.get('results',[]):
        m=req('/api/v1/videos/metadata?source='+urllib.parse.quote(x['source'],safe=''),None,tok) if x['source'] not in ex else None
        cam=(x.get('camera_id') or (m or {}).get('camera_id') or json.dumps(m or {})[:0])
        ex.setdefault(x['source'],(x.get('camera_id'),x.get('location'),round(x['similarity_score'],2)))
print('result keys sample', list(r['results'][0].keys()) if r.get('results') else None)
c=collections.Counter((v[0],v[1]) for v in ex.values()); print('first-person-bike hits by camera/location', c)
