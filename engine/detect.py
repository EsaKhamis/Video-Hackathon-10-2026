"""Stage B: YOLO geometry for every helmet-cam segment (PLAN.md, "Near-miss definition").

Reads the pipeline's per-frame YOLO boxes (/api/v1/videos/detections), tracks vehicles, and for the
most threatening vehicle in each segment measures: how big it got (closeness), which side, whether its
inner edge entered the rider corridor (encroachment), how fast it moved sideways toward the rider net of
camera motion (as a lateral gap in metres, from box size and a typical vehicle height), and whether the whole scene swept sideways (the rider turning or swerving).

Also keeps the close-pass and cut-in shapes from probe/scan_nearmiss.py.
Stdlib only; runs on the VM or in the engine Job.

  python3 engine/detect.py                         # all 465 segments -> data/geometry.json
  python3 engine/detect.py --labels probe/labels.csv   # only the labelled clips, then print a check
"""
import argparse, json, os, statistics, sys, time, urllib.parse
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'probe'))
from vss_client import req, tok

VEH = ('car', 'truck', 'bus', 'motorcycle')
# Lateral gap: distance from the vehicle's near edge to the rider's line of travel, in metres.
# Pinhole camera: lateral_m = (x - cx) / box_height_px * vehicle_height_m, whatever the distance.
VEH_H_M = {'car': 1.5, 'truck': 3.0, 'bus': 3.0, 'motorcycle': 1.4}
CORRIDOR_M = 0.5            # near edge within 0.5 m of the rider's line = in the rider corridor
ENTER_M = 0.5               # ...having closed the gap by at least 0.5 m (it came in, not just there)
MIN_H = 0.15                # ignore tracks that never reach 15% of frame height
CLOSE_H = 0.30              # definition v1 condition 3
LATERAL_MIN = 1.0           # m / s toward the rider's line (best 0.5 s window); tuned on probe/labels.csv
PAN_MIN = 0.10              # scene shift (frame widths) that counts as the rider turning
CHUNK_SEC, SEG_SEC = 30, 5


def detections(source, tries=5):
    """The shared ingress returns 503 under load; back off and retry."""
    for n in range(tries):
        try:
            return req('/api/v1/videos/detections?source=' + urllib.parse.quote(source, safe=''), None, tok)
        except Exception:
            if n == tries - 1:
                raise
            time.sleep(1.5 * 2 ** n)


def seg_id(filename):
    return filename[16:].rsplit('_of_', 1)[0]


def iou(a, b):
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    i = max(0, x2 - x1) * max(0, y2 - y1)
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i
    return i / u if u else 0


def tracks_of(frames):
    tracks, live = [], []
    for f in frames:
        dets = [d for d in f['detections'] if d['label'] in VEH and d['confidence'] > 0.45]
        used, nxt = set(), []
        for tr in live:
            best, bi = 0.3, None
            for i, d in enumerate(dets):
                if i in used:
                    continue
                v = iou(tr['pts'][-1][1], d['bbox'])
                if v > best:
                    best, bi = v, i
            if bi is not None:
                used.add(bi)
                tr['pts'].append((f['time_sec'], dets[bi]['bbox']))
                nxt.append(tr)
            elif f['time_sec'] - tr['pts'][-1][0] < 0.3:
                nxt.append(tr)
        for i, d in enumerate(dets):
            if i not in used:
                tr = {'label': d['label'], 'pts': [(f['time_sec'], d['bbox'])]}
                tracks.append(tr)
                nxt.append(tr)
        live = nxt
    return [t for t in tracks if len(t['pts']) >= 8]


def centre_at(t, a, b, W):
    seg = [p for p in t['pts'] if a <= p[0] <= b]
    if len(seg) < 2:
        return None
    return ((seg[-1][1][0] + seg[-1][1][2]) - (seg[0][1][0] + seg[0][1][2])) / 2 / W


def scene_shift(trs, skip, a, b, W):
    """Median sideways shift of the other tracks over [a, b]: camera pan when the rider turns."""
    moves = [m for t in trs if t is not skip for m in [centre_at(t, a, b, W)] if m is not None]
    if len(moves) < 2:
        return 0.0, False
    med = statistics.median(moves)
    same = sum(1 for m in moves if m * med > 0 and abs(m) > PAN_MIN)
    return med, abs(med) > PAN_MIN and same / len(moves) > 0.6


