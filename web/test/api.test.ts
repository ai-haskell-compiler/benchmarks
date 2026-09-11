import { SELF, env } from "cloudflare:test";
import { beforeAll, describe, expect, it } from "vitest";

// The uploader writes through wrangler with the same SQL shapes as below, so
// these tests seed D1 and R2 directly and exercise the read-only API.

const MACHINE = "apple-m4-pro-542f1e";
// Experiments are per benchmark: fib was measured for the whole history, ack
// was added later and has only been filled in for ordinals 1 and 4.
const EXPERIMENT = "fib-1";
const ACK = "ack-2";
const SUITE = "core-both";
const OLD_SUITE = "core-fib";
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

async function seedSuites(): Promise<void> {
  const statement = env.DB.prepare("INSERT INTO suites(suite_key, suite_id, experiments, uploaded_at) VALUES (?, ?, ?, ?) ON CONFLICT(suite_key) DO UPDATE SET uploaded_at = excluded.uploaded_at");
  await env.DB.batch([
    statement.bind(OLD_SUITE, "core", JSON.stringify({ fib: EXPERIMENT }), "2026-09-09T00:00:00Z"),
    statement.bind(SUITE, "core", JSON.stringify({ fib: EXPERIMENT, ack: ACK }), "2026-09-10T00:00:00Z"),
  ]);
}

