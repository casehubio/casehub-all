#!/usr/bin/env bash
# publish-packages.sh — publish verified-green artifacts to GitHub Pages.
#
# Called from casehub-all finalize after a green build. Copies Maven
# artifacts and npm packages to casehubio/packages repo (orphan push).
#
# Usage:
#   bash docs/publish-packages.sh [--dry-run]
#
# Requires:
#   GH_TOKEN or GITHUB_TOKEN — for pushing to casehubio/packages
#   ~/.m2/repository/io/casehub/ populated from the green build

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN=false
if [ "${1:-}" = "--dry-run" ]; then DRY_RUN=true; fi

M2_CASEHUB="$HOME/.m2/repository/io/casehub"
PACKAGES_REPO="https://x-access-token:${GH_TOKEN:-${GITHUB_TOKEN:-}}@github.com/casehubio/packages.git"
PUBLISH_YAML="$SCRIPT_DIR/publish.yaml"
WORK_DIR=$(mktemp -d)

if [ ! -d "$M2_CASEHUB" ]; then
  echo "ERROR: $M2_CASEHUB not found — build must populate .m2 first"
  exit 1
fi

if [ ! -f "$PUBLISH_YAML" ]; then
  echo "ERROR: $PUBLISH_YAML not found"
  exit 1
fi

echo "==> Preparing packages for publish"

pip install --user pyyaml 2>&1 || true

python3 -c "
import yaml, os, shutil, zipfile, sys, tempfile

with open('$PUBLISH_YAML') as f:
    config = yaml.safe_load(f)

m2 = '$M2_CASEHUB'
out = '$WORK_DIR'
copied = 0

# Maven artifacts
for root, dirs, files in os.walk(m2):
    for f in files:
        if f.endswith(('.jar', '.pom', '.xml', '.module')):
            full = os.path.join(root, f)
            rel = os.path.relpath(full, os.path.dirname(m2))
            dest = os.path.join(out, 'maven', rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(full, dest)
            copied += 1

# npm packages from Maven artifacts
npm_artifacts = config.get('npm', [])
for artifact in npm_artifacts:
    artifact_dir = os.path.join(m2, artifact)
    if not os.path.isdir(artifact_dir):
        print(f'SKIP npm {artifact} — not in .m2', file=sys.stderr)
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
                            basename = os.path.basename(entry)
                            parts = basename.rsplit('-', 1)
                            pkg_name = parts[0] if len(parts) > 1 else basename.replace('.tgz','')
                            dest_dir = os.path.join(out, 'npm', '@casehubio', pkg_name, '-')
                            os.makedirs(dest_dir, exist_ok=True)
                            with zf.open(entry) as src, open(os.path.join(dest_dir, basename), 'wb') as dst:
                                dst.write(src.read())
                            copied += 1
            except Exception as e:
                print(f'WARN {jar_path}: {e}', file=sys.stderr)

print(f'{copied} files staged for publish')
"

# Landing page
cat > "$WORK_DIR/index.html" <<'HTML'
<!DOCTYPE html>
<html><head><title>CaseHub Packages</title></head>
<body>
<h1>CaseHub Public Packages</h1>
<p>Verified-green SNAPSHOT artifacts from <a href="https://github.com/casehubio/casehub-all">casehub-all</a>.</p>
<h2>Maven</h2>
<pre>&lt;repository&gt;
  &lt;id&gt;casehub-public&lt;/id&gt;
  &lt;url&gt;https://casehub.org/packages/maven&lt;/url&gt;
  &lt;snapshots&gt;&lt;enabled&gt;true&lt;/enabled&gt;&lt;/snapshots&gt;
&lt;/repository&gt;</pre>
<p>Or use <code>build-all.sh</code> from <a href="https://github.com/casehubio/parent">casehub-parent</a> to build from source.</p>
</body></html>
HTML

touch "$WORK_DIR/.nojekyll"

FILE_COUNT=$(find "$WORK_DIR" -type f | wc -l | tr -d ' ')
DIR_SIZE=$(du -sh "$WORK_DIR" | cut -f1)
echo "==> Staged $FILE_COUNT files ($DIR_SIZE)"

if [ "$DRY_RUN" = true ]; then
  echo "[dry-run] Would push $FILE_COUNT files to casehubio/packages"
  rm -rf "$WORK_DIR"
  exit 0
fi

echo "==> Publishing to casehubio/packages (orphan push)"
git -C "$WORK_DIR" init -b main
git -C "$WORK_DIR" add -A
git -C "$WORK_DIR" config user.email "github-actions@github.com"
git -C "$WORK_DIR" config user.name "casehub-all CI"
git -C "$WORK_DIR" commit -m "publish: verified-green artifacts from $(date -u +%Y-%m-%dT%H:%M:%SZ)"
git -C "$WORK_DIR" remote add origin "$PACKAGES_REPO"
git -C "$WORK_DIR" push --force origin main

rm -rf "$WORK_DIR"
echo "==> Published to https://casehub.org/packages/"