def track_geometry(t, trs, W, H):
    P = t['pts']
    hs = [(b[3] - b[1]) / H for _, b in P]
    k = max(range(len(P)), key=lambda i: hs[i])
    if hs[k] < MIN_H:
        return None
    tk, bk = P[k]
    side = 'left' if (bk[0] + bk[2]) / 2 / W < 0.5 else 'right'

    hm = VEH_H_M[t['label']]

    def gap(b, h):  # near edge to the rider's line (image centre), metres; 0 once it crosses
        g = (W / 2 - b[2]) if side == 'left' else (b[0] - W / 2)
        return max(g, 0) / (h * H) * hm

    G = [(tt, gap(b, h)) for (tt, b), h in zip(P, hs) if h >= MIN_H]
    min_gap = min(g for _, g in G)
    gap0 = G[0][1]
    gap_peak = next(g for tt, g in G if tt >= tk)
    # encroachment: the near edge reaches the rider corridor after starting further out
    entered = min_gap < CORRIDOR_M and gap0 - min_gap >= ENTER_M

    # how fast the gap closes, best 0.5 s window, corrected for camera pan (pan in frame widths -> metres)
    lateral = 0.0
    for i in range(len(G)):
        j = next((j for j in range(i + 1, len(G)) if G[j][0] - G[i][0] >= 0.5), None)
        if j is None:
            break
        pan, _ = scene_shift(trs, t, G[i][0], G[j][0], W)
        h_ref = hs[min(range(len(P)), key=lambda n: abs(P[n][0] - G[j][0]))]
        pan_m = pan * W / (h_ref * H) * hm
        closing = (G[i][1] - G[j][1]) - (pan_m if side == 'left' else -pan_m)
        lateral = max(lateral, closing / (G[j][0] - G[i][0]))

    # approach: fastest growth in box height in the second before the peak
    before = [i for i in range(k) if tk - P[i][0] <= 1.0]
    approach = max(((hs[k] - hs[i]) / (tk - P[i][0]) for i in before if tk > P[i][0]), default=0.0)

    _, pan = scene_shift(trs, t, tk - 1.0, tk + 0.5, W)
    return {
        'label': t['label'], 'side': side, 't_peak': round(tk, 2), 'peak_h': round(hs[k], 3),
        'bbox': [round(bk[0] / W, 3), round(bk[1] / H, 3), round(bk[2] / W, 3), round(bk[3] / H, 3)],
        'gap_start_m': round(gap0, 2), 'min_gap_m': round(min_gap, 2), 'gap_at_peak_m': round(gap_peak, 2),
        'entered_corridor': entered, 'lateral_toward_rider': round(lateral, 2),
        'approach': round(approach, 3), 'pan_suspect': pan, 'rider_manoeuvre': pan,
        'size_steady': max(hs) - min(hs) < 0.1,
    }


def shapes(trs, W, H):
    """Close-pass and cut-in shapes, unchanged from probe/scan_nearmiss.py."""
    best_pass, best_cut = None, None
    for t in trs:
        P = t['pts']
        hs = [(b[3] - b[1]) / H for _, b in P]
        cx = [((b[0] + b[2]) / 2) / W for _, b in P]
        k = max(range(len(P)), key=lambda i: hs[i])
        hmax, (tk, bk) = hs[k], P[k]
        at_edge = bk[0] < 0.04 * W or bk[2] > 0.96 * W
        side = 'left' if cx[k] < 0.5 else 'right'
        after = [i for i in range(k, len(P)) if P[i][0] - tk <= 2.0]
        if hmax >= 0.3 and at_edge and len(after) >= 6:
            j = after[-1]
            shrink = 1 - hs[j] / hmax
            inward = (cx[j] - cx[k]) if side == 'left' else (cx[k] - cx[j])
            born_edge = P[0][1][0] < 0.04 * W or P[0][1][2] > 0.96 * W
            if shrink > 0.25 and inward > 0.05 and born_edge:
                s = hmax * shrink * (1 + inward)
                if not best_pass or s > best_pass['score']:
                    best_pass = {'score': round(s, 3), 't': round(tk, 2), 'label': t['label'], 'side': side,
                                 'peak_h': round(hmax, 2), 'shrink': round(shrink, 2), 'inward': round(inward, 2)}
        for i in range(len(P)):
            for j in range(i + 1, len(P)):
                dt = P[j][0] - P[i][0]
                if dt > 1.5:
                    break
                if min(hs[i], hs[j]) < 0.15 or dt < 0.3:
                    continue
                dx = cx[j] - cx[i]
                if abs(dx) >= 0.25 and (cx[i] - 0.5) * (cx[j] - 0.5) < 0:
                    s = abs(dx) / dt * statistics.mean(hs[i:j + 1])
                    if not best_cut or s > best_cut['score']:
                        best_cut = {'score': round(s, 3), 't0': round(P[i][0], 2), 't1': round(P[j][0], 2),
                                    'label': t['label'], 'dx': round(dx, 2), 'h': round(hs[j], 2), '_t': t}
    if best_cut:
        _, best_cut['pan_suspect'] = scene_shift(trs, best_cut.pop('_t'), best_cut['t0'], best_cut['t1'], W)
    return best_pass, best_cut


