import { SELF, env } from "cloudflare:test";
import { beforeAll, describe, expect, it } from "vitest";

// The uploader writes through wrangler with the same SQL shapes as below, so
// these tests seed D1 and R2 directly and exercise the read-only API.

const MACHINE = "apple-m4-pro-542f1e";
const EXPERIMENT = "exp-1";
const ENVIRONMENT = "aarch64-darwin-abc";

function sha(digit: number): string {
  return String(digit).repeat(40);
}

// Ordinal 0 is committed at 01:00 in UTC+2, an hour before the AIHC_SINCE
// cutoff of 2026-09-01T00:00:00Z, so every listing must start at ordinal 1.
async function seedCommits(): Promise<void> {
  const statement = env.DB.prepare(
    "INSERT INTO commits(sha, ordinal, committed_at, subject, tree_key) VALUES (?, ?, ?, ?, ?) ON CONFLICT(sha) DO UPDATE SET ordinal = excluded.ordinal",
  );
  await env.DB.batch([
    statement.bind(sha(0), 0, "2026-09-01T01:00:00+02:00", "before cutoff", "k0"),
    ...[1, 2, 3, 4].map((ordinal) => statement.bind(sha(ordinal), ordinal, "2026-09-01T00:00:00Z", `c${ordinal}`, ordinal === 2 ? "k1" : `k${ordinal}`)),
  ]);
}

async function seedRun(ordinal: number, runId: string, wall: number, inheritedFrom: string | null = null): Promise<string> {
  const rowId = inheritedFrom ? `${runId}~${sha(ordinal).slice(0, 12)}` : runId;
  const key = `raw/v2/${MACHINE}/${inheritedFrom ?? sha(ordinal)}/${runId}.json.gz`;
  if (!inheritedFrom) {
    const body = new Blob([JSON.stringify({ run_id: runId, aihc_commit: { sha: sha(ordinal) } })]).stream().pipeThrough(new CompressionStream("gzip"));
    await env.RAW.put(key, await new Response(body).arrayBuffer(), { httpMetadata: { contentType: "application/json", contentEncoding: "gzip" } });
  }
  const now = "2026-09-10T00:00:00Z";
  const statements = [
    env.DB.prepare("INSERT INTO machines(machine_id, token_hash, created_at, last_seen_at) VALUES (?, '', ?, ?) ON CONFLICT(machine_id) DO UPDATE SET last_seen_at = excluded.last_seen_at").bind(MACHINE, now, now),
    env.DB.prepare("INSERT OR IGNORE INTO environments(environment_id, machine_id, first_seen_at, record) VALUES (?, ?, ?, ?)").bind(ENVIRONMENT, MACHINE, now, JSON.stringify({ id: ENVIRONMENT, cpu_brand: "Apple M4 Pro" })),
    env.DB.prepare(
      "INSERT OR IGNORE INTO runs(run_id, machine_id, environment_id, experiment_id, commit_sha, commit_ordinal, compiler_status, unavailable_reason, inherited_from, created_at, uploaded_at, envelope_key) VALUES (?, ?, ?, ?, ?, ?, 'available', NULL, ?, ?, ?, ?)",
    ).bind(rowId, MACHINE, ENVIRONMENT, EXPERIMENT, sha(ordinal), ordinal, inheritedFrom, now, now, key),
  ];
  const measurement = env.DB.prepare(
    "INSERT OR REPLACE INTO measurements(run_id, machine_id, experiment_id, commit_ordinal, benchmark, configuration, compiler_family, compiler_version, backend, optimization, baseline, metric, unit, status, estimate, sample_count) VALUES (?, ?, ?, ?, 'fib', ?, ?, ?, 'native', ?, ?, ?, ?, ?, ?, ?)",
  );
  const cells: Array<[string, string, string, string, number, string, string, string, number | null]> = [
    ["aihc-native-semispace-O2", "aihc", sha(ordinal), "O2", 0, "wall_time", "ns", "ok", wall],
    ["aihc-native-semispace-O2", "aihc", sha(ordinal), "O2", 0, "peak_heap", "byte", "unavailable", null],
    ["aihc-native-semispace-O2", "aihc", sha(ordinal), "O2", 0, "compile_time", "ns", "ok", 3000],
    ["aihc-native-semispace-O2", "aihc", sha(ordinal), "O2", 0, "artifact_size", "byte", "ok", 5000],
    ["ghc-9.14.1-native-O2", "ghc", "9.14.1", "O2", 1, "wall_time", "ns", "ok", 100],
    ["ghc-9.14.1-native-O2", "ghc", "9.14.1", "O2", 1, "compile_time", "ns", "ok", 1000],
    ["ghc-9.14.1-native-O0", "ghc", "9.14.1", "O0", 1, "wall_time", "ns", "ok", 400],
  ];
  for (const [configuration, family, version, profile, baseline, metric, unit, status, estimate] of cells) {
    statements.push(measurement.bind(rowId, MACHINE, EXPERIMENT, ordinal, configuration, family, version, profile, baseline, metric, unit, status, estimate, estimate === null ? null : 2));
  }
  await env.DB.batch(statements);
  return key;
}

async function get(path: string) {
  const response = await SELF.fetch(`https://perf.aihc.app${path}`);
  return { status: response.status, body: (await response.json()) as any };
}

