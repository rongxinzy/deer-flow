"use client";

import { ArrowLeft, Bot } from "lucide-react";
import { useEffect, useState } from "react";

import { useI18n } from "@/core/i18n/hooks";
import {
  getPlatformConsoleURL,
  getPlatformEmployeeDisplay,
  getPlatformEmployeeName,
  isPlatformMode,
} from "@/core/platform-mode";

import { Tooltip } from "./tooltip";

/**
 * Platform mode header context: which digital employee this chat belongs to
 * (the portal mirrors the display name into a readable cookie on selection)
 * and the way back to the console workbench. Renders nothing outside
 * platform mode. Mounted in the chat view header (the landing surface after
 * the workbench handoff) and in the workspace container header (list pages).
 */
export function PlatformHeaderActions() {
  const { t } = useI18n();
  // Cookie-derived values only exist client-side; render them after mount so
  // the server and first client render agree (hydration-safe).
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    setMounted(true);
  }, []);
  if (!isPlatformMode()) return null;
  const employee = mounted
    ? (getPlatformEmployeeDisplay() ?? getPlatformEmployeeName())
    : null;
  const consoleURL = mounted ? getPlatformConsoleURL() : "";
  return (
    <div className="flex items-center gap-3">
      {employee ? (
        <Tooltip content={t.workspace.currentEmployee}>
          <span className="text-muted-foreground flex max-w-40 min-w-0 items-center gap-1.5 text-sm">
            <Bot size={16} className="shrink-0" />
            <span className="truncate">{employee}</span>
          </span>
        </Tooltip>
      ) : null}
      {consoleURL ? (
        <a
          href={consoleURL}
          className="text-muted-foreground hover:text-foreground flex items-center gap-1 text-sm"
        >
          <ArrowLeft size={14} className="shrink-0" />
          <span className="hidden sm:inline">{t.sidebar.backToWorkbench}</span>
        </a>
      ) : null}
    </div>
  );
}
