#!/usr/bin/env bash
# Usage: fetch_listing.sh <competition-slug> <competition-id> <output.json>
# Snapshot a competition's public notebooks with their best public score and version history. The public Kaggle API
# exposes neither, so this uses the endpoints behind kaggle.com's own code-listing page with an anonymous session
# (no login). Get the numeric id from `KaggleApi().competitions_list(search=<slug>)[0].id`.
# Only the first 100 notebooks by score are listed (pageSize 200 is refused). See references/known-scores.md.
set -euo pipefail
SLUG=$1 COMPETITION_ID=$2 OUTPUT=$3
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
JAR="$WORK/cookies.txt"
API=https://www.kaggle.com/api/i/kernels.KernelsService

curl -sSf -c "$JAR" -o /dev/null "https://www.kaggle.com/competitions/$SLUG/code"
XSRF=$(awk '$6 == "XSRF-TOKEN" {print $7}' "$JAR")
post() { curl -sSf -b "$JAR" -H "x-xsrf-token: $XSRF" -H 'content-type: application/json' -X POST "$API/$1" -d "$2"; }

post ListKernels "{\"kernelFilterCriteria\":{\"search\":\"\",\"listRequest\":{\"competitionId\":$COMPETITION_ID,\"sortBy\":\"SCORE_DESCENDING\",\"pageSize\":100,\"group\":\"EVERYONE\",\"page\":1}},\"detailFilterCriteria\":{\"deletedAccessBehavior\":\"RETURN_NOTHING\",\"unauthorizedAccessBehavior\":\"RETURN_NOTHING\",\"excludeResultsFilesOutputs\":true,\"wantOutputFiles\":false,\"kernelIds\":[],\"outputFileTypes\":[],\"includeInvalidDataSources\":false}}" > "$WORK/list.json"
mkdir "$WORK/versions"
python3 -I -c 'import json, sys
for k in json.load(open(sys.argv[1]))["kernels"]:
    if k.get("bestPublicScore") is not None: print(k["id"])' "$WORK/list.json" | while read -r id; do
  post ListKernelVersions "{\"kernelId\":$id}" > "$WORK/versions/$id.json"
  sleep 0.3
done
python3 -I "$HERE/summarize_listing.py" "$WORK/list.json" "$WORK/versions" "$OUTPUT"
