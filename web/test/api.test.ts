import { SELF } from "cloudflare:test";
import { beforeAll, describe, expect, it } from "vitest";

const MACHINE = "apple-m4-pro-542f1e";
let token = "";

function envelope(sha: string, ordinal: number, runId: string, wall: number, extra: Record<string, unknown> = {}) {
  const results = [];
  for (const [configuration, family, backend, profile, baseline, estimate] of [
    ["aihc-native-semispace-O2", "aihc", "native", "O2", false, wall],
    ["ghc-9.14.1-native-O2", "ghc", "native", "O2", true, 100],
    ["ghc-9.14.1-native-O0", "ghc", "native", "O0", true, 400],
    ["aihc-native-semispace-O0", "aihc", "native", "O0", false, null],
  ] as const) {
    results.push({
      benchmark: "fib",
      configuration,
      compiler_family: family,
      compiler_version: family === "ghc" ? "9.14.1" : sha,
      backend,
      optimization: profile,
      baseline,
      compile: { status: estimate === null ? "unavailable" : "compiled" },
      measurement:
        estimate === null
          ? { status: "unavailable" }
          : {
              status: "converged",
              metrics: [
                { metric: "wall_time", unit: "ns", status: "ok", estimate, samples: [estimate, estimate] },
                { metric: "peak_heap", unit: "byte", status: "unavailable", estimate: null, samples: [] },
              ],
            },
    });
  }
  return {
    schema_version: 2,
    run_id: runId,
    created_at: "2026-09-10T00:00:00Z",
    experiment_id: "exp-1",
    platform: "aarch64-darwin",
    machine_id: MACHINE,
    environment: { id: "aarch64-darwin-abc", cpu_brand: "Apple M4 Pro" },
    aihc_commit: { sha, ordinal, committed_at: "2026-09-01T00:00:00Z", subject: `commit ${ordinal}`, tree_key: `k${ordinal}` },
    aihc_capabilities: {},
    compiler_status: "available",
    unavailable_reason: null,
    results,
    ...extra,
  };
}

async function post(path: string, body: unknown, auth: string, gzip = false) {
  let payload: BodyInit = JSON.stringify(body);
  const headers: Record<string, string> = { authorization: `Bearer ${auth}`, "content-type": "application/json" };
  if (gzip) {
    payload = await new Response(new Blob([payload]).stream().pipeThrough(new CompressionStream("gzip"))).arrayBuffer();
    headers["content-encoding"] = "gzip";
  }
  return SELF.fetch(`https://perf.aihc.app${path}`, { method: "POST", headers, body: payload });
}

async function get(path: string) {
  const response = await SELF.fetch(`https://perf.aihc.app${path}`);
  return { status: response.status, body: (await response.json()) as any };
}

