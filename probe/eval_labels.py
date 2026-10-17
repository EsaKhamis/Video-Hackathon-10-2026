"""Engine verdict vs hand labels (PLAN.md Must 8 and the definition-loop stop criteria).

  python3 probe/eval_labels.py      # reads data/stage_c.json, data/geometry.json, probe/labels.csv
"""
import csv, json, os, sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, os.path.join(ROOT, 'engine'))
import fault  # noqa: E402

EXPECT = {'driver': 'driver', 'rider': 'rider', 'shared': 'unclear'}   # anything else should be dropped
NEVER_DRIVER = {'rider', 'adjacent_lane', 'separated', 'rider_turning', 'other_fp', 'stationary'}

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
    rows.append((lab['hero'] or '-', k, lab['label'], verdict, rule, 'ok' if ok else ('unclear' if verdict == 'unclear' else 'WRONG')))

print(f'rules {fault.VERSION}')
for r in rows:
    print(f'{r[0]:3} {r[1]:34} label={r[2]:13} engine={r[3]:8} rule={r[4]:<3} {r[5]}')
print(f'\nagree with label: {agree}/{len(rows)}')
print(f'wrong driver verdicts (label rider/adjacent/separated/fp): {len(wrong_driver)} {wrong_driver}')
