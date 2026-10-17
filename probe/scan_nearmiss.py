"""Score every helmet-cam segment for close-pass and cut-in shapes from YOLO boxes.

Overtaking vehicle: appears at a frame edge, large, then moves inward and shrinks (pulls ahead).
Parked / slower vehicles do the opposite (grow and move outward), which is what this rule rejects.
Cut-in: a large vehicle box moves sideways across the frame centre faster than the camera pans.
"""
import json, os, sys, urllib.parse, statistics
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(__file__))
from vss_client import req, tok

VEH = ('car', 'truck', 'bus', 'motorcycle')
ROWS = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser('~/bike-probe/gopro_all.json')
OUT = sys.argv[2] if len(sys.argv) > 2 else 'nearmiss_scores.json'


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


def score(row):
    try:
        d = req('/api/v1/videos/detections?source=' + urllib.parse.quote(row['source'], safe=''), None, tok)
    except Exception as e:
        return {'source': row['source'], 'err': str(e)[:80]}
    H, W = d['video_shape'][:2]
    trs = tracks_of(d['frames'])
    best_pass, best_cut = None, None
    for t in trs:
        P = t['pts']
        hs = [(b[3] - b[1]) / H for _, b in P]
        cx = [((b[0] + b[2]) / 2) / W for _, b in P]
        k = max(range(len(P)), key=lambda i: hs[i])
        hmax, (tk, bk) = hs[k], P[k]
        at_edge = bk[0] < 0.04 * W or bk[2] > 0.96 * W
        side = 'left' if cx[k] < 0.5 else 'right'
        # close pass: big at the edge, then shrinks and moves inward within 2 s
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
                                 'peak_h': round(hmax, 2), 'shrink': round(shrink, 2), 'inward': round(inward, 2),
                                 'bbox': [round(v) for v in bk]}
        # cut-in: sideways sweep across the centre while big, net of camera pan
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
                                    'label': t['label'], 'dx': round(dx, 2), 'h': round(hs[j], 2),
                                    'bbox': [round(v) for v in P[j][1]]}
    # camera pan check for cut-ins: if most other tracks moved the same way, it's the rider turning
    if best_cut:
        t0, t1 = best_cut['t0'], best_cut['t1']
        moves = []
        for t in trs:
            seg = [p for p in t['pts'] if t0 <= p[0] <= t1]
            if len(seg) >= 2:
                moves.append(((seg[-1][1][0] + seg[-1][1][2]) - (seg[0][1][0] + seg[0][1][2])) / 2 / W)
        same = sum(1 for m in moves if m * best_cut['dx'] > 0 and abs(m) > 0.1)
        best_cut['pan_suspect'] = len(moves) > 1 and same / len(moves) > 0.6
    return {'source': row['source'], 'filename': row['filename'], 'start': row.get('segment_start_sec'),
            'n_tracks': len(trs), 'pass': best_pass, 'cut': best_cut,
            'desc': (row.get('reasoning_content') or '')[:400]}


if __name__ == '__main__':
    rows = json.load(open(ROWS))
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(score, rows))
    json.dump(res, open(OUT, 'w'))
    ok = [r for r in res if 'err' not in r]
    P = sorted([r for r in ok if r['pass']], key=lambda r: -r['pass']['score'])
    C = sorted([r for r in ok if r['cut'] and not r['cut']['pan_suspect']], key=lambda r: -r['cut']['score'])
    print(f'scanned {len(ok)}/{len(res)} | close-pass shape: {len(P)} | cut-in shape (not camera pan): {len(C)}')
    print('\nTOP CLOSE PASSES')
    for r in P[:12]:
        print(f"  {r['pass']}  {r['filename'][16:58]}")
    print('\nTOP CUT-INS')
    for r in C[:12]:
        print(f"  {r['cut']}  {r['filename'][16:58]}")
