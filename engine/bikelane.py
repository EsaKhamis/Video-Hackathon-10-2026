"""Stage F: vehicles stopped or parked in the bike lane, and who they are.

Part 1, detection (every helmet-cam segment):
  1. download, ffmpeg -> 1280 px @ 6 fps (same as Stage C), one Cosmos call with the bike-lane schema below
  2. a "yes" gets two more calls; the field-by-field majority of the three decides
Part 2, identification (confirmed segments only):
  3. the pipeline's YOLO tracks: the vehicle track on the reported side near the reported time, and the
     5 frames where its box is biggest (closest, so the plate is largest)
  4. each box is cropped from the full-resolution frame (20% padding) and saved to data/bikelane/
  5. one Cosmos call per crop: type, colour, make, model, plate. A field is kept only when most crops agree;
     a plate needs 3 matching reads (2 if only 2-3 crops), otherwise "unreadable". Cosmos will happily invent
     a plausible plate from a blurry crop, and a wrong plate in a report accuses the wrong driver.

Verdict (plain Python, as in Stage D): "driver" only if the vote says a vehicle is stopped in a visible bike lane
and is not moving with traffic; anything missing or unknown is "unclear". The cyclist checks the crops before
anything is sent. Writes data/stage_f.json. Stdlib + ffmpeg; runs on the VM.

  python3 engine/bikelane.py                       # all 465 segments (~7 min, 6-way)
  python3 engine/bikelane.py GX050001_chunk_0013_segment_004 ...   # these segments, merged into stage_f.json
"""
import argparse, base64, collections, json, math, os, re, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from confirm import MODEL, ask, download, majority, parse
from detect import VEH, detections, tracks_of
import wb_trace as trace

INTRO = "You are reviewing a 5-second clip from a cyclist's helmet camera."
SCHEMA = """Is a motor vehicle stopped or parked IN the bike lane (on the painted or green bike lane, blocking it)?
A vehicle in the next traffic lane, in a parking lane beside the bike lane, or moving with traffic does not count.
Answer ONLY with JSON:
{"bike_lane_visible": true|false,
 "rider_position": "bike_lane"|"traffic_lane"|"sidewalk"|"crosswalk"|"unknown",
 "vehicle_in_bike_lane": true|false,
 "vehicle_moving": true|false|null,
 "vehicle": {"type": "car|taxi|van|suv|truck|bus|motorcycle|none", "colour": "<colour you see>", "side": "left"|"right"|"front"|"none"},
 "time_sec": number,
 "rider_left_lane_to_pass": true|false|null,
 "driver_visible": true|false|null,
 "hazard_lights": true|false|null,
 "evidence": "one sentence"}"""

ID = """This is a crop of one vehicle from a cyclist's camera. Describe the vehicle. Answer ONLY with JSON:
{"type": "car|taxi|van|suv|truck|bus|motorcycle",
 "colour": "<main body colour>",
 "make": "<manufacturer, or unknown>",
 "model": "<model, or unknown>",
 "plate_visible": true|false,
 "plate": "<characters exactly as you can read them, or unreadable>"}
Only write plate characters you can actually read. If any character is unclear, answer unreadable."""

OUT_DIR = os.path.join(ROOT, 'data', 'bikelane')


def video_call(small):
    video = base64.b64encode(open(small, 'rb').read()).decode()
    return parse(ask([{'type': 'video_url', 'video_url': {'url': 'data:video/mp4;base64,' + video}},
                      {'type': 'text', 'text': INTRO + '\n' + SCHEMA}]))


