"""
heal_resume.py — classify build errors and find the optimal resume level.

Maps errors to their source repo via package naming conventions, then
finds that repo's level in pipeline.yaml. The auto-retry resumes from
the source level, not the failed level.
"""

import re


# Package prefix → repo name mapping for io.casehub.*
# Order matters: longer prefixes match first
PACKAGE_REPO_MAP = [
    ('io.casehub.pages.', 'pages'),
    ('io.casehub.platform.', 'platform'),
    ('io.casehub.engine.', 'engine'),
    ('io.casehub.connectors.', 'connectors'),
    ('io.casehub.neocortex.', 'neocortex'),
    ('io.casehub.eidos.', 'eidos'),
    ('io.casehub.ledger.', 'ledger'),
    ('io.casehub.work.', 'work'),
    ('io.casehub.qhorus.', 'qhorus'),
    ('io.casehub.agency.', 'agency'),
    ('io.casehub.claudony.', 'claudony'),
    ('io.casehub.ops.', 'ops'),
    ('io.casehub.iot.', 'iot'),
    ('io.casehub.worker.', 'worker'),
    ('io.casehub.workers.', 'workers'),
    ('io.casehub.ras.', 'ras'),
    ('io.casehub.desiredstate.', 'desiredstate'),
    ('io.casehub.aml.', 'aml'),
    ('io.casehub.clinical.', 'clinical'),
    ('io.casehub.devtown.', 'devtown'),
    ('io.casehub.soc.', 'soc'),
    ('io.casehub.life.', 'life'),
    ('io.casehub.chat.', 'chat-app'),
    ('io.casehub.openclaw.', 'openclaw'),
    ('io.casehub.scaffold.', 'scaffold'),
    ('io.casehub.fsitrading.', 'fsitrading'),
    ('io.casehub.persistence.', 'work'),
    ('io.quarkmind.', 'quarkmind'),
]


def classify_error(error_text):
    """Classify a build error into a category."""
    if 'ClassNotFoundException' in error_text:
        match = re.search(r'ClassNotFoundException:\s*(\S+)', error_text)
        return {
            'type': 'upstream_artifact',
            'class': match.group(1) if match else None,
        }
    if '429' in error_text or 'Too Many Requests' in error_text:
        return {'type': 'infrastructure'}
    if 'invalid CEN header' in error_text or 'corrupt' in error_text.lower():
        return {'type': 'infrastructure'}
    if 'cannot find symbol' in error_text or 'does not exist' in error_text:
        return {'type': 'code_error'}
    return {'type': 'unknown'}


def map_class_to_repo(class_name):
    """Map a fully-qualified class name to a casehub repo name."""
    for prefix, repo in PACKAGE_REPO_MAP:
        if class_name.startswith(prefix):
            return repo
    return None


def find_repo_level(repo_name, pipeline):
    """Find which level a repo belongs to in the pipeline."""
    for level in pipeline.get('levels', []):
        names_map = level.get('names', {})
        for stack in level.get('stacks', []):
            if isinstance(stack, dict):
                repos = stack.get('repos', [])
                if repo_name in repos:
                    return level['id']
            elif isinstance(stack, list):
                for entry in stack:
                    if entry == repo_name or names_map.get(entry) == repo_name:
                        return level['id']
            elif isinstance(stack, str):
                if stack == repo_name:
                    return level['id']
    return None


def find_heal_level(error_text, failed_level, pipeline):
    """Find the optimal resume level for a build error.

    Returns the level to resume from:
    - upstream_artifact: resume from the source repo's level
    - infrastructure: resume from the failed level (retry in place)
    - code_error: resume from the failed level
    - unknown: resume from the failed level
    """
    classified = classify_error(error_text)

    if classified['type'] == 'upstream_artifact' and classified.get('class'):
        repo = map_class_to_repo(classified['class'])
        if repo:
            source_level = find_repo_level(repo, pipeline)
            if source_level:
                return source_level

    return failed_level
