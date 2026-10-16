/**
 * React hooks for file uploads
 */

import { useQuery } from "@tanstack/react-query";

import { getUploadLimits } from "./api";

/**
 * Hook to load the gateway-enforced upload limits.
 * Callers intentionally degrade to server-side validation if this request fails.
 */
export function useUploadLimits(threadId: string) {
  return useQuery({
    queryKey: ["uploads", "limits", threadId],
    queryFn: () => getUploadLimits(threadId),
    enabled: !!threadId,
    retry: false,
    staleTime: 60_000,
  });
}