def target_frames(source, facts, n=5):
    """The vehicle's YOLO track near the reported time and side, and its n biggest boxes (normalised)."""
    d = detections(source)
    H, W = (json.loads(d['video_shape']) if isinstance(d['video_shape'], str) else d['video_shape'])[:2]
    t = facts.get('time_sec') if isinstance(facts.get('time_sec'), (int, float)) else 2.5
    side = (facts.get('vehicle') or {}).get('side')
    best, best_h = None, 0
    for tr in tracks_of(d['frames']):
        near = [(ts, b) for ts, b in tr['pts'] if abs(ts - t) <= 2.0]
        if not near:
            continue
        cx = sum((b[0] + b[2]) / 2 for _, b in near) / len(near) / W
        if (side == 'left' and cx > 0.6) or (side == 'right' and cx < 0.4) or (side == 'front' and not 0.2 < cx < 0.8):
            continue
        h = max((b[3] - b[1]) / H for _, b in tr['pts'])
        if h > best_h:
            best, best_h = tr, h
    if not best:
        return []
    pts, picked = sorted(best['pts'], key=lambda p: p[1][3] - p[1][1], reverse=True), []
    for ts, b in pts:  # biggest boxes, at least 0.2 s apart so the reads are not the same frame
        if all(abs(ts - p[0]) >= 0.2 for p in picked):
            picked.append((ts, [b[0] / W, b[1] / H, b[2] / W, b[3] / H]))
        if len(picked) == n:
            break
    return [{'t': round(ts, 2), 'bbox': [round(v, 3) for v in b], 'label': best['label']} for ts, b in picked]


def crop(raw, t, bbox, path, pad=0.2):
    x1, y1, x2, y2 = bbox
    w, h = x2 - x1, y2 - y1
    x1, y1 = max(0.0, x1 - pad * w), max(0.0, y1 - pad * h)
    x2, y2 = min(1.0, x2 + pad * w), min(1.0, y2 + pad * h)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', str(t), '-i', raw, '-frames:v', '1', '-vf',
                    f'crop=iw*{x2 - x1:.4f}:ih*{y2 - y1:.4f}:iw*{x1:.4f}:ih*{y1:.4f}', '-q:v', '2', path], check=True)


def norm_plate(p):
    p = re.sub(r'[^A-Z0-9]', '', str(p or '').upper())
    return p if 4 <= len(p) <= 8 and p != 'UNREADABLE' else None


def agree(vals, need):
    vals = [v for v in vals if v and str(v).lower() not in ('unknown', 'none', 'unreadable')]
    if not vals:
        return None, 0
    v, c = collections.Counter(vals).most_common(1)[0]
    return (v, c) if c >= need else (None, c)


