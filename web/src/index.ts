import { handleApi, refreshOverview } from "./api";
import type { Bindings } from "./bindings";

export default {
  async fetch(request, env, ctx): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/api" || url.pathname.startsWith("/api/")) {
      return handleApi(request, env, url, ctx);
    }
    return env.ASSETS.fetch(request);
  },
  /** Cron trigger: keep the materialized overview current between visits. */
  async scheduled(_event, env, ctx): Promise<void> {
    ctx.waitUntil(refreshOverview(env).catch((error) => console.error("overview refresh failed", error)));
  },
} satisfies ExportedHandler<Bindings>;
