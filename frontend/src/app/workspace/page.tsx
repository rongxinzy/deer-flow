import { redirect } from "next/navigation";

import { DEMO_THREAD_IDS } from "@/core/threads/static-demo";
import { env } from "@/env";

export default async function WorkspacePage({
  searchParams,
}: Readonly<{
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}>) {
  // Preserve the query (the admin console's ?embed=1 must survive this
  // hop, or the framed app loses its embed chrome on landing).
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(await searchParams)) {
    for (const item of Array.isArray(value) ? value : [value]) {
      if (item !== undefined) params.append(key, item);
    }
  }
  const query = params.toString();
  if (env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY === "true") {
    return redirect(
      `/workspace/chats/${DEMO_THREAD_IDS[0]}${query ? `?${query}` : ""}`,
    );
  }
  return redirect(`/workspace/chats/new${query ? `?${query}` : ""}`);
}
