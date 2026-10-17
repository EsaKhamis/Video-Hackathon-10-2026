"""Engine verdict vs hand labels (PLAN.md Must 8 and the definition-loop stop criteria).

  python3 probe/eval_labels.py      # reads data/stage_c.json, data/geometry.json, probe/labels.csv
"""
import csv, json, os, sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, os.path.join(ROOT, 'engine'))
import fault  # noqa: E402

EXPECT = {'driver': 'driver', 'rider': 'rider', 'shared': 'unclear'}   # anything else should be dropped
NEVER_DRIVER = {'rider', 'adjacent_lane', 'separated', 'rider_turning', 'other_fp', 'stationary'}


def evaluate():
    geo = {r['id']: r for r in json.load(open(os.path.join(ROOT, 'data/geometry.json')))['rows']}
    sc = {r['id']: r for r in json.load(open(os.path.join(ROOT, 'data/stage_c.json')))['rows']}
    rows, agree, wrong_driver = [], 0, []
    for lab in csv.DictReader(open(os.path.join(ROOT, 'probe/labels.csv'))):
        k = lab['segment']
        if k not in sc:
            continue
        verdict, rule, _ = fault.decide(sc[k]['facts'], sc[k]['still_signal'], geo.get(k), sc[k].get('votes'))
        want = EXPECT.get(lab['label'], 'drop')
        ok = verdict == want
        agree += ok
        if verdict == 'driver' and lab['label'] in NEVER_DRIVER:
            wrong_driver.append(k)
        rows.append({'hero': lab['hero'], 'segment': k, 'label': lab['label'], 'engine': verdict, 'rule': rule,
                     'result': 'ok' if ok else ('unclear' if verdict == 'unclear' else 'wrong')})
    return {'rules': fault.VERSION, 'agree': agree, 'total': len(rows), 'wrong_driver': len(wrong_driver),
            'heroes_ok': sum(r['result'] == 'ok' for r in rows if r['hero']),
            'heroes': sum(1 for r in rows if r['hero']), 'rows': rows}


if __name__ == '__main__':
    e = evaluate()
    print(f"rules {e['rules']}")
    for r in e['rows']:
        print(f"{r['hero'] or '-':3} {r['segment']:34} label={r['label']:13} engine={r['engine']:8} rule={r['rule']:<3} {r['result']}")
    print(f"\nagree with label: {e['agree']}/{e['total']}")
    print(f"wrong driver verdicts (label rider/adjacent/separated/fp): {e['wrong_driver']}")