def identify(raw, sid, facts):
    frames = target_frames(row_source[sid], facts)
    reads = []
    for k, f in enumerate(frames):
        path = os.path.join(OUT_DIR, f'{sid}_{k}.jpg')
        crop(raw, f['t'], f['bbox'], path)
        img = base64.b64encode(open(path, 'rb').read()).decode()
        r = parse(ask([{'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + img}},
                       {'type': 'text', 'text': ID}], max_tokens=300)) or {}
        reads.append({**f, 'crop': os.path.relpath(path, ROOT), **{k2: r.get(k2) for k2 in
                      ('type', 'colour', 'make', 'model', 'plate_visible', 'plate')}})
    n = len(reads)
    need = max(1, n // 2 + 1)
    low = lambda key: [str(r.get(key) or '').strip().lower() or None for r in reads]
    plates = [norm_plate(r.get('plate')) for r in reads]
    plate, plate_n = agree(plates, 3 if n >= 4 else 2)
    out = {'yolo_label': frames[0]['label'] if frames else None, 'n_crops': n,
           'plate': plate or 'unreadable', 'plate_reads': plates, 'plate_agree': plate_n, 'reads': reads}
    for key in ('type', 'colour', 'make', 'model'):
        out[key] = agree(low(key), need)[0] or 'unknown'
    if plate:  # the crop where the agreed plate was read: shown first in the review screen
        out['best_crop'] = reads[plates.index(plate)]['crop']
    elif reads:
        out['best_crop'] = reads[0]['crop']
    return out


def verdict(f):
    if not f or f.get('vehicle_in_bike_lane') is not True:
        return 'none'
    if f.get('bike_lane_visible') is True and f.get('vehicle_moving') is False:
        return 'driver'
    return 'unclear'


@trace.op(name='bikelane_segment', call_display_name=lambda c: f"bikelane {c.inputs['row'].get('id')}",
          postprocess_inputs=lambda i: {'row': {k: i['row'].get(k) for k in ('id', 'source')}, 'votes': i['votes']})
def run(row, votes):
    sid = row['id']
    out = {'id': sid, 'ride': row.get('ride'), 'source': row['source'], 'ride_offset_sec': row.get('ride_offset_sec')}
    t0 = time.time()
    with tempfile.TemporaryDirectory() as d:
        raw, small = os.path.join(d, 'raw.mp4'), os.path.join(d, 'small.mp4')
        try:
            download(row['source'], raw)
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', raw, '-vf', 'scale=1280:-2,fps=6', '-an',
                            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '28', small], check=True)
            answers = [video_call(small)]
            if (answers[0] or {}).get('vehicle_in_bike_lane') is True and votes > 1:
                answers += [video_call(small) for _ in range(votes - 1)]
            facts = majority(answers)
            out.update(facts=facts, votes=[(a or {}).get('vehicle_in_bike_lane') for a in answers],
                       verdict=verdict(facts))
            if out['verdict'] != 'none':
                out['vehicle_id'] = identify(raw, sid, facts)
        except Exception as e:
            out['err'] = str(e)[:200]
    out['sec'] = round(time.time() - t0, 1)
    return out


row_source = {}

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('keys', nargs='*', help='segment ids; merged into an existing --out')
    ap.add_argument('--geometry', default=os.path.join(ROOT, 'data', 'geometry.json'))
    ap.add_argument('--out', default=os.path.join(ROOT, 'data', 'stage_f.json'))
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--votes', type=int, default=3)
    ap.add_argument('--limit', type=int, default=0, help='first N segments only (smoke test)')
    a = ap.parse_args()
    trace.init()
    os.makedirs(OUT_DIR, exist_ok=True)
    rows = [r for r in json.load(open(a.geometry))['rows'] if r.get('source')]
    row_source.update({r['id']: r['source'] for r in rows})
    if a.keys:
        rows = [r for r in rows if r['id'] in set(a.keys)]
    if a.limit:
        rows = rows[:a.limit]
    print(f'Stage F: {len(rows)} segments, {a.workers}-way, model {MODEL}', flush=True)
    t = time.time()
    done = []
    with ThreadPoolExecutor(a.workers) as ex:
        for r in ex.map(lambda r: run(r, a.votes), rows):
            done.append(r)
            if r.get('verdict') not in (None, 'none'):
                v = r.get('vehicle_id') or {}
                print(f"  {r['id']:34} {r['verdict']:8} {v.get('colour')} {v.get('make')} {v.get('model')} "
                      f"{v.get('type')} plate={v.get('plate')} {v.get('plate_reads')}", flush=True)
            if len(done) % 25 == 0:
                print(f'  {len(done)}/{len(rows)} in {round(time.time() - t)} s', flush=True)
    if a.keys and os.path.exists(a.out):
        new = {r['id']: r for r in done}
        done = [new.pop(r['id'], r) for r in json.load(open(a.out))['rows']] + list(new.values())
    errs = [r for r in done if 'err' in r]
    counts = collections.Counter(r.get('verdict', 'error') for r in done)
    json.dump({'stage': 'F', 'model': MODEL, 'votes': a.votes, 'count': len(done), 'errors': len(errs),
               'verdicts': counts, 'rows': done}, open(a.out, 'w'), indent=1)
    print(f'{len(done) - len(errs)}/{len(done)} ok in {round(time.time() - t)} s, {dict(counts)} -> {a.out}')
