export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const key = url.pathname.slice(1);

    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: corsHeaders() });
    }

    if (request.method === 'GET' || request.method === 'HEAD') {
      if (!key || key === '') {
        return new Response(INDEX_HTML, {
          headers: { ...corsHeaders(), 'content-type': 'text/html' },
        });
      }

      const object = await env.BUCKET.get(key);
      if (!object) {
        return new Response('Not Found', { status: 404, headers: corsHeaders() });
      }

      const headers = {
        ...corsHeaders(),
        'content-type': contentType(key),
        'etag': object.httpEtag,
        'cache-control': cacheControl(key),
      };

      if (request.method === 'HEAD') {
        return new Response(null, { headers });
      }

      return new Response(object.body, { headers });
    }

    if (request.method === 'PUT') {
      const token = request.headers.get('Authorization')?.replace('Bearer ', '');
      if (token !== env.WRITE_TOKEN) {
        return new Response('Unauthorized', { status: 401, headers: corsHeaders() });
      }

      const body = await request.arrayBuffer();
      await env.BUCKET.put(key, body, {
        httpMetadata: { contentType: contentType(key) },
      });

      return new Response('ok', { headers: corsHeaders() });
    }

    if (request.method === 'DELETE') {
      const token = request.headers.get('Authorization')?.replace('Bearer ', '');
      if (token !== env.WRITE_TOKEN) {
        return new Response('Unauthorized', { status: 401, headers: corsHeaders() });
      }

      await env.BUCKET.delete(key);
      return new Response('ok', { headers: corsHeaders() });
    }

    return new Response('Method Not Allowed', { status: 405, headers: corsHeaders() });
  },
};

function corsHeaders() {
  return {
    'access-control-allow-origin': '*',
    'access-control-allow-methods': 'GET, HEAD, PUT, DELETE, OPTIONS',
    'access-control-allow-headers': 'Authorization, Content-Type',
  };
}

function contentType(key) {
  if (key.endsWith('.pom') || key.endsWith('.xml')) return 'application/xml';
  if (key.endsWith('.jar')) return 'application/java-archive';
  if (key.endsWith('.sha1')) return 'text/plain';
  if (key.endsWith('.sha256')) return 'text/plain';
  if (key.endsWith('.sha512')) return 'text/plain';
  if (key.endsWith('.md5')) return 'text/plain';
  if (key.endsWith('.asc')) return 'text/plain';
  if (key.endsWith('.tgz')) return 'application/gzip';
  if (key.endsWith('.json')) return 'application/json';
  return 'application/octet-stream';
}

function cacheControl(key) {
  if (key.includes('SNAPSHOT')) return 'public, max-age=300';
  if (key.endsWith('maven-metadata.xml')) return 'public, max-age=300';
  return 'public, max-age=86400, immutable';
}

const INDEX_HTML = `<!DOCTYPE html>
<html><head><title>CaseHub Packages</title></head>
<body style="font-family:monospace;max-width:600px;margin:40px auto;padding:20px">
<h1>CaseHub Packages</h1>
<p>Public Maven and npm artifacts from the CaseHub ecosystem.</p>
<h2>Maven</h2>
<pre>&lt;repository&gt;
  &lt;id&gt;casehub&lt;/id&gt;
  &lt;url&gt;https://packages.casehub.org/maven&lt;/url&gt;
&lt;/repository&gt;</pre>
<h2>npm</h2>
<pre>@casehubio:registry=https://packages.casehub.org/npm</pre>
<p><a href="https://casehub.org/casehub-all/">CI Dashboard</a></p>
</body></html>`;
