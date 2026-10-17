"""Stage C: Cosmos3-Reason confirms each flagged segment with a detector hint (PLAN.md "Prompts").

For every candidate (Stage B flags, the labelled clips, or a list of ids):
  1. download the segment from the backend, ffmpeg -> 1280 px @ 6 fps, no audio (~0.6 MB)
  2. Cosmos on the video with the facts schema, blind (no detector hint; --hint to add one)
  3. Cosmos on a full-resolution still 1 s before the peak: the traffic signal facing the rider

Blind, not hinted: on the 18 labelled clips a detector hint made Cosmos agree with it (near miss + driver on
almost every clip, including adjacent-lane, sidewalk and separated ones). Blind, it rejected H2, H5 and the
adjacent-lane passes and still caught H1 and H4. Fault comes from the rules in build_incidents.py, which
pair these facts with the Stage B geometry; Cosmos's own `fault` is stored, never used.
Writes data/stage_c.json. Stdlib + ffmpeg; runs on the VM.

  python3 engine/confirm.py                               # every Stage B flag + labelled clips (~3 min)
  python3 engine/confirm.py --only-labels                 # labelled clips, prints a check table
  python3 engine/confirm.py GOPR0130_chunk_0026_segment_003 ...   # these segments, merged into stage_c.json
  python3 probe/eval_labels.py                            # score the rules in engine/fault.py against the labels

Rows keep the field names engine/fault.py and probe/eval_labels.py read: facts, still_signal, still_t, hint.
"""
import argparse, base64, collections, csv, glob, json, os, re, subprocess, sys, tempfile, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, os.path.join(ROOT, 'probe'))
from vss_client import B, tok
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wb_trace as trace  # noqa: E402

cfg = {}
for l in open(glob.glob('/config/*.config')[0]):
    if '=' in l and not l.startswith('#'):
        k, v = l.strip().split('=', 1)
        cfg[k] = v.strip('"\'')
COSMOS = os.environ.get('COSMOS_URL', 'http://166.19.38.112:8001')
CH = {'Authorization': 'Bearer ' + cfg['GPU_BEARER_TOKEN'], 'Content-Type': 'application/json'}
MODEL = json.load(urllib.request.urlopen(urllib.request.Request(COSMOS + '/v1/models', headers=CH)))['data'][0]['id']

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

INTRO = "You are reviewing a 5-second clip from a cyclist's helmet camera."
HINT = ("A detector flagged a {label} on the {side} at about {t} s, filling {h}% of the frame height{after}. "
        "Look closely at that vehicle around that time. Describe THAT vehicle's actual colour and type. "
        "Did it stay in its own lane, or move into the rider's path? Did the rider move into its path?")
SIGNAL = ("What colour is the traffic signal facing the camera's direction of travel? "
          "Answer with one word: red, green, yellow, none_visible or unknown.")


@trace.op(name='cosmos_ask', postprocess_inputs=lambda i: {**i, 'content': trace.strip_media(i['content'])})
def ask(content, max_tokens=500, tries=3):
    body = {'model': MODEL, 'temperature': 0, 'max_tokens': max_tokens,
            'messages': [{'role': 'user', 'content': content}]}
    for n in range(tries):
        try:
            r = json.load(urllib.request.urlopen(urllib.request.Request(
                COSMOS + '/v1/chat/completions', data=json.dumps(body).encode(), headers=CH), timeout=240))
            return r['choices'][0]['message']['content']
        except (urllib.error.URLError, TimeoutError) as e:
            if n == tries - 1:
                raise
            time.sleep(2 * 2 ** n)


def parse(text):
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.S)
    m = re.search(r'\{.*\}', text, flags=re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def download(source, path, tries=4):
    url = B + '/api/v1/videos/stream?' + urllib.parse.urlencode({'source': source, 'token': tok})  # token in the query
    for n in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=120) as r, open(path, 'wb') as f:
                f.write(r.read())
            return
        except Exception:
            if n == tries - 1:
                raise
            time.sleep(1.5 * 2 ** n)


