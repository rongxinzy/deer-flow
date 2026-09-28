"use client";

import { useEffect } from "react";

import { useSidebar } from "@/components/ui/sidebar";
import { isEmbedMode } from "@/core/platform-mode";

/**
 * In embed mode (framed by the admin console) the console already provides
 * the employee rail, so the sidebar starts collapsed and the conversation
 * canvas gets the width. The user can still reopen it from the trigger.
 */
export function EmbedAutoCollapse() {
  const { setOpen } = useSidebar();
  useEffect(() => {
    if (isEmbedMode()) setOpen(false);
  }, [setOpen]);
  return null;
}
