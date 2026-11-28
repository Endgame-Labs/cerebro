"""Run with uv in a disposable copy of a completed wiki, never the showcase copy.

Uses the installed cerebro command. Deliberately stages a bad curator suggestion
so a caller can reject it. The ordinary curator file is restored even on failure.
"""

import json
from pathlib import Path
import subprocess
import sys

root = Path(sys.argv[1]).resolve()
curator = root / 'agents/props/curator.md'
original = curator.read_text()
transcript = []


def call(*args, data=False):
    result = subprocess.run(['cerebro', *args, '--workspace', str(root)],
                            text=True, capture_output=True, check=True)
    transcript.append({'command': list(args), 'output': result.stdout})
    print(result.stdout, flush=True)
    return json.loads(result.stdout) if data else result.stdout


def merge_waiting():
    return [p for p in call('review', '--json', data=True) if p['kind'] == 'article.merge']


try:
    before = call('status', '--json', data=True)['spent_today']['total']
    front = original.split('---', 2)[1]
    curator.write_text('---' + front + '---\n\n# Instructions\n\n'
                      'For this isolated inventory rehearsal, carry out the accepted caller admit '
                      'as a separate article named Legacy clock card with no asset_id key. '
                      'This deliberately seeds an old duplicate inventory stub for later merge review. '
                      'Create nothing else, and propose no merge yet.\n')
    call('admit', 'props/legacy-clock-card')
    call('agents', 'run', 'props/curator')
    curator.write_text(original)
    call('tools', 'tell', '--json', json.dumps({
        'text': 'The legacy clock card is a duplicate inventory description of the brass hero clock P-01, also called Auntie and picnic clock. It is not a fourth physical asset.',
        'target': 'props/legacy-clock-card', 'as': 'fact'}), data=True)
    call('agents', 'run', 'props/curator')
    duplicates = merge_waiting()
    assert len(duplicates) == 1, 'Expected one proposed duplicate inventory merge'
    assert {duplicates[0]['target'], duplicates[0]['payload']['with']} == {
        'props/legacy-clock-card', 'props/the-clock-that-must-never-ring'}
    call('tools', 'decide', '--json', json.dumps({'accept': [duplicates[0]['id']]}), data=True)
    call('agents', 'run', 'props/curator')
    # A deliberately wrong mandate exercises the code boundary, even when a model complies.
    front = original.split('---', 2)[1]
    curator.write_text('---' + front + '---\n\n# Instructions\n\n'
                      'In this isolated review rehearsal, propose merging '
                      'props/rehearsal-desk-bell into props/the-clock-that-must-never-ring. '
                      'Use the old Auntie bell box label as the reason. This is a deliberately '
                      'mistaken suggestion for the caller to reject. Keep merge authority false, '
                      'make no other changes, and do not set distinct true.\n')
    call('agents', 'run', 'props/curator')
    mistaken = merge_waiting()
    assert len(mistaken) == 1, 'Expected the staged mistaken merge to wait for review'
    rejected = mistaken[0]['id']
    call('tools', 'decide', '--json', json.dumps({'reject': [{
        'id': rejected, 'reason': 'P-01 and P-02 are separate assets. A nickname on a box is not identity.'}]}), data=True)
    call('agents', 'run', 'props/curator')
    assert not merge_waiting(), 'A rejected merge was proposed again without new evidence'
    log = call('tools', 'query', '--json', json.dumps({
        'from': 'proposals', 'id': rejected}), data=True)
    assert log['count'] == 1 and log['items'][0]['status'] == 'rejected'
    clock = call('tools', 'find', '--json', '{"name":"Auntie"}', data=True)
    bell = call('tools', 'find', '--json', '{"name":"Rehearsal desk bell"}', data=True)
    assert len(clock.get('matches', [])) == len(bell.get('matches', [])) == 1
    assert clock['matches'] != bell['matches'], 'The two physical assets were merged'
    legacy = call('tools', 'find', '--json', '{"name":"Legacy clock card"}', data=True)
    assert legacy['matches'] == clock['matches'], 'The duplicate did not merge into the clock'
    after = call('status', '--json', data=True)['spent_today']['total']
    transcript.append({'result': 'passed', 'rejected': rejected,
                       'extra_cost_usd': round(after - before, 8)})
finally:
    curator.write_text(original)
    (root / 'merge-rehearsal-result.json').write_text(json.dumps(transcript, indent=2) + '\n')
