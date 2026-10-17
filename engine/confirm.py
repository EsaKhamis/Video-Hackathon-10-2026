"""Stage C: focused Cosmos confirmation (PLAN.md "Stage C focused prompt").

For each segment: download from the backend, transcode to 1280 px @ 6 fps, ask Cosmos3-Reason with the
Stage B detector hint + the full schema, then ask for the signal on a full-resolution still 1 s before the
peak. Writes data/stage_c.json, read by engine/build_incidents.py (fault via the Stage D rules there).

  python3 engine/confirm.py GOPR0130_chunk_0026_segment_003 GOPR0130_chunk_0017_segment_005
"""
import argparse, base64, glob, json, os, re, subprocess, sys, tempfile, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
DATA = os.path.join(ROOT, 'data')
cfg = {}
for l in open(glob.glob('/config/*.config')[0]):
    if '=' in l and not l.startswith('#'):
        k, v = l.strip().split('=', 1)
        cfg[k] = v.strip('"\'')
B = cfg['INGRESS_URL']
GPU = os.environ.get('GPU_HOST') or cfg.get('GPU_HOST') or 'http://166.19.38.112:8001'
GH = {'Authorization': 'Bearer ' + cfg['GPU_BEARER_TOKEN'], 'Content-Type': 'application/json'}

SCHEMA = """Answer ONLY with JSON:
{"near_miss": true|false,
 "event": "close_pass"|"cut_in"|"crossing_path"|"turning_across"|"blocked_bike_lane"|"door_zone"|"none",
 "vehicle": {"type": "car|taxi|van|suv|truck|bus|motorcycle|none", "colour": "<colour you see>", "side": "left"|"right"|"front"|"none"},
 "closest_distance": "under_1m"|"1_to_2m"|"over_2m"|"unknown",
 "peak_time_sec": number,
 "rider_position": "bike_lane"|"traffic_lane"|"sidewalk"|"crosswalk"|"unknown",
 "separator": "yes"|"no"|"unknown",
 "vehicle_action": "overtaking"|"turning_across"|"pulling_out"|"crossing"|"stopped_in_lane"|"parallel_in_own_lane"|"none",
 "vehicle_entered_rider_path": true|false|null,
 "rider_moved_into_vehicle_path": true|false|null,
 "signal_for_rider": "red"|"green"|"yellow"|"none_visible"|"unknown",
 "rider_entered_on_red": true|false|null,
 "fault": "driver"|"rider"|"shared"|"unclear"|"none",
 "evidence": "one sentence"}
"separator" means a physical barrier between the rider and the vehicle (posts, curb, parked cars); a painted line is not a separator."""
HINT = ("You are reviewing a 5-second clip from a cyclist's helmet camera. A detector flagged a {label} on the {side} "
        "edge at about {t} s, filling {h}% of the frame height, which then moved ahead of the rider. Look closely at "
        "that vehicle around that time. Describe THAT vehicle's actual colour and type. Did it stay in its own lane, "
        "or move into the rider's path? Did the rider move into its path?\n")
STILL = ("What colour is the traffic signal facing the camera's direction of travel? "
         "Answer red, green, yellow, none_visible or unknown.")


def backend_token():
    r = urllib.request.Request(B + '/api/v1/auth/login', headers={'Content-Type': 'application/json'},
                               data=json.dumps({'username': cfg['USERNAME'], 'password': cfg['PASSWORD']}).encode())
    return json.load(urllib.request.urlopen(r, timeout=60))['access_token']


def model_id():
    return json.load(urllib.request.urlopen(urllib.request.Request(GPU + '/v1/models', headers=GH), timeout=30))['data'][0]['id']


def cosmos(model, media, text, max_tokens=600):
    body = {'model': model, 'temperature': 0, 'max_tokens': max_tokens,
            'messages': [{'role': 'user', 'content': [media, {'type': 'text', 'text': text}]}]}
    r = urllib.request.Request(GPU + '/v1/chat/completions', data=json.dumps(body).encode(), headers=GH)
    return json.load(urllib.request.urlopen(r, timeout=240))['choices'][0]['message']['content']