def threat(g):
    """Rank tracks: closeness, boosted when the vehicle moves toward the rider."""
    return g['peak_h'] * (1 + (1 if g['entered_corridor'] else 0) + min(g['lateral_toward_rider'], 2) / 2)


def analyse(row):
    sid = seg_id(row['filename'])
    chunk = int(sid.split('_chunk_')[1].split('_')[0])
    seg = int(row.get('segment_number') or sid.rsplit('_', 1)[1])
    out = {'id': sid, 'ride': sid.split('_chunk')[0], 'source': row['source'], 'filename': row['filename'],
           'ride_offset_sec': chunk * CHUNK_SEC + (seg - 1) * SEG_SEC}
    try:
        d = detections(row['source'])
    except Exception as e:
        return {**out, 'err': str(e)[:120]}
    shape = d['video_shape']
    H, W = (json.loads(shape) if isinstance(shape, str) else shape)[:2]
    trs = tracks_of(d['frames'])
    geoms = [g for g in (track_geometry(t, trs, W, H) for t in trs) if g]
    geoms.sort(key=threat, reverse=True)
    best_pass, best_cut = shapes(trs, W, H)
    g = geoms[0] if geoms else None
    encroach = bool(g and g['entered_corridor'] and g['lateral_toward_rider'] >= LATERAL_MIN)
    reasons = []
    if best_pass:
        reasons.append('close_pass_shape')
    if best_cut and not best_cut['pan_suspect']:
        reasons.append('cut_in_shape')
    if g and g['peak_h'] >= CLOSE_H and encroach:
        reasons.append('close_and_encroaching')
    adjacent = bool(g and g['peak_h'] >= CLOSE_H and not encroach)
    return {**out, 'n_tracks': len(trs), 'geometry': g, 'runner_up': geoms[1] if len(geoms) > 1 else None,
            'pass': best_pass, 'cut': best_cut, 'encroachment': encroach, 'adjacent_lane_like': adjacent,
            'flag': bool(reasons), 'flag_reasons': reasons}


def check_labels(res, labels_path):
    import csv
    labels = {r['segment']: r for r in csv.DictReader(open(labels_path))}
    print(f"\n{'segment':34} {'label':14} {'hero':4} flag  enc   peak_h gap_m lat   side  pan   reasons")
    real = {'driver', 'rider', 'shared'}
    tally = {}
    for r in res:
        lab = labels.get(r['id'])
        if not lab:
            continue
        g = r.get('geometry') or {}
        print(f"{r['id']:34} {lab['label']:14} {lab['hero']:4} {str(r.get('flag', 'ERR')):5} "
              f"{str(r.get('encroachment', '')):5} {g.get('peak_h', 0):<6} {g.get('min_gap_m', '-'):<5} {g.get('lateral_toward_rider', 0):<5} "
              f"{g.get('side', '-'):5} {str(g.get('pan_suspect', '')):5} {','.join(r.get('flag_reasons', []))}")
        key = ('real' if lab['label'] in real else 'not_real', r.get('flag'))
        tally[key] = tally.get(key, 0) + 1
    print('\nflagged real near misses:', tally.get(('real', True), 0), '/',
          tally.get(('real', True), 0) + tally.get(('real', False), 0))
    print('flagged non-incidents:  ', tally.get(('not_real', True), 0), '/',
          tally.get(('not_real', True), 0) + tally.get(('not_real', False), 0))


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--rows', default=os.path.expanduser('~/bike-probe/gopro_all.json'))
    ap.add_argument('--out', default='data/geometry.json')
    ap.add_argument('--labels', help='only run the clips in this labels.csv and print a check')
    ap.add_argument('--workers', type=int, default=4)
    a = ap.parse_args()
    rows = json.load(open(a.rows))
    if a.labels:
        import csv
        want = {r['segment'] for r in csv.DictReader(open(a.labels))}
        rows = [r for r in rows if seg_id(r['filename']) in want]
    with ThreadPoolExecutor(a.workers) as ex:
        res = list(ex.map(analyse, rows))
    ok = [r for r in res if 'err' not in r]
    json.dump({'stage': 'B', 'camera_id': 'nyc_bike_gopro-1',
               'params': {'veh_h_m': VEH_H_M, 'corridor_m': CORRIDOR_M, 'enter_m': ENTER_M, 'close_h': CLOSE_H,
                          'lateral_min_mps': LATERAL_MIN, 'pan_min': PAN_MIN},
               'counts': {'rows': len(res), 'errors': len(res) - len(ok), 'flagged': sum(r['flag'] for r in ok)},
               'rows': res}, open(a.out, 'w'))
    print(f"scanned {len(ok)}/{len(res)} | flagged for Stage C: {sum(r['flag'] for r in ok)} -> {a.out}")
    if a.labels:
        check_labels(res, a.labels)
