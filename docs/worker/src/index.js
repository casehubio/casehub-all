export default {
  async fetch(request, env) {
    try {
    const url = new URL(request.url);
    const key = url.pathname.slice(1) || 'progress';

    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: corsHeaders() });
    }

    if (request.method === 'GET') {
      if (key === 'progress') {
        const cache = caches.default;
        const cacheKey = new Request(url.toString(), { method: 'GET' });
        const cached = await cache.match(cacheKey);
        if (cached) return cached;

        const value = await env.KV.get('progress');
        const data = value ? JSON.parse(value) : {};

        if (data.current?.run_id) {
          const runId = data.current.run_id;
          const phases = (data.current.phases || []).filter(p => p.status !== 'success');
          await Promise.all(phases.map(async p => {
            const phaseKey = `phase-${runId}-${p.name.toLowerCase().replace(/[ :+/()]/g, '-')}`;
            try {
              const pd = await env.KV.get(phaseKey);
              if (pd) {
                const phase = JSON.parse(pd);
                if (phase.name) {
                  const match = data.current.phases.find(pp => pp.name.toLowerCase() === phase.name.toLowerCase());
                  if (match) Object.assign(match, { ...phase, name: match.name });
                }
              }
            } catch (e) {}
          }));
        }

        const response = new Response(JSON.stringify(data), {
          headers: { ...corsHeaders(), 'content-type': 'application/json', 'cache-control': 'max-age=15' }
        });
        await cache.put(cacheKey, response.clone());
        return response;
      }

      const value = await env.KV.get(key);
      if (!value) return new Response('{}', { headers: { ...corsHeaders(), 'content-type': 'application/json' } });
      return new Response(value, { headers: { ...corsHeaders(), 'content-type': 'application/json' } });
    }

    if (request.method === 'PUT') {
      const token = request.headers.get('Authorization')?.replace('Bearer ', '');
      if (token !== env.WRITE_TOKEN) {
        return new Response('Unauthorized', { status: 401 });
      }

      const runId = request.headers.get('X-Run-Id');
      const isInit = request.headers.get('X-Init') === 'true';
      const body = await request.text();

      // Server-side run_id ownership enforcement.
      // Init writes (X-Init: true) establish new ownership — always accepted.
      // All other writes with X-Run-Id must match the current owner.
      // This prevents cancelled/stale builds from corrupting the current build's data.
      if (runId && !isInit) {
        const progress = await env.KV.get('progress');
        if (progress) {
          try {
            const p = JSON.parse(progress);
            const ownerRunId = p?.current?.run_id;
            if (ownerRunId && parseInt(runId) !== ownerRunId) {
              return new Response(
                JSON.stringify({ error: 'stale_run', owner: ownerRunId, rejected: parseInt(runId) }),
                { status: 409, headers: { ...corsHeaders(), 'content-type': 'application/json' } }
              );
            }
          } catch (e) { /* parse error — allow write */ }
        }
      }

      // Phase files get a 24h TTL — per-build ephemeral data that self-expires.
      const kvOptions = key.startsWith('phase-') ? { expirationTtl: 86400 } : {};

      // Emit events on state transitions
      if (key.startsWith('phase-') && body) {
        try {
          const phase = JSON.parse(body);
          if (phase.name && phase.status) {
            const eventType = phase.status === 'success' ? 'phase.success'
              : phase.status === 'failure' ? 'phase.failure'
              : phase.status === 'in_progress' ? 'phase.started' : null;
            if (eventType) {
              await emitEvent(env, eventType, {
                phase: phase.name, run_id: parseInt(runId),
                duration_s: phase.duration_s, tests_run: phase.tests_run,
                tests_failed: phase.tests_failed
              });
            }
          }
        } catch (e) { /* non-JSON phase file — skip event */ }
      }
      if (key === 'progress') {
        try {
          const progress = JSON.parse(body);
          if (isInit && progress?.current?.run_id) {
            await emitEvent(env, 'build.started', {
              run_id: progress.current.run_id, commit: progress.current.commit,
              resume_from: progress.current.resume_from, test_mode: progress.current.test_mode
            });
          }
          const latest = progress?.history?.[0];
          if (latest?.conclusion && latest?.run_id) {
            const eventType = latest.conclusion === 'success' ? 'build.success'
              : latest.conclusion === 'cancelled' ? 'build.cancelled' : 'build.failure';
            await emitEvent(env, eventType, {
              run_id: latest.run_id, conclusion: latest.conclusion,
              duration_s: latest.duration_s, total_tests: latest.total_tests,
              test_mode: latest.test_mode
            });
          }
        } catch (e) { /* skip */ }
      }

      await env.KV.put(key, body, kvOptions);

      if (key === 'progress' || key.startsWith('phase-')) {
        const cache = caches.default;
        const progressUrl = new URL('/progress', url.origin).toString();
        await cache.delete(new Request(progressUrl, { method: 'GET' }));
      }

      return new Response('ok', { headers: corsHeaders() });
    }

    // DELETE endpoint removed — phase files self-expire via KV TTL.
    // No client needs explicit deletes. This avoids the free-tier
    // 1,000 delete/day limit that caused 429 errors.

    if (request.method === 'POST' && key.startsWith('github/')) {
      const ghToken = request.headers.get('X-GitHub-Token');
      if (!ghToken) return new Response('Missing X-GitHub-Token', { status: 401, headers: corsHeaders() });

      const action = key.slice(7);
      const body = await request.json();
      const repo = 'casehubio/casehub-all';

      let ghUrl, ghMethod, ghBody;
      if (action === 'dispatch') {
        ghUrl = `https://api.github.com/repos/${repo}/actions/workflows/mvn-all.yml/dispatches`;
        ghMethod = 'POST';
        ghBody = JSON.stringify({ ref: 'main', inputs: body });
      } else if (action === 'cancel') {
        ghUrl = `https://api.github.com/repos/${repo}/actions/runs/${body.run_id}/cancel`;
        ghMethod = 'POST';
        ghBody = null;
      } else {
        return new Response('Unknown action', { status: 400, headers: corsHeaders() });
      }

      const ghResp = await fetch(ghUrl, {
        method: ghMethod,
        headers: { 'Authorization': `Bearer ${ghToken}`, 'Accept': 'application/vnd.github+json', 'User-Agent': 'casehub-ci-dashboard', 'X-GitHub-Api-Version': '2022-11-28' },
        body: ghBody
      });
      return new Response(await ghResp.text(), { status: ghResp.status, headers: { ...corsHeaders(), 'content-type': 'application/json' } });
    }

    if (request.method === 'GET' && key === 'events') {
      const since = parseInt(url.searchParams.get('since') || '0');
      const filter = url.searchParams.get('filter') || '*';
      const raw = await env.KV.get('event-log');
      const events = raw ? JSON.parse(raw) : [];
      const filtered = events.filter(e => {
        if (e.ts <= since) return false;
        return matchFilter(e.type + (e.phase ? ':' + e.phase : ''), filter);
      });
      return new Response(JSON.stringify(filtered), {
        headers: { ...corsHeaders(), 'content-type': 'application/json' }
      });
    }

    return new Response('Method not allowed', { status: 405 });
    } catch (e) {
      return new Response(JSON.stringify({ error: e.message, stack: e.stack }), {
        status: 500,
        headers: { ...corsHeaders(), 'content-type': 'application/json' }
      });
    }
  }
};

function matchFilter(eventKey, filter) {
  return filter.split(',').some(pattern => {
    const re = new RegExp('^' + pattern.trim().replace(/\./g, '\\.').replace(/\*/g, '.*') + '$');
    return re.test(eventKey);
  });
}

async function emitEvent(env, type, data) {
  const raw = await env.KV.get('event-log');
  const events = raw ? JSON.parse(raw) : [];
  events.push({ type, ts: Date.now(), ...data });
  if (events.length > 500) events.splice(0, events.length - 500);
  await env.KV.put('event-log', JSON.stringify(events), { expirationTtl: 86400 });
}

function corsHeaders() {
  return {
    'access-control-allow-origin': '*',
    'access-control-allow-methods': 'GET, PUT, POST, DELETE, OPTIONS',
    'access-control-allow-headers': 'Content-Type, Authorization, X-Run-Id, X-Init, X-GitHub-Token',
  };
}
