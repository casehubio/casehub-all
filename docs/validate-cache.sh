#!/usr/bin/env bash
# validate-cache.sh — verify Maven cache integrity and BOM completeness
#
# Three-step cache validation:
#   1. Integrity: check JARs are valid ZIPs (corrupt downloads are the #1 CDN failure)
#   2. BOM completeness: verify all managed deps exist at the expected path
#   3. Self-heal: re-download anything corrupt or missing
#
# Uses a manifest file (.cache-manifest) to avoid re-checking JARs that were
# already validated in a previous run. JARs don't corrupt in-place — once
# validated they stay valid. Only new arrivals need checking.
#
# Usage: bash docs/validate-cache.sh [--heal] [--verbose]
#   --heal     re-download corrupt/missing artifacts (default: report only)
#   --verbose  print each file checked

set -euo pipefail

M2_REPO="${M2_REPO:-$HOME/.m2/repository}"
MANIFEST="$M2_REPO/.cache-manifest"
HEAL=false
VERBOSE=false
CORRUPT=0
MISSING=0
HEALED=0
SKIPPED=0

for arg in "$@"; do
  case "$arg" in
    --heal) HEAL=true ;;
    --verbose) VERBOSE=true ;;
  esac
done

# ─── Step 1: Integrity — check JAR ZIP headers ───────────────────────────
echo "=== Cache validation: integrity check ==="

# Load existing manifest (file:size pairs already validated)
declare -A KNOWN
if [ -f "$MANIFEST" ]; then
  while IFS='|' read -r path size; do
    KNOWN["$path"]="$size"
  done < "$MANIFEST"
  echo "  Manifest loaded: ${#KNOWN[@]} known-good entries"
else
  echo "  No manifest — full scan"
fi

# Scan JARs
NEW_ENTRIES=()
while IFS= read -r jar; do
  size=$(stat -f%z "$jar" 2>/dev/null || stat -c%s "$jar" 2>/dev/null || echo "0")

  # Skip if already validated at same size
  if [ "${KNOWN[$jar]:-}" = "$size" ]; then
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  $VERBOSE && echo "  checking: $jar"

  # Zero-byte or tiny files are always corrupt
  if [ "$size" -lt 4 ]; then
    echo "  CORRUPT (zero/tiny): $jar"
    CORRUPT=$((CORRUPT + 1))
    if $HEAL; then
      rm -f "$jar"
      HEALED=$((HEALED + 1))
    fi
    continue
  fi

  # Check ZIP magic bytes (PK\x03\x04)
  magic=$(xxd -l4 -p "$jar" 2>/dev/null || echo "")
  if [ "$magic" != "504b0304" ]; then
    echo "  CORRUPT (bad header): $jar"
    CORRUPT=$((CORRUPT + 1))
    if $HEAL; then
      rm -f "$jar"
      # Also clear .lastUpdated markers so Maven will retry
      dir=$(dirname "$jar")
      rm -f "$dir"/*.lastUpdated 2>/dev/null || true
      HEALED=$((HEALED + 1))
    fi
    continue
  fi

  # Valid — record for manifest
  NEW_ENTRIES+=("$jar|$size")
done < <(find "$M2_REPO" -name '*.jar' -type f 2>/dev/null)

echo "  Scanned: skipped=${SKIPPED} new=$((${#NEW_ENTRIES[@]})) corrupt=${CORRUPT}"

# ─── Step 2: BOM completeness — check managed deps exist ─────────────────
echo "=== Cache validation: BOM completeness ==="

# Parse parent POM dependencyManagement for casehub artifacts
PARENT_POM="parent/pom.xml"
if [ -f "$PARENT_POM" ]; then
  # Extract casehub.version from properties
  CASEHUB_VERSION=$(python3 -c "
import xml.etree.ElementTree as ET
ns = {'m': 'http://maven.apache.org/POM/4.0.0'}
tree = ET.parse('$PARENT_POM')
v = tree.find('.//m:properties/m:casehub.version', ns)
print(v.text if v is not None else '0.4-SNAPSHOT')
" 2>/dev/null || echo "0.4-SNAPSHOT")

  # Check key casehub artifacts exist
  MISSING_ARTIFACTS=()
  while IFS= read -r artifactId; do
    [ -z "$artifactId" ] && continue
    path="$M2_REPO/io/casehub/$artifactId/$CASEHUB_VERSION"
    if [ ! -d "$path" ]; then
      MISSING=$((MISSING + 1))
      $VERBOSE && echo "  MISSING: $artifactId:$CASEHUB_VERSION"
      MISSING_ARTIFACTS+=("io.casehub:$artifactId:$CASEHUB_VERSION")
    fi
  done < <(python3 -c "
import xml.etree.ElementTree as ET
ns = {'m': 'http://maven.apache.org/POM/4.0.0'}
tree = ET.parse('$PARENT_POM')
for dep in tree.findall('.//m:dependencyManagement/m:dependencies/m:dependency', ns):
    g = dep.find('m:groupId', ns)
    a = dep.find('m:artifactId', ns)
    if g is not None and a is not None and g.text == 'io.casehub':
        print(a.text)
" 2>/dev/null)

  echo "  BOM check: missing=${MISSING} of casehub artifacts"
else
  echo "  No parent POM found — skipping BOM check"
fi

# ─── Step 3: Self-heal ────────────────────────────────────────────────────
if $HEAL && [ $((CORRUPT + MISSING)) -gt 0 ]; then
  echo "=== Cache validation: self-heal ==="

  # Clear .lastUpdated tombstones for missing/corrupt artifacts
  find "$M2_REPO" -name "*.lastUpdated" -delete 2>/dev/null || true
  echo "  Cleared .lastUpdated markers"

  if [ "$MISSING" -gt 0 ]; then
    echo "  $MISSING casehub artifacts missing (expected — restored via level artifacts)"
  fi

  echo "  Healed: ${HEALED} corrupt JARs removed"
fi

# ─── Update manifest ─────────────────────────────────────────────────────
echo "=== Updating manifest ==="
{
  # Preserve existing valid entries
  if [ -f "$MANIFEST" ]; then
    while IFS='|' read -r path size; do
      [ -f "$path" ] && echo "$path|$size"
    done < "$MANIFEST"
  fi
  # Add newly validated entries
  for entry in "${NEW_ENTRIES[@]+"${NEW_ENTRIES[@]}"}"; do
    echo "$entry"
  done
} | sort -u > "$MANIFEST.tmp"
mv "$MANIFEST.tmp" "$MANIFEST"

TOTAL=$(wc -l < "$MANIFEST" | tr -d ' ')
echo "  Manifest: ${TOTAL} validated entries"

# ─── Summary ─────────────────────────────────────────────────────────────
echo "=== Cache validation complete ==="
echo "  corrupt=${CORRUPT} missing=${MISSING} healed=${HEALED}"

if [ $CORRUPT -gt 0 ] && ! $HEAL; then
  echo "  Run with --heal to fix corrupt JARs"
  exit 1
fi