async function seedRun(ordinal: number, runId: string, wall: number, inheritedFrom: string | null = null, experiment = EXPERIMENT, benchmark = "fib"): Promise<string> {
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
    ).bind(rowId, MACHINE, ENVIRONMENT, experiment, sha(ordinal), ordinal, inheritedFrom, now, now, key),
  ];
  const measurement = env.DB.prepare(
    "INSERT OR REPLACE INTO measurements(run_id, machine_id, experiment_id, commit_ordinal, benchmark, configuration, compiler_family, compiler_version, backend, optimization, baseline, metric, unit, status, estimate, sample_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'native', ?, ?, ?, ?, ?, ?, ?)",
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
    statements.push(measurement.bind(rowId, MACHINE, experiment, ordinal, benchmark, configuration, family, version, profile, baseline, metric, unit, status, estimate, estimate === null ? null : 2));
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
    await seedSuites();
    await seedRun(0, "run-old", 50);
    envelopeKey = await seedRun(1, "run-1", 90);
    await seedRun(2, "run-1", 90, sha(1));
    await seedRun(4, "run-4", 180);
    await seedRun(1, "run-ack-1", 250, null, ACK, "ack");
    await seedRun(4, "run-ack-4", 300, null, ACK, "ack");
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

  it("serves the overview for the active suite with AIHC to baseline ratios", async () => {
    const { status, body } = await get("/api/overview");
    expect(status).toBe(200);
    expect(body.suite).toBe(SUITE);
    expect(body.benchmarks).toEqual({ fib: EXPERIMENT, ack: ACK });
    const machine = body.machines[0];
    // Only commits with a run for every benchmark count as covered: ordinals
    // 1 and 4 have both, ordinals 0 and 2 only fib.
    expect(machine).toMatchObject({ machine_id: MACHINE, measured: 2, inherited: 0, total_commits: 4 });
    expect(body).toMatchObject({ first_ordinal: 1, head_ordinal: 4, total_commits: 4 });
    expect(machine.latest.sha).toBe(sha(4));
    expect(machine.ratios).toHaveLength(6);
    expect(machine.ratios).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ benchmark: "fib", configuration: "aihc-native-semispace-O2", metric: "wall_time", value: 180, baseline_value: 100, ratio: 1.8 }),
        expect.objectContaining({ benchmark: "fib", configuration: "aihc-native-semispace-O2", metric: "compile_time", value: 3000, baseline_value: 1000, ratio: 3 }),
        expect.objectContaining({ benchmark: "fib", configuration: "aihc-native-semispace-O2", metric: "artifact_size", value: 5000, baseline_value: null, ratio: null }),
        expect.objectContaining({ benchmark: "ack", configuration: "aihc-native-semispace-O2", metric: "wall_time", value: 300, baseline_value: 100, ratio: 3 }),
      ]),
    );
  });

  it("selects older suites and single experiments by query", async () => {
    const old = (await get(`/api/overview?suite=${OLD_SUITE}`)).body;
    expect(old.suite).toBe(OLD_SUITE);
    expect(old.machines[0]).toMatchObject({ measured: 3, inherited: 1 });
    expect(old.machines[0].ratios).toHaveLength(3);
    const single = (await get(`/api/overview?experiment=${ACK}`)).body;
    expect(single).toMatchObject({ suite: ACK, benchmarks: {} });
    expect(single.machines[0].ratios.every((row: any) => row.benchmark === "ack")).toBe(true);
    expect((await get("/api/overview?suite=nope")).status).toBe(404);
  });

  it("prefers the newest commit measured for the whole suite", async () => {
    // Head is measured for ack only, so the headline stays at ordinal 4.
    await seedCommits();
    await env.DB.prepare("INSERT OR IGNORE INTO commits(sha, ordinal, committed_at, subject, tree_key) VALUES (?, 5, '2026-09-02T00:00:00Z', 'c5', 'k5')").bind(sha(5)).run();
    await seedRun(5, "run-ack-5", 310, null, ACK, "ack");
    const body = (await get(`/api/overview?suite=${SUITE}`)).body;
    expect(body.machines[0].latest.sha).toBe(sha(4));
    expect((await get(`/api/coverage?machine=${MACHINE}`)).body.statuses).toBe("MP.MP");
    await env.DB.prepare("DELETE FROM measurements WHERE commit_ordinal = 5").run();
    await env.DB.prepare("DELETE FROM runs WHERE commit_ordinal = 5").run();
    await env.DB.prepare("DELETE FROM commits WHERE ordinal = 5").run();
  });

  it("materializes the overview in R2 and serves it from there", async () => {
    const first = await get("/api/overview");
    const stored = await env.RAW.get("cache/overview/v2.json");
    expect(stored).not.toBeNull();
    const body = JSON.parse(await stored!.text());
    expect(body.computed_at).toBeTruthy();
    expect(body.machines[0].ratios).toEqual(first.body.machines[0].ratios);
    // A poisoned copy proves the next request is served from R2, and that
    // ?refresh=1 recomputes only once the copy is old enough.
    await env.RAW.put("cache/overview/v2.json", JSON.stringify({ ...body, suite: "stale" }), { httpMetadata: { contentType: "application/json" } });
    expect((await get("/api/overview")).body.suite).toBe("stale");
    expect((await get("/api/overview?refresh=1")).body.suite).toBe("stale");
    expect((await get(`/api/overview?suite=${SUITE}`)).body.suite).toBe(SUITE);
    await env.RAW.delete("cache/overview/v2.json");
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
    const ack = (await get(`/api/series?machine=${MACHINE}&benchmark=ack&metric=wall_time&profile=O2`)).body;
    expect(ack.series.find((entry: any) => entry.configuration === "aihc-native-semispace-O2").points).toEqual([
      [1, 250, "ok", 0],
      [4, 300, "ok", 0],
    ]);
  });

  it("serves commit detail with parent deltas and unavailable cells", async () => {
    const { body } = await get(`/api/commit/${sha(2).slice(0, 12)}`);
    expect(body.commit.ordinal).toBe(2);
    expect(body.benchmarks).toEqual({ fib: EXPERIMENT, ack: ACK });
    expect(body.runs).toHaveLength(1);
    expect(body.runs[0]).toMatchObject({ inherited_from: sha(1), envelope_key: envelopeKey, experiment_id: EXPERIMENT });
    const head = (await get(`/api/commit/${sha(4)}`)).body;
    expect(head.runs.map((run: any) => run.experiment_id)).toEqual([ACK, EXPERIMENT]);
    const wall = body.measurements.find((row: any) => row.configuration === "aihc-native-semispace-O2" && row.metric === "wall_time");
    expect(wall).toMatchObject({ estimate: 90, parent_estimate: 90, ratio_to_parent: 1 });
    const heap = body.measurements.find((row: any) => row.configuration === "aihc-native-semispace-O2" && row.metric === "peak_heap");
    expect(heap.status).toBe("unavailable");
  });

  it("reports coverage per ordinal from the cutoff onwards", async () => {
    const { body } = await get(`/api/coverage?machine=${MACHINE}`);
    // Ordinal 2 is inherited for fib but has no ack run yet, so it is partial.
    expect(body).toMatchObject({ first: 1, total: 4, statuses: "MP.M" });
    expect((await get(`/api/coverage?machine=${MACHINE}&suite=${OLD_SUITE}`)).body.statuses).toBe("MI.M");
  });

  it("lists commits, machines and experiments", async () => {
    expect((await get("/api/commits")).body.commits.map((c: any) => c.ordinal)).toEqual([4, 3, 2, 1]);
    const machines = (await get("/api/machines")).body.machines;
    expect(machines[0]).toMatchObject({ machine_id: MACHINE, measured_runs: 5, inherited_runs: 1 });
    expect(machines[0].environment.cpu_brand).toBe("Apple M4 Pro");
    const experiments = (await get("/api/experiments")).body;
    expect(experiments.active).toBe(SUITE);
    expect(experiments.suites.map((suite: any) => suite.suite_key)).toEqual([SUITE, OLD_SUITE]);
    expect(experiments.suites[0].experiments).toEqual({ fib: EXPERIMENT, ack: ACK });
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
