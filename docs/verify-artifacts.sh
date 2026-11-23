#!/usr/bin/env bash
# verify-artifacts.sh — pre-flight check for casehub artifacts before Maven build
#
# Called by run-with-progress.sh after actions/download-artifact, before mvn.
# Catches missing or corrupt JARs early with a clear error, instead of letting
# Maven fail deep in compilation with an opaque classpath message.
#
# Usage: bash docs/verify-artifacts.sh <repo> [repo2 ...]
#
# Two modes, controlled by RESUME_FROM env var (set in workflow top-level env):
#
#   Full build (RESUME_FROM empty):
#     All casehub artifacts should be present — uploaded by prior levels in this
#     run. Missing = broken upload/download chain → hard fail.
#
#   Resume build (RESUME_FROM set):
#     Prior levels were skipped, so their artifacts weren't uploaded in this run.
#     Maven resolves them from GitHub Packages at build time. Missing = expected
#     → warning only. Corrupt = still hard fail (GP served a bad JAR).
#
# Intra-repo module handling:
#   Multi-module repos (e.g. desiredstate) list their own sub-modules as
#   io.casehub dependencies in dependencyManagement. These are built together
#   in the same Maven invocation, not downloaded from prior levels. We walk
#   ALL subdirectories (not just first-level <modules>) because repos like
#   desiredstate have nested modules (annotations/spring/) whose artifactIds
#   wouldn't be found by only checking the root POM's <modules> list.
#
# Dashboard marker: writes /tmp/artifact-status.json for progress reporting.

set -euo pipefail

M2_CASEHUB="${M2_REPO:-$HOME/.m2/repository}/io/casehub"
RESUME="${RESUME_FROM:-}"
MISSING=0
CORRUPT=0
VERIFIED=0

echo "=== Artifact verification (mode: ${RESUME:+resume}${RESUME:-full}) ==="

for repo in "$@"; do
  pom="$repo/pom.xml"
  [ -f "$pom" ] || continue

  # Extract cross-repo casehub dependencies (excluding this repo's own modules).
  # We walk the entire repo directory tree for pom.xml files to collect ALL
  # descendant artifactIds — needed because repos have nested multi-module
  # structures (e.g. desiredstate/annotations/spring/ is a sub-module of
  # desiredstate/annotations/, not a direct child of desiredstate/).
  deps=$(python3 -c "
import xml.etree.ElementTree as ET, os
ns = {'m': 'http://maven.apache.org/POM/4.0.0'}
try:
    tree = ET.parse('$pom')
    # Collect artifactIds from every pom.xml under this repo — these are
    # intra-repo modules that Maven builds together, not cross-repo deps.
    own_modules = set()
    for root, dirs, files in os.walk('$repo'):
        if 'pom.xml' in files and root != '$repo':
            try:
                mtree = ET.parse(os.path.join(root, 'pom.xml'))
                aid = mtree.find('m:artifactId', ns)
                if aid is not None and aid.text:
                    own_modules.add(aid.text)
            except: pass
    # Print only cross-repo deps (not in own_modules)
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

    # ZIP magic number check — catches corrupt downloads (truncated, empty
    # central directory) before Maven tries to read the JAR as a classpath entry.
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

# Corrupt artifacts always block — Maven will fail with ZipException anyway,
# and the error from here is clearer than a compiler error 5 minutes later.
if [ "$CORRUPT" -gt 0 ]; then
  echo "::error::Artifact verification failed: $MISSING missing, $CORRUPT corrupt"
  echo "::error::Corrupt artifacts will cause build failures."
  exit 1
fi

# Missing artifacts: in resume mode, Maven resolves from GitHub Packages
# at build time (the warm cache or GP has them). In full build mode, missing
# means the upload/download chain between levels is broken.
if [ -n "$RESUME" ]; then
  echo "::warning::$MISSING artifacts missing (resume mode — Maven resolves from GitHub Packages)"
  exit 0
else
  echo "::error::Artifact verification failed: $MISSING missing"
  echo "::error::Upload/download chain broken — run a full build to rebuild."
  exit 1
fi
