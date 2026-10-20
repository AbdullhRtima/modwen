import { useEffect, useRef } from "react";
import type { BBEvent } from "./api";
import type { Ctx } from "./App";

/** Run handler for every server event while the component is mounted (none skipped, latest handler used). */
export function useServerEvents(ctx: Ctx, handler: (e: BBEvent) => void): void {
  const ref = useRef(handler);
  ref.current = handler;
  useEffect(() => ctx.subscribe((e) => ref.current(e)), [ctx.subscribe]);
}
