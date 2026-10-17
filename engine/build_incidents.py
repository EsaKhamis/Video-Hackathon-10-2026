"""Join engine outputs into the app's data/incidents.json (schema: PLAN.md "Incident schema").

  data/stage_d.json   Stage D rider-on-red survivors (frame-reviewed)   -> fault rider, rule 3, rider lesson
  data/stage_c.json   Stage C Cosmos facts (engine/confirm.py)          -> driver (rule 7), sidewalk (rule 4),
                                                                           unclear (rule 8)
  data/geometry.json  Stage B YOLO geometry for every segment            -> boxes, side, severity
  data/segments.json  playable source + ride offset per segment

Driver (rule 7) needs BOTH models: the YOLO close-pass shape (a vehicle overtaking, not a camera pan) AND
Cosmos, by majority vote, saying near miss with the vehicle entering the rider's path; no separator, rider
not on the sidewalk, no red signal in the video or the still, rider did not move into the vehicle's path.
Any missing piece -> unclear, never driver. Vehicle side comes from YOLO; Cosmos colour is not trusted.
Without stage_c.json, falls back to Stage B only (unclear, no driver verdicts).

  python3 engine/build_incidents.py            # -> data/incidents.json
"""
import argparse, json, os

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
DATA = os.path.join(ROOT, 'data')
UNCLEAR_PEAK_H = 0.45      # rule 8: YOLO encroachment with peak >= 45% of frame height
MAX_UNCLEAR = 10           # keep the review list short: top N by severity
PLACE = {'road': 'traffic_lane', 'bike_lane': 'bike_lane', 'crosswalk': 'crosswalk', 'sidewalk': 'sidewalk'}
RIDES = {  # hand-written ride cards, as in app/incidents.stub.json
    'GOPR0130': ('Midday Manhattan ride', 'Busy daytime streets, following another cyclist through heavy traffic',
                 'Daytime, heavy Manhattan traffic'),
    'GX010001': ('Dusk delivery-truck ride', 'Bike lanes at dusk with vans and trucks pulling in and out',
                 'Dusk, bike lanes and delivery trucks'),
    'GX050001': ('Night green-lane ride', 'After dark on marked green bike lanes, harder to judge distance',
                 'Night, green bike lanes, low light'),
}


def load(name):
    return json.load(open(os.path.join(DATA, name)))


def seg_key(source):
    return os.path.basename(source).split('_', 2)[2].rsplit('_of_', 1)[0]


def stage_c_verdict(row, c):
    """PLAN.md rules 4, 7, 8 on Stage C facts + Stage B geometry. Returns driver / sidewalk / unclear / None."""
    f = c.get('facts') or {}
    g = row.get('geometry') or {}
    red = f.get('signal_for_rider') == 'red' or c.get('signal_still') == 'red'
    near = f.get('near_miss') is True and f.get('vehicle_entered_rider_path') is True
    votes = c.get('votes') or {}
    if f.get('rider_position') == 'sidewalk' and votes.get('rider_position', []).count('sidewalk') >= 4:
        return 'sidewalk'   # one Cosmos call is not enough here: 4 of 5 votes
    if (row.get('pass') and not g.get('pan_suspect') and near and f.get('separator') != 'yes' and not red
            and f.get('rider_moved_into_vehicle_path') is not True):
        return 'driver'
    if near and f.get('separator') != 'yes':
        return 'unclear'
    return None


def blank_facts(**kw):
    f = {'signal_for_rider': 'unknown', 'rider_position': 'unknown', 'separator': 'unknown',
         'vehicle_action': 'none', 'vehicle_entered_rider_path': None, 'rider_moved_into_vehicle_path': None,
         'vehicle': {'type': 'none', 'colour': 'unknown', 'side': 'none'}, 'closest_distance': 'unknown',
         'evidence': '', 'model_fault': None}
    f.update(kw)
    return f