describe("perf.aihc.app API", () => {
  beforeAll(async () => {
    const response = await post("/api/machines", { machine_id: MACHINE, display_name: "Lemmih's laptop" }, "admin-secret");
    expect(response.status).toBe(201);
    token = ((await response.json()) as { token: string }).token;
  });

  it("rejects machine registration without the admin token", async () => {
    const response = await post("/api/machines", { machine_id: "other" }, "nope");
    expect(response.status).toBe(403);
  });

  it("rejects uploads without a valid token and for other machines", async () => {
    expect((await post("/api/upload", envelope("a".repeat(40), 0, "run-a", 90), "bad")).status).toBe(403);
    const other = await post("/api/upload", { ...envelope("a".repeat(40), 0, "run-a", 90), machine_id: "someone-else" }, token);
    expect(other.status).toBe(403);
  });

  it("stores commits, runs, measurements and raw envelopes idempotently", async () => {
    const commits = await post(
      "/api/commits",
      { commits: [0, 1, 2, 3].map((ordinal) => ({ sha: String(ordinal).repeat(40), ordinal, committed_at: "2026-09-01T00:00:00Z", subject: `c${ordinal}`, tree_key: ordinal === 1 ? "k0" : `k${ordinal}` })) },
      token,
    );
    expect(commits.status).toBe(200);

    const first = await post("/api/upload", envelope("0".repeat(40), 0, "run-0", 90), token, true);
    expect(first.status).toBe(201);
    const created = (await first.json()) as { run_id: string; inserted: boolean; measurements: number; envelope_key: string };
    expect(created).toMatchObject({ run_id: "run-0", inserted: true, measurements: 6 });

    const again = await post("/api/upload", envelope("0".repeat(40), 0, "run-0", 90), token);
    expect(again.status).toBe(200);
    expect(((await again.json()) as { inserted: boolean }).inserted).toBe(false);

    const raw = await SELF.fetch(`https://perf.aihc.app/api/${created.envelope_key}`);
    expect(raw.status).toBe(200);
    expect(raw.headers.get("content-encoding")).toBe("gzip");
    const decoded = (await new Response(raw.body!.pipeThrough(new DecompressionStream("gzip"))).json()) as { run_id: string };
    expect(decoded.run_id).toBe("run-0");

    const inherited = await post("/api/upload", envelope("1".repeat(40), 1, "run-0", 90, { inherited_from: "0".repeat(40) }), token);
    expect(inherited.status).toBe(201);
    const inheritedBody = (await inherited.json()) as { run_id: string; envelope_key: string };
    expect(inheritedBody.run_id).toBe("run-0~111111111111");
    expect(inheritedBody.envelope_key).toBe(created.envelope_key);

    expect((await post("/api/upload", envelope("3".repeat(40), 3, "run-3", 180), token)).status).toBe(201);
  });

  it("serves the overview with AIHC to baseline ratios", async () => {
    const { status, body } = await get("/api/overview");
    expect(status).toBe(200);
    expect(body.experiment).toBe("exp-1");
    const machine = body.machines[0];
    expect(machine).toMatchObject({ machine_id: MACHINE, display_name: "Lemmih's laptop", measured: 2, inherited: 1, total_commits: 4 });
    expect(machine.latest.sha).toBe("3".repeat(40));
    expect(machine.ratios).toEqual([
      expect.objectContaining({ configuration: "aihc-native-semispace-O2", wall_time: 180, baseline_wall_time: 100, ratio: 1.8 }),
    ]);
  });

  it("serves series with inherited points and profile filtering", async () => {
    const { body } = await get(`/api/series?machine=${MACHINE}&benchmark=fib&metric=wall_time&profile=O2`);
    const aihc = body.series.find((entry: any) => entry.configuration === "aihc-native-semispace-O2");
    expect(aihc.points).toEqual([
      [0, 90, "ok", 0],
      [1, 90, "ok", 1],
      [3, 180, "ok", 0],
    ]);
    expect(body.series.every((entry: any) => entry.optimization === "O2")).toBe(true);
    expect(body.environments).toEqual([{ ordinal: 0, environment_id: "aarch64-darwin-abc" }]);
  });

  it("serves commit detail with parent deltas and unavailable cells", async () => {
    const { body } = await get(`/api/commit/${"1".repeat(12)}`);
    expect(body.commit.ordinal).toBe(1);
    expect(body.runs[0].inherited_from).toBe("0".repeat(40));
    const wall = body.measurements.find((row: any) => row.configuration === "aihc-native-semispace-O2" && row.metric === "wall_time");
    expect(wall).toMatchObject({ estimate: 90, parent_estimate: 90, ratio_to_parent: 1 });
    const heap = body.measurements.find((row: any) => row.configuration === "aihc-native-semispace-O2" && row.metric === "peak_heap");
    expect(heap.status).toBe("unavailable");
    expect(body.measurements.some((row: any) => row.configuration === "aihc-native-semispace-O0")).toBe(false);
  });

  it("reports coverage per ordinal", async () => {
    const { body } = await get(`/api/coverage?machine=${MACHINE}`);
    expect(body).toMatchObject({ total: 4, statuses: "MI.M" });
  });

  it("lists commits, machines and experiments", async () => {
    expect((await get("/api/commits")).body.commits.map((c: any) => c.ordinal)).toEqual([3, 2, 1, 0]);
    const machines = (await get("/api/machines")).body.machines;
    expect(machines[0]).toMatchObject({ machine_id: MACHINE, measured_runs: 2, inherited_runs: 1 });
    expect(machines[0].environment.cpu_brand).toBe("Apple M4 Pro");
    expect((await get("/api/experiments")).body.active).toBe("exp-1");
    expect((await get("/api/nothing")).status).toBe(404);
  });

  it("falls through to static assets", async () => {
    const response = await SELF.fetch("https://perf.aihc.app/");
    expect(response.status).toBe(200);
    expect(await response.text()).toContain("AIHC benchmarks");
    for (const page of ["/timeline.html", "/commit.html", "/coverage.html", "/site.js", "/site.css", "/vendor/uPlot.iife.min.js"]) {
      expect((await SELF.fetch(`https://perf.aihc.app${page}`)).status, page).toBe(200);
    }
    expect((await SELF.fetch("https://perf.aihc.app/missing")).status).toBe(404);
  });
});