def majority(answers):
    """Field-by-field vote over several Cosmos answers (it is not deterministic, even at temperature 0)."""
    answers = [a for a in answers if a]
    if not answers:
        return None
    out = {}
    for k in answers[0]:
        vals = [a.get(k) for a in answers]
        if isinstance(vals[0], dict):
            out[k] = majority([v for v in vals if isinstance(v, dict)])
        else:
            out[k] = collections.Counter(json.dumps(v) for v in vals).most_common(1)[0][0]
            out[k] = json.loads(out[k])
    return out


def signal_word(text):
    w = re.findall(r'none_visible|red|green|yellow|unknown', re.sub(r'<think>.*?</think>', '', text, flags=re.S).lower())
    return w[-1] if w else 'unknown'


def hint_for(row):
    g = row.get('geometry')
    if not g:
        return ''
    after = ''
    if row.get('pass'):
        after = ', which then moved ahead of the rider'
    elif row.get('cut') and not row['cut'].get('pan_suspect'):
        after = ', which then moved sideways across the rider\'s view'
    return HINT.format(label=g['label'], side=g['side'] + ' side', t=g['t_peak'],
                       h=round(g['peak_h'] * 100), after=after) + '\n'


@trace.op(name='confirm_segment', call_display_name=lambda c: f"confirm {c.inputs['row'].get('id')}",
          postprocess_inputs=lambda i: {**i, 'row': {k: i['row'].get(k) for k in ('id', 'source', 'flag_reasons')}})