def geometry_fields(g):
    if not g:
        return None
    keys = ('peak_h', 'side', 'bbox', 'pan_suspect', 'entered_corridor', 'lateral_toward_rider',
            'min_gap_m', 'approach', 't_peak', 'label')
    return {k: g[k] for k in keys}


def raw_severity(g):
    """PLAN.md: peak box height x approach speed; scaled 0-100 across incidents below."""
    return g['peak_h'] * (1 + min(g['approach'], 1.0)) if g else 0.0   # cap: one jerky track shouldn't flatten the rest


def frames(t, dur=5.0):
    return [round(max(0.0, t - 1.0), 1), round(t, 1), round(min(dur - 0.1, t + 0.8), 1)]


def base(seg, geo_row):
    g = (geo_row or {}).get('geometry')
    t = g['t_peak'] if g else 2.5
    return {'id': None, 'ride': seg['ride'], 'source': seg_key(seg['source']), 'preview_source': seg['source'],
            'ride_offset_sec': int(seg['offset_sec']), 't_peak': t, 'geometry': geometry_fields(g),
            'evidence_frames': frames(t), '_raw': raw_severity(g),
            'review': {'status': 'pending', 'plate': '', 'street': '', 'fault_override': None, 'recorded_at': None}}


def main(out):
    segments = load('segments.json')
    by_chunk = {(s['ride'], s['chunk_index'], float(s['start_sec'])): s for s in segments}
    by_key = {seg_key(s['source']): s for s in segments}
    geo = {r['id']: r for r in load('geometry.json')['rows'] if 'err' not in r}
    stage_d = load('stage_d.json')

    incidents, taken = [], set()
    for r in stage_d['rows']:
        if not r.get('keep'):
            continue
        seg = by_chunk.get((r['ride'], r['chunk_index'], float(r['start_sec'])))
        if not seg:
            print('stage_d row with no segment:', r['id'])
            continue
        key = seg_key(seg['source'])
        f = r.get('fields', {})
        inc = base(seg, geo.get(key))
        inc.update({
            'id': r['id'], 'event_type': 'entered_on_red', 'fault': 'rider', 'fault_rule': 3,
            'facts': blank_facts(signal_for_rider=f.get('signal', 'unknown'),
                                 rider_position=PLACE.get(f.get('place'), 'unknown'),
                                 vehicle_entered_rider_path=f.get('vehicle_in_path'),
                                 evidence='The signal facing the rider is red as the rider enters'
                                          + (' the crosswalk.' if f.get('place') == 'crosswalk' else ' the intersection.'),
                                 model_fault='rider'),
            'model_agreement': True,
            'document': {'kind': 'rider_lesson', 'title': 'Entered on red'},
            'stage_d': {'rule': r['rule'], 'rank': r.get('rank'), 'still_review': r.get('still_review')},
        })
        incidents.append(inc)
        taken.add(key)

    stage_c_path = os.path.join(DATA, 'stage_c.json')
    stage_c = {r['id']: r for r in json.load(open(stage_c_path))['rows']
               if r.get('facts')} if os.path.exists(stage_c_path) else None
    unclear = []
    for key, row in geo.items():
        g = row.get('geometry')
        seg = by_key.get(key)
        if key in taken or not g or not seg:
            continue
        if stage_c is None:
            verdict, c = ('unclear', None) if row.get('encroachment') and g['peak_h'] >= UNCLEAR_PEAK_H else (None, None)
        else:
            c = stage_c.get(key)
            verdict = stage_c_verdict(row, c) if c else None
        if not verdict:
            continue
        inc = base(seg, row)
        p = row.get('pass')
        side = p['side'] if verdict == 'driver' else g['side']
        label = p['label'] if verdict == 'driver' else g['label']
        f = (c or {}).get('facts') or {}
        votes = (c or {}).get('votes')
        if verdict == 'driver':
            inc['t_peak'] = p['t']
            inc['evidence_frames'] = frames(p['t'])
            seen = f"{sum(v is True for v in votes['entered_path'])} of {votes['n']}" if votes else '1 of 1'
            dist = {'under_1m': 'under 1 m', '1_to_2m': '1 to 2 m', 'over_2m': 'over 2 m'}.get(f.get('closest_distance'), 'an unknown distance')
            evidence = (f"A {label} on the {side} came within {dist} "
                        f"and moved into the rider's path (Cosmos, {seen} checks); YOLO shows it overtaking at "
                        f"{round(p['peak_h'] * 100)}% of frame height.")
            doc = {'kind': 'authority_report', 'title': f"Close pass by a {label} on the {side}"}
            event, rule, fault = 'close_pass', 7, 'driver'
        elif verdict == 'sidewalk':
            evidence = 'Cosmos sees the rider on the sidewalk. ' + (f.get('evidence') or '')
            doc = {'kind': 'rider_lesson', 'title': 'Riding on the sidewalk'}
            event, rule, fault = 'sidewalk', 4, 'rider'
        else:
            evidence = (f"Close {label} on the {side} ({round(g['peak_h'] * 100)}% of frame height). "
                        + (f"Cosmos: {f.get('evidence')}" if f.get('evidence') else 'Not yet checked by Cosmos.')
                        + ' The two models disagree or a fact is missing, so you decide.')
            doc = {'kind': 'both', 'title': f"Close {label} on the {side}"}
            event = 'close_pass' if p else 'cut_in' if row.get('cut') else 'close_vehicle'
            rule, fault = 8, 'unclear'
        inc.update({
            'id': ('C-' if c else 'B-') + key, 'event_type': event, 'fault': fault, 'fault_rule': rule,
            'facts': blank_facts(
                signal_for_rider=f.get('signal_for_rider', 'unknown'),
                signal_still=(c or {}).get('signal_still', 'unknown'),
                rider_position=f.get('rider_position', 'unknown'), separator=f.get('separator', 'unknown'),
                vehicle_action=f.get('vehicle_action', 'none'),
                vehicle_entered_rider_path=f.get('vehicle_entered_rider_path'),
                rider_moved_into_vehicle_path=f.get('rider_moved_into_vehicle_path'),
                vehicle={'type': label, 'colour': 'cyclist to confirm', 'side': side},
                closest_distance=f.get('closest_distance', 'unknown'), evidence=evidence,
                model_fault=f.get('fault')),
            'model_agreement': (fault == 'driver') if c else None,
            'document': doc,
        })
        if votes:
            inc['votes'] = votes
        (incidents if fault != 'unclear' else unclear).append(inc)
    unclear.sort(key=lambda i: -i['_raw'])
    incidents += unclear[:MAX_UNCLEAR]

    top = max((i['_raw'] for i in incidents), default=0) or 1
    for i in incidents:
        s = round(100 * i.pop('_raw') / top)
        i['severity'] = s
        i['severity_label'] = 'High' if s >= 70 else 'Med' if s >= 40 else 'Low'
        if i['ride'] == 'GX050001':
            i['low_light'] = True
    incidents.sort(key=lambda i: (i['ride'], -i['severity']))

    rides = []
    for rid in sorted({s['ride'] for s in segments}):
        segs = [s for s in segments if s['ride'] == rid]
        title, situation, conditions = RIDES.get(rid, (rid, '', ''))
        dur = max(s['offset_sec'] + s['end_sec'] - s['start_sec'] for s in segs)
        rides.append({'id': rid, 'title': title, 'situation': situation, 'conditions': conditions,
                      'duration_min': round(dur / 60), 'segments': len(segs)})

    json.dump({'generated_from': 'engine/build_incidents.py: stage_d.json (rider/red, rule 3) + stage_c.json '
                                 '(Cosmos facts: driver rule 7, sidewalk rule 4, unclear rule 8) + geometry.json.',
               'rides': rides, 'incidents': incidents}, open(out, 'w'), indent=1)
    c = {}
    for i in incidents:
        c[i['fault']] = c.get(i['fault'], 0) + 1
    print(f'{len(incidents)} incidents {c} -> {out}')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=os.path.join(DATA, 'incidents.json'))
    main(ap.parse_args().out)
