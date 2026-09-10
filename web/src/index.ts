import { handleApi } from "./api";
import type { Bindings } from "./bindings";

export default {
  async fetch(request, env, ctx): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/api" || url.pathname.startsWith("/api/")) {
      return handleApi(request, env, url, ctx);
    }
    return env.ASSETS.fetch(request);
  },
} satisfies ExportedHandler<Bindings>;
