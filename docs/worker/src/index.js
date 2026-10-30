export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const key = url.pathname.slice(1) || 'progress';

    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: corsHeaders() });
    }

    if (request.method === 'GET') {
      const value = await env.KV.get(key);
      if (!value) return new Response('{}', { headers: { ...corsHeaders(), 'content-type': 'application/json' } });
      return new Response(value, { headers: { ...corsHeaders(), 'content-type': 'application/json' } });
    }

    if (request.method === 'PUT') {
      const token = request.headers.get('Authorization')?.replace('Bearer ', '');
      if (token !== env.WRITE_TOKEN) {
        return new Response('Unauthorized', { status: 401 });
      }
      const body = await request.text();
      await env.KV.put(key, body);
      return new Response('ok', { headers: corsHeaders() });
    }

    if (request.method === 'DELETE') {
      const token = request.headers.get('Authorization')?.replace('Bearer ', '');
      if (token !== env.WRITE_TOKEN) {
        return new Response('Unauthorized', { status: 401 });
      }
      await env.KV.delete(key);
      return new Response('ok', { headers: corsHeaders() });
    }

    return new Response('Method not allowed', { status: 405 });
  }
};

function corsHeaders() {
  return {
    'access-control-allow-origin': '*',
    'access-control-allow-methods': 'GET, PUT, DELETE, OPTIONS',
    'access-control-allow-headers': 'Content-Type, Authorization',
  };
}
