import { type NextRequest, NextResponse } from "next/server";

import { isPlatformMode } from "@/core/platform-mode";

/**
 * Platform-mode API proxy: forwards same-origin
 * /api/v1/employees/{name}/chat/... requests to the de-portal service.
 *
 * Why a route handler and not the next.config.js rewrites: rewrites to an
 * external destination do not forward the request's Cookie header, which
 * would strand the portal session; and they give no control over response
 * streaming. Here the Cookie header (HttpOnly session included) is carried
 * verbatim server-side, and the upstream body is passed through unbuffered
 * so LangGraph SDK fetch-streams (POST .../runs/stream) stay incremental.
 *
 * Active only when NEXT_PUBLIC_PLATFORM_MODE is set — otherwise this path
 * does not exist upstream and answers 404.
 */
const GATEWAY_BASE_URL =
  process.env.DEER_FLOW_INTERNAL_GATEWAY_BASE_URL ?? "http://127.0.0.1:8001";

// Hop-by-hop or decompression-managed headers are not forwarded; everything
// the portal and runtime need (content-type, content-disposition for
// downloads) is copied explicitly.
const FORWARDED_RESPONSE_HEADERS = [
  "content-type",
  "content-disposition",
  "content-language",
  "cache-control",
  "etag",
  "last-modified",
];

async function proxy(request: NextRequest, rest: string[]) {
  if (!isPlatformMode()) {
    return NextResponse.json({ error: "not found" }, { status: 404 });
  }
  const target = new URL(
    `/api/v1/employees/${rest.map(encodeURIComponent).join("/")}${request.nextUrl.search}`,
    GATEWAY_BASE_URL,
  );
  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("connection");
  headers.delete("content-length");

  const hasBody = !["GET", "HEAD"].includes(request.method);
  const upstream = await fetch(target, {
    method: request.method,
    headers,
    body: hasBody ? await request.arrayBuffer() : undefined,
    cache: "no-store",
  });

  const responseHeaders = new Headers();
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }
  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders,
  });
}

type RouteContext = { params: Promise<{ rest: string[] }> };

export async function GET(request: NextRequest, context: RouteContext) {
  return proxy(request, (await context.params).rest);
}

export async function POST(request: NextRequest, context: RouteContext) {
  return proxy(request, (await context.params).rest);
}

export async function PUT(request: NextRequest, context: RouteContext) {
  return proxy(request, (await context.params).rest);
}

export async function PATCH(request: NextRequest, context: RouteContext) {
  return proxy(request, (await context.params).rest);
}

export async function DELETE(request: NextRequest, context: RouteContext) {
  return proxy(request, (await context.params).rest);
}
