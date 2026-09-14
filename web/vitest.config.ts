import { cloudflareTest, readD1Migrations } from "@cloudflare/vitest-pool-workers";
import path from "node:path";
import { defineConfig } from "vitest/config";

export default defineConfig(async () => {
  const migrations = await readD1Migrations(path.join(__dirname, "migrations"));
  return {
    plugins: [
      cloudflareTest({
        wrangler: { configPath: "./wrangler.jsonc" },
        miniflare: {
          // The cutoff is pinned here rather than taken from wrangler.jsonc:
          // the fixtures exercise the boundary itself, so they must not move
          // every time the production AIHC_SINCE advances.
          bindings: { TEST_MIGRATIONS: migrations, AIHC_SINCE: "2026-09-01T00:00:00Z" },
        },
      }),
    ],
    test: {
      setupFiles: ["./test/apply-migrations.ts"],
    },
  };
});
