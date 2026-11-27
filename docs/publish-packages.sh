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

# Generate maven-metadata.xml for SNAPSHOT resolution
import re, hashlib
from datetime import datetime, timezone
now = datetime.now(timezone.utc)
ts = now.strftime('%Y%m%d.%H%M%S')
updated = now.strftime('%Y%m%d%H%M%S')

maven_dir = os.path.join(out, 'maven', 'casehub')
if os.path.isdir(maven_dir):
    for artifact_name in os.listdir(maven_dir):
        artifact_path = os.path.join(maven_dir, artifact_name)
        if not os.path.isdir(artifact_path):
            continue
        for version in os.listdir(artifact_path):
            version_path = os.path.join(artifact_path, version)
            if not os.path.isdir(version_path) or 'SNAPSHOT' not in version:
                continue
            base_version = version
            jars = [f for f in os.listdir(version_path) if f.endswith('.jar') and not f.endswith('-sources.jar') and not f.endswith('-javadoc.jar')]
            if not jars:
                continue
            # Find the build number from existing timestamped filenames
            build_num = 1
            for j in jars:
                m = re.search(r'-(\d+)\.jar$', j)
                if m:
                    build_num = max(build_num, int(m.group(1)))
            # Version metadata (in version dir)
            snapshot_version = base_version.replace('-SNAPSHOT', '') + '-' + ts + '-' + str(build_num)
            metadata = f'''<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<metadata modelVersion=\"1.1.0\">
  <groupId>io.casehub</groupId>
  <artifactId>{artifact_name}</artifactId>
  <version>{base_version}</version>
  <versioning>
    <snapshot>
      <timestamp>{ts}</timestamp>
      <buildNumber>{build_num}</buildNumber>
    </snapshot>
    <lastUpdated>{updated}</lastUpdated>
    <snapshotVersions>'''
            for f_name in os.listdir(version_path):
                if f_name.startswith(artifact_name):
                    ext_match = re.search(r'\.(jar|pom|module)$', f_name)
                    if ext_match:
                        ext = ext_match.group(1)
                        classifier = ''
                        # Check for classifier (e.g., -sources, -tests)
                        cm = re.search(rf'{re.escape(artifact_name)}-[^-]+-\d+-(\w+)\.{ext}$', f_name)
                        if cm:
                            classifier = cm.group(1)
                        metadata += f'''
      <snapshotVersion>
        {f'<classifier>{classifier}</classifier>' if classifier else ''}
        <extension>{ext}</extension>
        <value>{snapshot_version}</value>
        <updated>{updated}</updated>
      </snapshotVersion>'''
            metadata += '''
    </snapshotVersions>
  </versioning>
</metadata>
'''
            meta_path = os.path.join(version_path, 'maven-metadata.xml')
            with open(meta_path, 'w') as mf:
                mf.write(metadata)
            # Also write artifact-level metadata (parent dir)
            artifact_meta = f'''<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<metadata>
  <groupId>io.casehub</groupId>
  <artifactId>{artifact_name}</artifactId>
  <versioning>
    <latest>{base_version}</latest>
    <release>{base_version}</release>
    <versions>
      <version>{base_version}</version>
    </versions>
    <lastUpdated>{updated}</lastUpdated>
  </versioning>
</metadata>
'''
            with open(os.path.join(artifact_path, 'maven-metadata.xml'), 'w') as mf:
                mf.write(artifact_meta)
            copied += 2

print(f'{copied} files staged for publish')

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