def parse_json(s):
    m = re.search(r'\{.*\}', s, re.S)
    return json.loads(m.group(0)) if m else None


def confirm(seg, geo, tok, model, tmp):
    key = seg['_key']
    raw = os.path.join(tmp, key + '.mp4')
    url = B + '/api/v1/videos/stream?' + urllib.parse.urlencode({'source': seg['source'], 'token': tok})
    with urllib.request.urlopen(url, timeout=120) as r:
        open(raw, 'wb').write(r.read())
    small = os.path.join(tmp, key + '_1280.mp4')
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', raw, '-an', '-vf', 'scale=1280:-2', '-r', '6', small], check=True)
    g = (geo or {}).get('geometry')
    shape = (geo or {}).get('pass') or (geo or {}).get('cut')
    if shape:  # the shape's track is the one the flag is about; geometry's "most threatening" can be another car
        g = {'label': shape['label'], 'side': shape.get('side') or g['side'], 't_peak': shape.get('t', shape.get('t1')),
             'peak_h': shape.get('peak_h', shape.get('h'))}
    hint = HINT.format(label=g['label'], side=g['side'], t=g['t_peak'], h=round(g['peak_h'] * 100)) if g else \
        "You are reviewing a 5-second clip from a cyclist's helmet camera.\n"
    t0 = time.time()
    video = {'type': 'video_url', 'video_url': {'url': 'data:video/mp4;base64,' + base64.b64encode(open(small, 'rb').read()).decode()}}
    text = cosmos(model, video, hint + SCHEMA)
    t_still = max(0.0, (g['t_peak'] if g else 2.5) - 1.0)
    jpg = os.path.join(tmp, key + '.jpg')
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', str(t_still), '-i', raw, '-frames:v', '1', jpg], check=True)
    still = cosmos(model, {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(open(jpg, 'rb').read()).decode()}},
                   STILL, max_tokens=20)
    sig = next((w for w in ('none_visible', 'unknown', 'red', 'green', 'yellow') if w in still.lower()), 'unknown')
    return {'id': key, 'hint': hint.strip(), 'facts': parse_json(text), 'raw': text,
            'still_t': round(t_still, 1), 'still_signal': sig, 'still_raw': still.strip(), 'secs': round(time.time() - t0, 1)}


def main(keys, out):
    segs = {os.path.basename(s['source']).split('_', 2)[2].rsplit('_of_', 1)[0]: s
            for s in json.load(open(os.path.join(DATA, 'segments.json')))}
    geo = {r['id']: r for r in json.load(open(os.path.join(DATA, 'geometry.json')))['rows']}
    tok, model = backend_token(), model_id()
    prev = {r['id']: r for r in json.load(open(out))['rows']} if os.path.exists(out) else {}
    with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(6) as ex:
        jobs = {k: ex.submit(confirm, {**segs[k], '_key': k}, geo.get(k), tok, model, tmp) for k in keys if k in segs}
        for k in keys:
            if k not in jobs:
                print('no segment:', k, file=sys.stderr)
                continue
            try:
                prev[k] = jobs[k].result()
                f = prev[k]['facts'] or {}
                print(k, prev[k]['secs'], 's', f.get('event'), f.get('signal_for_rider'), 'still:', prev[k]['still_signal'],
                      '|', f.get('evidence'))
            except Exception as e:
                print(k, 'ERROR', e, file=sys.stderr)
    json.dump({'stage': 'C', 'model': model, 'rows': list(prev.values())}, open(out, 'w'), indent=1)
    print(len(prev), 'rows ->', out)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('keys', nargs='+')
    ap.add_argument('--out', default=os.path.join(DATA, 'stage_c.json'))
    a = ap.parse_args()
    main(a.keys, a.out)