def confirm(row, hinted=False, votes=1):
    out = {'id': row['id'], 'source': row['source'], 'still_t': None, 'hint': ''}
    t0 = time.time()
    with tempfile.TemporaryDirectory() as d:
        raw, small, still = (os.path.join(d, n) for n in ('raw.mp4', 'small.mp4', 'still.jpg'))
        try:
            download(row['source'], raw)
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', raw, '-vf', 'scale=1280:-2,fps=6', '-an',
                            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '28', small], check=True)
            g = row.get('geometry')
            t_still = max(0.0, (g['t_peak'] if g else 2.5) - 1.0)
            out['still_t'] = round(t_still, 1)
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', str(t_still), '-i', raw, '-frames:v', '1',
                            '-q:v', '3', still], check=True)
            video = base64.b64encode(open(small, 'rb').read()).decode()
            hint = hint_for(row) if hinted else ''
            out['hint'] = hint.strip()
            texts = [ask([{'type': 'video_url', 'video_url': {'url': 'data:video/mp4;base64,' + video}},
                          {'type': 'text', 'text': INTRO + '\n' + hint + SCHEMA}]) for _ in range(votes)]
            img = base64.b64encode(open(still, 'rb').read()).decode()
            sigs = [ask([{'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + img}},
                         {'type': 'text', 'text': SIGNAL}], max_tokens=200) for _ in range(votes)]
        except Exception as e:
            return {**out, 'err': str(e)[:200]}
    parsed = [parse(t) for t in texts]
    facts = majority(parsed)
    words = [signal_word(t) for t in sigs]
    res = {**out, 'facts': facts,
           'still_signal': collections.Counter(words).most_common(1)[0][0],
           'raw': texts[0] if facts is None else None, 'sec': round(time.time() - t0, 1)}
    if votes > 1:
        res['votes'] = {'n': votes, 'near_miss': [p.get('near_miss') if p else None for p in parsed],
                        'entered_path': [p.get('vehicle_entered_rider_path') if p else None for p in parsed],
                        'rider_position': [p.get('rider_position') if p else None for p in parsed],
                        'still_signal': words}
    return res


def check(res, labels):
    print(f"\n{'segment':32} {'label':13} {'hero':4} nm    event          veh             dist      pos          sep  action               v>r   r>v   sig/still     fault")
    for r in sorted(res, key=lambda r: labels[r['id']]['label'] if r['id'] in labels else 'z'):
        lab = labels.get(r['id'], {})
        f = r.get('facts') or {}
        v = f.get('vehicle') or {}
        if 'err' in r or not f:
            print(f"{r['id'][:32]:32} {lab.get('label', ''):13} ERROR {r.get('err') or (r.get('raw') or '')[:80]}")
            continue
        print(f"{r['id'][:32]:32} {lab.get('label', ''):13} {lab.get('hero', ''):4} {str(f.get('near_miss'))[:5]:5} "
              f"{str(f.get('event'))[:14]:14} {(str(v.get('colour')) + ' ' + str(v.get('type')) + '/' + str(v.get('side')))[:15]:15} "
              f"{str(f.get('closest_distance'))[:9]:9} {str(f.get('rider_position'))[:12]:12} {str(f.get('separator'))[:4]:4} "
              f"{str(f.get('vehicle_action'))[:20]:20} {str(f.get('vehicle_entered_rider_path'))[:5]:5} "
              f"{str(f.get('rider_moved_into_vehicle_path'))[:5]:5} {str(f.get('signal_for_rider'))[:5]}/{r['still_signal'][:5]:7} "
              f"{f.get('fault')}")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--geometry', default=os.path.join(ROOT, 'data', 'geometry.json'))
    ap.add_argument('--labels', default=os.path.join(ROOT, 'probe', 'labels.csv'))
    ap.add_argument('--only-labels', action='store_true', help='only the labelled clips')
    ap.add_argument('keys', nargs='*', help='segment ids; merged into an existing --out')
    ap.add_argument('--out', default=os.path.join(ROOT, 'data', 'stage_c.json'))
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--votes', type=int, default=5, help='Cosmos calls per close-pass candidate (majority vote)')
    ap.add_argument('--vote-all', action='store_true', help='vote on every segment in this run, not just close passes')
    ap.add_argument('--merge', action='store_true', help='replace these rows in an existing --out file')
    ap.add_argument('--hint', action='store_true', help='add the Stage B detector hint (biases Cosmos toward yes)')
    a = ap.parse_args()
    trace.init()
    geo = {r['id']: r for r in json.load(open(a.geometry))['rows'] if 'err' not in r}
    labels = {r['segment']: r for r in csv.DictReader(open(a.labels))}
    if a.keys:
        want, a.merge = set(a.keys), True
    elif a.only_labels:
        want = set(labels)
    else:
        want = {k for k, r in geo.items() if r.get('flag')} | set(labels)
    rows = [geo[k] for k in sorted(want) if k in geo]
    print(f'Stage C: {len(rows)} segments, {a.workers}-way, model {MODEL}', flush=True)
    t = time.time()
    with ThreadPoolExecutor(a.workers) as ex:
        # driver candidates (Stage B close-pass shape, not a camera pan) get a majority vote
        res = list(ex.map(lambda r: confirm(r, a.hint, a.votes if a.vote_all or (r.get('pass') and not
                                            (r.get('geometry') or {}).get('pan_suspect')) else 1), rows))
        # one call says "sidewalk" or "red" too easily: re-ask those with a vote (fault.py rules 3 and 4)
        again = {r['id'] for r in res if 'votes' not in r and ((r.get('facts') or {}).get('rider_position') == 'sidewalk'
                                                                or r.get('still_signal') == 'red')}
        if again and a.votes > 1:
            redo = {r['id']: r for r in ex.map(lambda r: confirm(r, a.hint, a.votes), [geo[k] for k in sorted(again)])}
            res = [redo.get(r['id'], r) for r in res]
    if a.merge and os.path.exists(a.out):
        new = {r['id']: r for r in res}
        res = [new.pop(r['id'], r) for r in json.load(open(a.out))['rows']] + list(new.values())
    errs = [r for r in res if 'err' in r or not r.get('facts')]
    json.dump({'stage': 'C', 'model': MODEL, 'hinted': a.hint, 'count': len(res), 'errors': len(errs), 'rows': res},
              open(a.out, 'w'), indent=1)
    print(f'{len(res) - len(errs)}/{len(res)} ok in {round(time.time() - t)} s -> {a.out}')
    check([r for r in res if r['id'] in labels] if not a.keys else [r for r in res if r['id'] in want], labels)