describe("perf.aihc.app API", () => {
  let envelopeKey = "";

  beforeAll(async () => {
    await seedCommits();
    await seedRun(0, "run-old", 50);
    envelopeKey = await seedRun(1, "run-1", 90);
    await seedRun(2, "run-1", 90, sha(1));
    await seedRun(4, "run-4", 180);
  });

  it("is read-only", async () => {
    for (const path of ["/api/upload", "/api/machines", "/api/commits"]) {
      const response = await SELF.fetch(`https://perf.aihc.app${path}`, { method: "POST", body: "{}" });
      expect(response.status, path).toBe(405);
    }
  });

  it("serves raw envelopes from R2", async () => {
    const raw = await SELF.fetch(`https://perf.aihc.app/api/${envelopeKey}`);
    expect(raw.status).toBe(200);
    expect(raw.headers.get("content-encoding")).toBe("gzip");
    const decoded = (await new Response(raw.body!.pipeThrough(new DecompressionStream("gzip"))).json()) as { run_id: string };
    expect(decoded.run_id).toBe("run-1");
    expect((await SELF.fetch("https://perf.aihc.app/api/raw/v2/nothing")).status).toBe(404);
  });

  it("serves the overview with AIHC to baseline ratios", async () => {
    const { status, body } = await get("/api/overview");
    expect(status).toBe(200);
    expect(body.experiment).toBe(EXPERIMENT);
    const machine = body.machines[0];
    expect(machine).toMatchObject({ machine_id: MACHINE, measured: 3, inherited: 1, total_commits: 4 });
    expect(body).toMatchObject({ first_ordinal: 1, head_ordinal: 4, total_commits: 4 });
    expect(machine.latest.sha).toBe(sha(4));
    expect(machine.ratios).toHaveLength(3);
    expect(machine.ratios).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ configuration: "aihc-native-semispace-O2", metric: "wall_time", value: 180, baseline_value: 100, ratio: 1.8 }),
        expect.objectContaining({ configuration: "aihc-native-semispace-O2", metric: "compile_time", value: 3000, baseline_value: 1000, ratio: 3 }),
        expect.objectContaining({ configuration: "aihc-native-semispace-O2", metric: "artifact_size", value: 5000, baseline_value: null, ratio: null }),
      ]),
    );
  });

  it("materializes the overview in R2 and serves it from there", async () => {
    const first = await get("/api/overview");
    const stored = await env.RAW.get("cache/overview/v1.json");
    expect(stored).not.toBeNull();
    const body = JSON.parse(await stored!.text());
    expect(body.computed_at).toBeTruthy();
    expect(body.machines[0].ratios).toEqual(first.body.machines[0].ratios);
    // A poisoned copy proves the next request is served from R2, and that
    // ?refresh=1 recomputes only once the copy is old enough.
    await env.RAW.put("cache/overview/v1.json", JSON.stringify({ ...body, experiment: "stale" }), { httpMetadata: { contentType: "application/json" } });
    expect((await get("/api/overview")).body.experiment).toBe("stale");
    expect((await get("/api/overview?refresh=1")).body.experiment).toBe("stale");
    expect((await get(`/api/overview?experiment=${EXPERIMENT}`)).body.experiment).toBe(EXPERIMENT);
    await env.RAW.delete("cache/overview/v1.json");
  });

  it("serves series with inherited points and profile filtering", async () => {
    const { body } = await get(`/api/series?machine=${MACHINE}&benchmark=fib&metric=wall_time&profile=O2`);
    const aihc = body.series.find((entry: any) => entry.configuration === "aihc-native-semispace-O2");
    expect(aihc.points).toEqual([
      [0, 50, "ok", 0],
      [1, 90, "ok", 0],
      [2, 90, "ok", 1],
      [4, 180, "ok", 0],
    ]);
    expect(body.series.every((entry: any) => entry.optimization === "O2")).toBe(true);
    expect(body.environments).toEqual([{ ordinal: 0, environment_id: ENVIRONMENT }]);
  });

  it("serves commit detail with parent deltas and unavailable cells", async () => {
    const { body } = await get(`/api/commit/${sha(2).slice(0, 12)}`);
    expect(body.commit.ordinal).toBe(2);
    expect(body.runs[0].inherited_from).toBe(sha(1));
    expect(body.runs[0].envelope_key).toBe(envelopeKey);
    const wall = body.measurements.find((row: any) => row.configuration === "aihc-native-semispace-O2" && row.metric === "wall_time");
    expect(wall).toMatchObject({ estimate: 90, parent_estimate: 90, ratio_to_parent: 1 });
    const heap = body.measurements.find((row: any) => row.configuration === "aihc-native-semispace-O2" && row.metric === "peak_heap");
    expect(heap.status).toBe("unavailable");
  });

  it("reports coverage per ordinal from the cutoff onwards", async () => {
    const { body } = await get(`/api/coverage?machine=${MACHINE}`);
    expect(body).toMatchObject({ first: 1, total: 4, statuses: "MI.M" });
  });

  it("lists commits, machines and experiments", async () => {
    expect((await get("/api/commits")).body.commits.map((c: any) => c.ordinal)).toEqual([4, 3, 2, 1]);
    const machines = (await get("/api/machines")).body.machines;
    expect(machines[0]).toMatchObject({ machine_id: MACHINE, measured_runs: 3, inherited_runs: 1 });
    expect(machines[0].environment.cpu_brand).toBe("Apple M4 Pro");
    expect((await get("/api/experiments")).body.active).toBe(EXPERIMENT);
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
