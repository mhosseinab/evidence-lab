interface ProxyContext {
  request: Request;
  env: { EVIDENCE_LAB_BACKEND_ORIGIN?: string };
}

function failure(detail: string, status: number): Response {
  return Response.json({ detail }, { status, headers: { "Cache-Control": "no-store" } });
}

export async function onRequest({ request, env }: ProxyContext): Promise<Response> {
  const incoming = new URL(request.url);
  if (
    incoming.pathname !== "/health" &&
    !incoming.pathname.startsWith("/health/") &&
    incoming.pathname !== "/api" &&
    !incoming.pathname.startsWith("/api/")
  ) {
    return failure("Not found.", 404);
  }

  let backend: URL;
  try {
    backend = new URL(env.EVIDENCE_LAB_BACKEND_ORIGIN ?? "");
    if (
      backend.protocol !== "https:" ||
      backend.username ||
      backend.password ||
      backend.pathname !== "/" ||
      backend.search ||
      backend.hash ||
      backend.origin === incoming.origin
    ) {
      return failure("The workspace backend is not configured correctly.", 503);
    }
  } catch {
    return failure("The workspace backend is not configured correctly.", 503);
  }

  const origin = request.headers.get("Origin");
  if (
    !["GET", "HEAD", "OPTIONS"].includes(request.method) &&
    ((origin && origin !== incoming.origin) || request.headers.get("Sec-Fetch-Site") === "cross-site")
  ) {
    return failure("Cross-origin writes are disabled.", 403);
  }

  backend.pathname = incoming.pathname;
  backend.search = incoming.search;
  const headers = new Headers(request.headers);
  for (const name of ["Host", "Cookie", "Forwarded", "X-Forwarded-Host", "X-Forwarded-Proto"]) {
    headers.delete(name);
  }
  // Preserve the browser boundary before translating the same-origin request for FastAPI.
  if (origin === incoming.origin) headers.set("Origin", backend.origin);

  try {
    const forwarded: RequestInit & { duplex: "half" } = {
      method: request.method,
      body: request.body,
      duplex: "half",
    };
    const upstream = await fetch(new Request(backend, forwarded), { headers, redirect: "manual" });
    // Never send a browser (or its authorization) to an upstream redirect target.
    if (upstream.status >= 300 && upstream.status < 400) {
      await upstream.body?.cancel();
      return failure("The workspace backend returned an unexpected redirect.", 502);
    }
    const responseHeaders = new Headers(upstream.headers);
    responseHeaders.delete("Set-Cookie");
    responseHeaders.set("Cache-Control", "no-store");
    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: responseHeaders,
    });
  } catch {
    return failure("Cannot reach the workspace backend.", 502);
  }
}
