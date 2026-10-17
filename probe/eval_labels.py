"""Engine verdict vs hand labels (PLAN.md Must 8 and the definition-loop stop criteria).

  python3 probe/eval_labels.py      # reads data/stage_c.json, data/geometry.json, probe/labels.csv, data/reviews.json

Labels come from probe/labels.csv plus the rider's reviews in the app (data/reviews.json, pulled by
k8s/pull-reviews.sh). A review is the newer judgement, so it replaces the hand label for the same segment.
"""
import csv, json, os, sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, os.path.join(ROOT, 'engine'))
import fault  # noqa: E402

REVIEWS = os.path.join(ROOT, 'data', 'reviews.json')
EXPECT = {'driver': 'driver', 'rider': 'rider', 'shared': 'unclear'}   # anything else should be dropped
NEVER_DRIVER = {'rider', 'adjacent_lane', 'separated', 'rider_turning', 'other_fp', 'stationary'}
REVIEW_LABEL = {'driver': 'driver', 'rider': 'rider', 'unclear': 'shared', 'shared': 'shared'}


def load_reviews():
    return json.load(open(REVIEWS)) if os.path.exists(REVIEWS) else {}


def review_segment(incident_id, review, heroes):
    """Segment a review is about: stored by k8s/pull-reviews.sh, else read from the incident id."""
    if review.get('segment'):
        return review['segment']
    if incident_id in heroes:
        return heroes[incident_id]
    return incident_id[2:] if incident_id[:2] in ('C-', 'B-') else None


def review_labels():
    """{segment: label} from app reviews. Dismissed means "not an incident"; otherwise the fault the rider chose."""
    heroes = {r['hero']: r['segment'] for r in csv.DictReader(open(os.path.join(ROOT, 'probe/labels.csv'))) if r['hero']}
    out = {}
    for incident_id, r in load_reviews().items():
        seg = review_segment(incident_id, r, heroes)
        if not seg:
            continue
        if r.get('status') == 'dismissed':
            out[seg] = 'other_fp'
        elif r.get('fault_override') in REVIEW_LABEL:
            out[seg] = REVIEW_LABEL[r['fault_override']]
    return out


def evaluate():
    geo = {r['id']: r for r in json.load(open(os.path.join(ROOT, 'data/geometry.json')))['rows']}
    sc = {r['id']: r for r in json.load(open(os.path.join(ROOT, 'data/stage_c.json')))['rows']}
    labels = {lab['segment']: {'hero': lab['hero'], 'label': lab['label'], 'from': 'hand'}
              for lab in csv.DictReader(open(os.path.join(ROOT, 'probe/labels.csv')))}
    for seg, label in review_labels().items():
        labels[seg] = {'hero': labels.get(seg, {}).get('hero', ''), 'label': label, 'from': 'review',
                       'was': labels.get(seg, {}).get('label')}
    rows, agree, wrong_driver = [], 0, []
    for k, lab in labels.items():
        if k not in sc:
            continue
        verdict, rule, _ = fault.decide(sc[k]['facts'], sc[k]['still_signal'], geo.get(k), sc[k].get('votes'))
        want = EXPECT.get(lab['label'], 'drop')
        ok = verdict == want
        agree += ok
        if verdict == 'driver' and lab['label'] in NEVER_DRIVER:
            wrong_driver.append(k)
        rows.append({'hero': lab['hero'], 'segment': k, 'label': lab['label'], 'from': lab['from'],
                     'was': lab.get('was'), 'engine': verdict, 'rule': rule,
                     'result': 'ok' if ok else ('unclear' if verdict == 'unclear' else 'wrong')})
    return {'rules': fault.VERSION, 'agree': agree, 'total': len(rows), 'wrong_driver': len(wrong_driver),
            'reviewed': sum(r['from'] == 'review' for r in rows),
            'heroes_ok': sum(r['result'] == 'ok' for r in rows if r['hero']),
            'heroes': sum(1 for r in rows if r['hero']), 'rows': rows}


if __name__ == '__main__':
    e = evaluate()
    print(f"rules {e['rules']}")
    for r in e['rows']:
        src = 'review' + (f" (hand said {r['was']})" if r['was'] and r['was'] != r['label'] else '') if r['from'] == 'review' else 'hand'
        print(f"{r['hero'] or '-':3} {r['segment']:34} label={r['label']:13} engine={r['engine']:8} rule={r['rule']:<3} "
              f"{r['result']:8} {src}")
    print(f"\nagree with label: {e['agree']}/{e['total']} ({e['reviewed']} labelled by app review)")
    print(f"wrong driver verdicts (label rider/adjacent/separated/fp): {e['wrong_driver']}")
