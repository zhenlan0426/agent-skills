"""Condense raw Kaggle listing + version responses (from fetch_listing.sh) into a listing snapshot JSON."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


def main(listing, versions, output):
    rows = []
    for kernel in json.loads(Path(listing).read_text())['kernels']:
        if kernel.get('bestPublicScore') is None:
            continue
        items = json.loads((Path(versions) / f"{kernel['id']}.json").read_text()).get('items', [])
        complete = [item for item in items if item['run'].get('status') == 'COMPLETE']
        latest = max(items, key=lambda item: item['version']['versionNumber'])
        rows.append({
            'kernel': kernel['scriptUrl'].removeprefix('/code/'),
            'id': kernel['id'],
            # Best over all versions; it is the current version's score only when one version completed.
            'best_public_score': f"{kernel['bestPublicScore']:.3f}",
            'complete_versions': len(complete),
            # A failed or pending latest version has no score, so the listed score belongs to an older one.
            'latest_complete': latest['run'].get('status') == 'COMPLETE',
            'latest_version': latest['version']['versionNumber'],
            'gpu': bool(kernel.get('isGpuEnabled')),
            'visible_run_seconds': latest['run'].get('runInfo', {}).get('runTimeSeconds'),
            'latest_run_created': latest['run'].get('dateCreated'),
            'title': kernel['title'],
        })
    snapshot = {'fetched_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'notebooks': rows}
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text(json.dumps(snapshot, indent=1) + '\n')
    print(f'{len(rows)} scored notebooks -> {output}')


if __name__ == '__main__':
    main(*sys.argv[1:])
