#!/usr/bin/env bash
# publish-to-r2.sh — upload verified-green artifacts to R2 public repo.
#
# Called from casehub-all finalize after a green build. Uploads Maven
# artifacts and npm packages to packages.casehub.org via the R2 Worker
# PUT endpoint.
#
# Usage:
#   bash docs/publish-to-r2.sh [--dry-run]
#
# Requires:
#   R2_PACKAGES_URL   — Worker URL (e.g. https://packages.casehub.org)
#   R2_PACKAGES_TOKEN — write auth token
#   ~/.m2/repository/io/casehub/ populated from the green build

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN=false
if [ "${1:-}" = "--dry-run" ]; then DRY_RUN=true; fi

R2_URL="${R2_PACKAGES_URL:-https://packages.casehub.org}"
R2_TOKEN="${R2_PACKAGES_TOKEN:-}"
M2_CASEHUB="$HOME/.m2/repository/io/casehub"

if [ -z "$R2_TOKEN" ] && [ "$DRY_RUN" = false ]; then
  echo "ERROR: R2_PACKAGES_TOKEN not set"
  exit 1
fi

if [ ! -d "$M2_CASEHUB" ]; then
  echo "ERROR: $M2_CASEHUB not found — build must populate .m2 first"
  exit 1
fi

PUBLISH_YAML="$SCRIPT_DIR/publish.yaml"
if [ ! -f "$PUBLISH_YAML" ]; then
  echo "ERROR: $PUBLISH_YAML not found"
  exit 1
fi

upload_file() {
  local r2_key="$1"
  local local_path="$2"
  if [ "$DRY_RUN" = true ]; then
    echo "  [dry-run] PUT $r2_key ($(wc -c < "$local_path" | tr -d ' ') bytes)"
    return
  fi
  curl -sf -X PUT "$R2_URL/$r2_key" \
    -H "Authorization: Bearer $R2_TOKEN" \
    -H "Content-Type: application/octet-stream" \
    --data-binary "@$local_path" > /dev/null
}

echo "==> Publishing Maven artifacts to R2"
UPLOADED=0
SKIPPED=0

python3 -c "
import yaml, os, sys, hashlib

with open('$PUBLISH_YAML') as f:
    config = yaml.safe_load(f)

m2 = '$M2_CASEHUB'
maven_repos = config.get('maven', [])

for repo in maven_repos:
    repo_dir = os.path.join(m2)
    for root, dirs, files in os.walk(repo_dir):
        for f in files:
            if f.endswith(('.jar', '.pom', '.xml', '.sha1', '.md5', '.sha256', '.sha512', '.asc', '.module')):
                full = os.path.join(root, f)
                rel = os.path.relpath(full, os.path.dirname(m2))
                print(f'maven/{rel}|{full}')
" 2>/dev/null | sort -u | while IFS='|' read -r r2_key local_path; do
  if [ -f "$local_path" ]; then
    upload_file "$r2_key" "$local_path"
    UPLOADED=$((UPLOADED + 1))
  fi
done

echo "==> Publishing npm packages to R2"

python3 -c "
import yaml, os, zipfile, json, sys, tempfile

with open('$PUBLISH_YAML') as f:
    config = yaml.safe_load(f)

m2 = '$M2_CASEHUB'
npm_artifacts = config.get('npm', [])

for artifact in npm_artifacts:
    artifact_dir = os.path.join(m2, artifact)
    if not os.path.isdir(artifact_dir):
        print(f'SKIP {artifact} — not found in .m2', file=sys.stderr)
        continue
    for version_dir in os.listdir(artifact_dir):
        vpath = os.path.join(artifact_dir, version_dir)
        if not os.path.isdir(vpath):
            continue
        for jar_name in os.listdir(vpath):
            if not jar_name.endswith('.jar'):
                continue
            jar_path = os.path.join(vpath, jar_name)
            try:
                with zipfile.ZipFile(jar_path, 'r') as zf:
                    for entry in zf.namelist():
                        if entry.endswith('.tgz'):
                            tmpdir = tempfile.mkdtemp()
                            tgz_path = zf.extract(entry, tmpdir)
                            basename = os.path.basename(entry)
                            # @casehubio/foo-1.0.0.tgz → npm/@casehubio/foo/-/foo-1.0.0.tgz
                            parts = basename.rsplit('-', 1)
                            pkg_name = parts[0] if len(parts) > 1 else basename.replace('.tgz','')
                            r2_key = f'npm/@casehubio/{pkg_name}/-/{basename}'
                            print(f'{r2_key}|{tgz_path}')
            except Exception as e:
                print(f'WARN {jar_path}: {e}', file=sys.stderr)
" 2>/dev/null | while IFS='|' read -r r2_key local_path; do
  if [ -f "$local_path" ]; then
    upload_file "$r2_key" "$local_path"
  fi
done

echo "==> Publish complete"
