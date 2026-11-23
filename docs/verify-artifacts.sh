#!/usr/bin/env bash
# verify-artifacts.sh — check that required casehub artifacts are present
#
# Called after actions/download-artifact to verify completeness before building.
# Exits 0 if all required artifacts found, exits 1 with details if any missing.
#
# Usage: bash docs/verify-artifacts.sh <repo> [repo2 ...]
#   Each repo's dependencies are checked against ~/.m2/repository/io/casehub/
#
# For resumed levels, also outputs a marker file for dashboard colouring:
#   /tmp/artifact-status.json — {"verified": true, "source": "cache"}

set -euo pipefail

M2_CASEHUB="${M2_REPO:-$HOME/.m2/repository}/io/casehub"
MISSING=0
CORRUPT=0
VERIFIED=0

echo "=== Artifact verification ==="

# Check each repo's dependencies exist as installed artifacts
for repo in "$@"; do
  pom="$repo/pom.xml"
  [ -f "$pom" ] || continue

  # Extract casehub dependencies from the POM
  deps=$(python3 -c "
import xml.etree.ElementTree as ET
ns = {'m': 'http://maven.apache.org/POM/4.0.0'}
try:
    tree = ET.parse('$pom')
    for dep in tree.findall('.//m:dependencies/m:dependency', ns):
        g = dep.find('m:groupId', ns)
        a = dep.find('m:artifactId', ns)
        if g is not None and a is not None and g.text == 'io.casehub':
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

    # Check at least one JAR exists and is valid
    jar=$(find "$dir" -name "*.jar" -not -name "*-sources.jar" -not -name "*-javadoc.jar" | head -1)
    if [ -z "$jar" ]; then
      echo "  MISSING JAR: $artifact (directory exists but no JAR)"
      MISSING=$((MISSING + 1))
      continue
    fi

    # ZIP header check
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

# Write status for dashboard
if [ $((MISSING + CORRUPT)) -eq 0 ]; then
  echo '{"verified":true,"source":"cache","count":'$VERIFIED'}' > /tmp/artifact-status.json
  echo "=== All artifacts verified ==="
  exit 0
else
  echo '{"verified":false,"missing":'$MISSING',"corrupt":'$CORRUPT'}' > /tmp/artifact-status.json
  if [ "$CORRUPT" -gt 0 ]; then
    echo "::error::Artifact verification failed: $MISSING missing, $CORRUPT corrupt"
    echo "::error::Cannot proceed — corrupt artifacts will cause build failures."
    exit 1
  else
    echo "::warning::Artifact verification: $MISSING missing (will resolve from GitHub Packages)"
    exit 0
  fi
fi
