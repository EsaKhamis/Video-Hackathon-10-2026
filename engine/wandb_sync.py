"""Push the engine's results to W&B Weave: incidents and labels as datasets, the fault rules as an evaluation.

  python3 engine/wandb_sync.py      # needs `weave` installed, WANDB_API_KEY and WANDB_PROJECT (.env is read)

Run it after build_incidents.py / report.py / pull-reviews.sh. Each run adds a new dataset version and one
evaluation named after fault.VERSION, so rule rounds can be compared side by side in the Weave UI.
"""
import csv, json, os, sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'probe'))
import wb_trace as trace  # noqa: E402
from report import load_env  # noqa: E402
import eval_labels  # noqa: E402


def incident_row(item):
    doc = item.get('document') or {}
    return {'id': item.get('id'), 'source': item.get('source'), 'ride': item.get('ride'),
            'fault': item.get('fault'), 'fault_rule': item.get('fault_rule'), 'severity': item.get('severity'),
            'event_type': item.get('event_type'), 'facts': item.get('facts'), 'geometry': item.get('geometry'),
            'kind': doc.get('kind'), 'title': doc.get('title'), 'summary': doc.get('summary'),
            'what_happened': doc.get('what_happened'), 'why_risky': doc.get('why_risky'),
            'next_time': doc.get('next_time'), 'generated_by': doc.get('generated_by')}


def main():
    load_env()
    if not trace.init():
        sys.exit('Weave is off: install weave and set WANDB_API_KEY and WANDB_PROJECT')
    weave = trace.weave

    incidents = json.load(open(os.path.join(ROOT, 'data', 'incidents.json')))['incidents']
    weave.publish(weave.Dataset(name='witness-incidents', rows=[incident_row(i) for i in incidents]))
    labels = list(csv.DictReader(open(os.path.join(ROOT, 'probe', 'labels.csv'))))
    weave.publish(weave.Dataset(name='witness-labels', rows=labels))
    print(f'datasets: {len(incidents)} incidents, {len(labels)} labels')

    e = eval_labels.evaluate()
    ev = weave.EvaluationLogger(name=f"fault-rules-{e['rules']}", model=f"fault-rules-{e['rules']}",
                                dataset='witness-labels')
    for r in e['rows']:
        pred = ev.log_prediction(inputs={'segment': r['segment'], 'label': r['label'], 'hero': r['hero'],
                                         'label_from': r['from']},
                                 output={'verdict': r['engine'], 'rule': r['rule']})
        pred.log_score('agree', r['result'] == 'ok')
        pred.log_score('false_driver', r['engine'] == 'driver' and r['label'] in eval_labels.NEVER_DRIVER)
        pred.finish()
    ev.log_summary({'agree': e['agree'], 'total': e['total'], 'wrong_driver': e['wrong_driver'],
                    'heroes_ok': e['heroes_ok'], 'heroes': e['heroes'], 'reviewed': e['reviewed']})
    print(f"evaluation fault-rules-{e['rules']}: {e['agree']}/{e['total']} agree, {e['wrong_driver']} wrong driver")


if __name__ == '__main__':
    main()
