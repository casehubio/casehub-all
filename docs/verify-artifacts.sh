#!/usr/bin/env bash
# verify-artifacts.sh — check that required casehub artifacts are present
#
# Called after actions/download-artifact to verify completeness before building.
# Exits 0 if all required artifacts found, exits 1 with details if any missing.
#
# Usage: bash docs/verify-artifacts.sh <repo> [repo2 ...]
#   Each repo's dependencies are checked against ~/.m2/repository/io/casehub/
#
# Skips intra-repo modules — a multi-module repo's own modules are built
# together, not downloaded from prior levels.
#
# Resume mode (RESUME_FROM env var set):
#   Missing artifacts are expected — prior levels were skipped, so their
#   artifacts come from GitHub Packages at build time, not from run uploads.
#   Missing → warning (exit 0). Corrupt → still hard fail (exit 1).
#
# Full build mode (RESUME_FROM empty or unset):
#   Missing artifacts mean the upload/download chain is broken.
#   Missing → hard fail (exit 1). Corrupt → hard fail (exit 1).
#
# Dashboard marker: /tmp/artifact-status.json

set -euo pipefail

M2_CASEHUB="${M2_REPO:-$HOME/.m2/repository}/io/casehub"
RESUME="${RESUME_FROM:-}"
MISSING=0
CORRUPT=0
VERIFIED=0
SKIPPED_INTERNAL=0

echo "=== Artifact verification (mode: ${RESUME:+resume}${RESUME:-full}) ==="

for repo in "$@"; do
  pom="$repo/pom.xml"
  [ -f "$pom" ] || continue

  deps=$(python3 -c "
import xml.etree.ElementTree as ET, os
ns = {'m': 'http://maven.apache.org/POM/4.0.0'}
try:
    tree = ET.parse('$pom')
    # Collect this repo's own module artifactIds to skip
    own_modules = set()
    for mod in tree.findall('.//m:modules/m:module', ns):
        if mod.text:
            mod_pom = os.path.join('$repo', mod.text, 'pom.xml')
            if os.path.isfile(mod_pom):
                try:
                    mtree = ET.parse(mod_pom)
                    aid = mtree.find('m:artifactId', ns)
                    if aid is not None and aid.text:
                        own_modules.add(aid.text)
                except: pass
    for dep in tree.findall('.//m:dependencies/m:dependency', ns):
        g = dep.find('m:groupId', ns)
        a = dep.find('m:artifactId', ns)
        if g is not None and a is not None and g.text == 'io.casehub':
            if a.text not in own_modules:
                print(a.text)
except: pass
" 2>/dev/null)

  for artifact in $deps; do
    dir="$M2_CASEHUB/$artifact"
    if [ ! -d "$dir" ]; then
      echo "  MISSING: $artifact (needed by $repo)"
      MISSING=$((MISSING + 1))
      continue
    fi

    jar=$(find "$dir" -name "*.jar" -not -name "*-sources.jar" -not -name "*-javadoc.jar" | head -1)
    if [ -z "$jar" ]; then
      echo "  MISSING JAR: $artifact (directory exists but no JAR)"
      MISSING=$((MISSING + 1))
      continue
    fi

    magic=$(xxd -l4 -p "$jar" 2>/dev/null || echo "")
    if [ "$magic" != "504b0304" ]; then
      echo "  CORRUPT: $artifact ($jar)"
      CORRUPT=$((CORRUPT + 1))
      continue
    fi

    VERIFIED=$((VERIFIED + 1))
  done
done

echo "  verified=$VERIFIED missing=$MISSING corrupt=$CORRUPT"

if [ $((MISSING + CORRUPT)) -eq 0 ]; then
  echo '{"verified":true,"source":"cache","count":'$VERIFIED'}' > /tmp/artifact-status.json
  echo "=== All artifacts verified ==="
  exit 0
fi

echo '{"verified":false,"missing":'$MISSING',"corrupt":'$CORRUPT'}' > /tmp/artifact-status.json

if [ "$CORRUPT" -gt 0 ]; then
  echo "::error::Artifact verification failed: $MISSING missing, $CORRUPT corrupt"
  echo "::error::Corrupt artifacts will cause build failures."
  exit 1
fi

if [ -n "$RESUME" ]; then
  echo "::warning::$MISSING artifacts missing (resume mode — Maven resolves from GitHub Packages)"
  exit 0
else
  echo "::error::Artifact verification failed: $MISSING missing"
  echo "::error::Upload/download chain broken — run a full build to rebuild."
  exit 1
fi
