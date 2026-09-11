import type { Bindings } from "./bindings";

const CACHE_SECONDS = 60;
/**
 * The overview is materialized in R2 so the front page never waits for D1.
 * A copy older than `OVERVIEW_FRESH_SECONDS` is served as is and recomputed in
 * the background; `?refresh=1` (sent by the uploader) recomputes synchronously
 * unless the copy is younger than `OVERVIEW_FORCE_SECONDS`.
 */
const OVERVIEW_KEY = "cache/overview/v2.json";
const OVERVIEW_FRESH_SECONDS = 60;
const OVERVIEW_FORCE_SECONDS = 10;

type Json = Record<string, unknown>;

class HttpError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function handleApi(request: Request, env: Bindings, url: URL, ctx: ExecutionContext): Promise<Response> {
  try {
    const segments = url.pathname.split("/").filter(Boolean).slice(1);
    const head = segments[0] ?? "";
    if (request.method !== "GET" && request.method !== "HEAD") throw new HttpError(405, "method not allowed");
    switch (head) {
      case "raw":
        return await serveRaw(env, segments.join("/"));
      case "commits":
        return cached(await listCommits(env, url));
      case "machines":
        return cached(await listMachines(env));
      case "experiments":
        return cached(await listExperiments(env));
      case "overview":
        return await overviewCached(env, url, ctx);
      case "series":
        return cached(await series(env, url));
      case "commit":
        if (segments.length === 2) return cached(await commitDetail(env, segments[1], url));
        break;
      case "coverage":
        return cached(await coverage(env, url));
    }
    throw new HttpError(404, "unknown endpoint");
  } catch (error) {
    if (error instanceof HttpError) return json({ error: error.message }, error.status);
    console.error(error);
    return json({ error: "internal error" }, 500);
  }
}

// ---------------------------------------------------------------------------
// Responses and helpers

function json(data: unknown, status = 200, headers: HeadersInit = {}): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "access-control-allow-origin": "*", ...headers },
  });
}

function cached(data: unknown): Response {
  return json(data, 200, { "cache-control": `public, max-age=${CACHE_SECONDS}` });
}

function cachedBody(body: BodyInit, ageSeconds: number): Response {
  return new Response(body, {
    status: 200,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "access-control-allow-origin": "*",
      "cache-control": `public, max-age=${CACHE_SECONDS}`,
      age: String(Math.floor(ageSeconds)),
    },
  });
}

/**
 * The set of experiments the site shows. Experiments are per benchmark; the
 * uploader records the suite it ran with, mapping benchmark id to experiment
 * id, and the most recently uploaded suite is active. `?suite=<key>` selects
 * another recorded suite and `?experiment=<id>` a single experiment.
 */
interface Suite {
  key: string;
  /** Benchmark id to experiment id; empty when a single experiment was requested. */
  benchmarks: Record<string, string>;
  experiments: string[];
}

async function activeSuite(env: Bindings, url: URL): Promise<Suite> {
  const experiment = url.searchParams.get("experiment");
  if (experiment) return { key: experiment, benchmarks: {}, experiments: [experiment] };
  const requested = url.searchParams.get("suite");
  const row = requested
    ? await env.DB.prepare("SELECT suite_key, experiments FROM suites WHERE suite_key = ?").bind(requested).first<{ suite_key: string; experiments: string }>()
    : await env.DB.prepare("SELECT suite_key, experiments FROM suites ORDER BY uploaded_at DESC LIMIT 1").first<{ suite_key: string; experiments: string }>();
  if (!row) throw new HttpError(404, requested ? "unknown suite" : "no results have been uploaded");
  const benchmarks = JSON.parse(row.experiments) as Record<string, string>;
  const experiments = [...new Set(Object.values(benchmarks))].sort();
  // Suites backfilled from suite-level uploads without measurements carry no
  // mapping; such a suite is its single experiment.
  return { key: row.suite_key, benchmarks, experiments: experiments.length ? experiments : [row.suite_key] };
}

/** Placeholders for `experiment_id IN (...)`; bind `suite.experiments` after the preceding parameters. */
function experimentList(suite: Suite): string {
  return suite.experiments.map(() => "?").join(", ");
}

function suiteFields(suite: Suite): { suite: string; benchmarks: Record<string, string> } {
  return { suite: suite.key, benchmarks: suite.benchmarks };
}

function requireParam(url: URL, name: string): string {
  const value = url.searchParams.get(name);
  if (!value) throw new HttpError(400, `missing query parameter ${name}`);
  return value;
}

interface CommitWindow {
  /** Ordinal of the first commit at or after the cutoff, or null when there are none. */
  first: number | null;
  head: number | null;
  total: number;
}

/**
 * The commits the site shows: those committed at or after `AIHC_SINCE`.
 * `committed_at` carries a zone offset, so the comparison goes through
 * SQLite's `datetime`, which normalizes both sides to UTC.
 */
function sinceClause(env: Bindings): { sql: string; params: string[] } {
  const since = env.AIHC_SINCE?.trim();
  if (!since) return { sql: "1", params: [] };
  return { sql: "datetime(committed_at) >= datetime(?)", params: [since] };
}

async function commitWindow(env: Bindings): Promise<CommitWindow> {
  const since = sinceClause(env);
  const row = await env.DB.prepare(`SELECT MIN(ordinal) AS first, MAX(ordinal) AS head, COUNT(*) AS n FROM commits WHERE ${since.sql}`)
    .bind(...since.params)
    .first<{ first: number | null; head: number | null; n: number }>();
  return { first: row?.first ?? null, head: row?.head ?? null, total: row?.n ?? 0 };
}

// ---------------------------------------------------------------------------
// Reads

async function serveRaw(env: Bindings, key: string): Promise<Response> {
  if (!key.startsWith("raw/")) throw new HttpError(404, "not found");
  const object = await env.RAW.get(key);
  if (!object) throw new HttpError(404, "not found");
  return new Response(object.body, {
    headers: {
      "content-type": "application/json",
      "content-encoding": "gzip",
      "cache-control": "public, max-age=31536000, immutable",
      "access-control-allow-origin": "*",
    },
  });
}

async function listCommits(env: Bindings, url: URL): Promise<unknown> {
  const limit = Math.min(Number(url.searchParams.get("limit") ?? 5000), 5000);
  const since = sinceClause(env);
  const rows = await env.DB.prepare(`SELECT sha, ordinal, committed_at, subject, tree_key FROM commits WHERE ${since.sql} ORDER BY ordinal DESC LIMIT ?`)
    .bind(...since.params, limit)
    .all();
  return { commits: rows.results };
}

async function listMachines(env: Bindings): Promise<unknown> {
  const rows = await env.DB.prepare(
    "SELECT m.machine_id, m.created_at, m.last_seen_at, " +
      "(SELECT record FROM environments e WHERE e.machine_id = m.machine_id ORDER BY first_seen_at DESC LIMIT 1) AS environment, " +
      "(SELECT COUNT(*) FROM runs r WHERE r.machine_id = m.machine_id AND r.inherited_from IS NULL) AS measured_runs, " +
      "(SELECT COUNT(*) FROM runs r WHERE r.machine_id = m.machine_id AND r.inherited_from IS NOT NULL) AS inherited_runs " +
      "FROM machines m ORDER BY m.machine_id",
  ).all<{ environment: string | null } & Json>();
  return {
    machines: rows.results.map((row) => ({ ...row, environment: row.environment ? JSON.parse(row.environment) : null })),
  };
}

async function listExperiments(env: Bindings): Promise<unknown> {
  const suites = await env.DB.prepare("SELECT suite_key, suite_id, experiments, uploaded_at FROM suites ORDER BY uploaded_at DESC").all<{ experiments: string } & Json>();
  const rows = await env.DB.prepare(
    "SELECT experiment_id, machine_id, COUNT(*) AS runs, MAX(uploaded_at) AS last_upload FROM runs GROUP BY experiment_id, machine_id ORDER BY last_upload DESC",
  ).all();
  return {
    active: suites.results[0]?.suite_key ?? null,
    suites: suites.results.map((row) => ({ ...row, experiments: JSON.parse(row.experiments) })),
    experiments: rows.results,
  };
}

async function overviewCached(env: Bindings, url: URL, ctx: ExecutionContext): Promise<Response> {
  if (url.searchParams.get("experiment") || url.searchParams.get("suite")) return cached(await overview(env, await activeSuite(env, url)));
  const object = await env.RAW.get(OVERVIEW_KEY);
  if (object) {
    const ageSeconds = (Date.now() - object.uploaded.getTime()) / 1000;
    const force = url.searchParams.has("refresh") && ageSeconds >= OVERVIEW_FORCE_SECONDS;
    if (!force) {
      if (ageSeconds >= OVERVIEW_FRESH_SECONDS) ctx.waitUntil(refreshOverview(env).catch((error) => console.error("overview refresh failed", error)));
      return cachedBody(object.body, ageSeconds);
    }
  }
  return cachedBody(await refreshOverview(env), 0);
}

/** Recompute the overview for the active experiment and store it in R2. */
async function refreshOverview(env: Bindings): Promise<string> {
  const suite = await activeSuite(env, new URL("https://perf.aihc.app/api/overview"));
  const data = await overview(env, suite);
  const body = JSON.stringify({ ...data, computed_at: new Date().toISOString() });
  await env.RAW.put(OVERVIEW_KEY, body, { httpMetadata: { contentType: "application/json" } });
  return body;
}

interface CoverageRow {
  machine_id: string;
  commit_ordinal: number;
  experiments: number;
  measured_runs: number;
  measured_available: number;
}

/**
 * Per machine and commit, how many of the suite's experiments have a run and
 * how many of those were measured (not inherited) with a working compiler.
 */
async function coverageRows(env: Bindings, suite: Suite): Promise<CoverageRow[]> {
  const rows = await env.DB.prepare(
    "SELECT machine_id, commit_ordinal, COUNT(DISTINCT experiment_id) AS experiments, " +
      "SUM(CASE WHEN inherited_from IS NULL THEN 1 ELSE 0 END) AS measured_runs, " +
      "SUM(CASE WHEN inherited_from IS NULL AND compiler_status = 'available' THEN 1 ELSE 0 END) AS measured_available " +
      `FROM runs WHERE experiment_id IN (${experimentList(suite)}) GROUP BY machine_id, commit_ordinal`,
  )
    .bind(...suite.experiments)
    .all<CoverageRow>();
  return rows.results;
}

async function overview(env: Bindings, suite: Suite): Promise<Record<string, unknown>> {
  const window = await commitWindow(env);
  const machines = await env.DB.prepare("SELECT machine_id, last_seen_at FROM machines ORDER BY machine_id").all<{ machine_id: string; last_seen_at: string | null }>();
  const rows = await coverageRows(env, suite);
  const size = suite.experiments.length;
  // A commit counts as covered once every benchmark has a run for it. The
  // headline commit is the newest one measured for the whole suite, or, while
  // a new benchmark is still filling in, the newest measured for any of it.
  const summaries = new Map<string, { measured: number; inherited: number; complete: number | null; partial: number | null }>();
  for (const machine of machines.results) summaries.set(machine.machine_id, { measured: 0, inherited: 0, complete: null, partial: null });
  for (const row of rows) {
    const summary = summaries.get(row.machine_id);
    if (!summary) continue;
    if (row.experiments === size) {
      if (row.measured_runs > 0) summary.measured += 1;
      else summary.inherited += 1;
    }
    if (row.measured_available === size) summary.complete = Math.max(summary.complete ?? -1, row.commit_ordinal);
    else if (row.measured_available > 0) summary.partial = Math.max(summary.partial ?? -1, row.commit_ordinal);
  }
  const latestOrdinal = (machine: string): number | null => {
    const summary = summaries.get(machine)!;
    return summary.complete ?? summary.partial;
  };

  const measured = machines.results.filter((machine) => latestOrdinal(machine.machine_id) !== null);
  const commitStatement = env.DB.prepare("SELECT sha, ordinal, committed_at, subject FROM commits WHERE ordinal = ?");
  const ratioStatement = env.DB.prepare(
    "SELECT benchmark, configuration, compiler_family, backend, optimization, baseline, metric, estimate FROM measurements " +
      `WHERE machine_id = ? AND commit_ordinal = ? AND experiment_id IN (${experimentList(suite)}) AND metric IN ('wall_time', 'compile_time', 'artifact_size') AND estimate IS NOT NULL`,
  );
  const results = measured.length
    ? await env.DB.batch(
        measured.flatMap((machine) => [
          commitStatement.bind(latestOrdinal(machine.machine_id)),
          ratioStatement.bind(machine.machine_id, latestOrdinal(machine.machine_id), ...suite.experiments),
        ]),
      )
    : [];
  const cards = [];
  for (const machine of machines.results) {
    let ratios: unknown[] = [];
    let latest: unknown = null;
    const index = measured.indexOf(machine);
    if (index >= 0) {
      latest = results[2 * index].results[0] ?? null;
      ratios = ratioTable(results[2 * index + 1].results as RatioRow[]);
    }
    const summary = summaries.get(machine.machine_id)!;
    cards.push({
      machine_id: machine.machine_id,
      last_seen_at: machine.last_seen_at,
      measured: summary.measured,
      inherited: summary.inherited,
      total_commits: window.total,
      latest,
      ratios,
    });
  }
  return { ...suiteFields(suite), first_ordinal: window.first, head_ordinal: window.head, total_commits: window.total, machines: cards };
}

interface RatioRow {
  benchmark: string;
  configuration: string;
  compiler_family: string;
  backend: string;
  optimization: string;
  baseline: number;
  metric: string;
  estimate: number;
}

/**
 * AIHC divided by the baseline GHC value, per benchmark, backend, profile and
 * metric, for the headline metrics: wall time, compile time and artifact size.
 */
function ratioTable(rows: RatioRow[]): unknown[] {
  const baselines = new Map<string, number>();
  for (const row of rows) {
    if (row.compiler_family === "ghc" && row.baseline) baselines.set(`${row.benchmark}|${row.backend}|${row.optimization}|${row.metric}`, row.estimate);
  }
  const table = [];
  for (const row of rows) {
    if (row.compiler_family !== "aihc") continue;
    const baseline = baselines.get(`${row.benchmark}|${row.backend}|${row.optimization}|${row.metric}`) ?? null;
    table.push({
      benchmark: row.benchmark,
      configuration: row.configuration,
      backend: row.backend,
      optimization: row.optimization,
      metric: row.metric,
      value: row.estimate,
      baseline_value: baseline,
      ratio: baseline ? row.estimate / baseline : null,
    });
  }
  return table;
}

async function series(env: Bindings, url: URL): Promise<unknown> {
  const suite = await activeSuite(env, url);
  const machine = requireParam(url, "machine");
  const benchmark = requireParam(url, "benchmark");
  const metric = url.searchParams.get("metric") ?? "wall_time";
  const profile = url.searchParams.get("profile");
  const rows = await env.DB.prepare(
    "SELECT m.configuration, m.compiler_family, m.compiler_version, m.backend, m.optimization, m.baseline, m.unit, m.commit_ordinal, m.estimate, m.status, " +
      "r.inherited_from IS NOT NULL AS inherited FROM measurements m JOIN runs r ON r.run_id = m.run_id " +
      `WHERE m.machine_id = ? AND m.benchmark = ? AND m.metric = ? AND (? IS NULL OR m.optimization = ?) AND m.experiment_id IN (${experimentList(suite)}) ` +
      "ORDER BY m.configuration, m.commit_ordinal",
  )
    .bind(machine, benchmark, metric, profile, profile, ...suite.experiments)
    .all<{
      configuration: string;
      compiler_family: string;
      compiler_version: string;
      backend: string;
      optimization: string;
      baseline: number;
      unit: string;
      commit_ordinal: number;
      estimate: number | null;
      status: string;
      inherited: number;
    }>();
  const grouped = new Map<string, { configuration: string; compiler_family: string; compiler_version: string; backend: string; optimization: string; baseline: boolean; unit: string; points: unknown[] }>();
  for (const row of rows.results) {
    let entry = grouped.get(row.configuration);
    if (!entry) {
      entry = {
        configuration: row.configuration,
        compiler_family: row.compiler_family,
        compiler_version: row.compiler_version,
        backend: row.backend,
        optimization: row.optimization,
        baseline: row.baseline === 1,
        unit: row.unit,
        points: [],
      };
      grouped.set(row.configuration, entry);
    }
    entry.points.push([row.commit_ordinal, row.estimate, row.status, row.inherited === 1 ? 1 : 0]);
  }
  const environments = await env.DB.prepare(
    `SELECT MIN(commit_ordinal) AS ordinal, environment_id FROM runs WHERE machine_id = ? AND experiment_id IN (${experimentList(suite)}) GROUP BY environment_id ORDER BY ordinal`,
  )
    .bind(machine, ...suite.experiments)
    .all();
  return { ...suiteFields(suite), machine, benchmark, metric, profile, series: [...grouped.values()], environments: environments.results };
}

async function commitDetail(env: Bindings, sha: string, url: URL): Promise<unknown> {
  const suite = await activeSuite(env, url);
  const commit = await env.DB.prepare("SELECT sha, ordinal, committed_at, subject, tree_key FROM commits WHERE sha = ? OR sha LIKE ?")
    .bind(sha, `${sha}%`)
    .first<{ sha: string; ordinal: number }>();
  if (!commit) throw new HttpError(404, "unknown commit");
  const runs = await env.DB.prepare(
    "SELECT run_id, machine_id, environment_id, experiment_id, compiler_status, unavailable_reason, inherited_from, created_at, uploaded_at, envelope_key " +
      `FROM runs WHERE commit_sha = ? AND experiment_id IN (${experimentList(suite)}) ORDER BY machine_id, experiment_id`,
  )
    .bind(commit.sha, ...suite.experiments)
    .all();
  const rows = await env.DB.prepare(
    "SELECT machine_id, benchmark, configuration, compiler_family, backend, optimization, metric, unit, status, estimate, commit_ordinal " +
      `FROM measurements WHERE commit_ordinal IN (?, ?) AND experiment_id IN (${experimentList(suite)}) ORDER BY machine_id, benchmark, configuration, metric`,
  )
    .bind(commit.ordinal, commit.ordinal - 1, ...suite.experiments)
    .all<{ machine_id: string; benchmark: string; configuration: string; compiler_family: string; backend: string; optimization: string; metric: string; unit: string; status: string; estimate: number | null; commit_ordinal: number }>();
  const parents = new Map<string, number | null>();
  for (const row of rows.results) {
    if (row.commit_ordinal === commit.ordinal - 1) parents.set(`${row.machine_id}|${row.benchmark}|${row.configuration}|${row.metric}`, row.estimate);
  }
  const measurements = rows.results
    .filter((row) => row.commit_ordinal === commit.ordinal)
    .map((row) => {
      const parent = parents.get(`${row.machine_id}|${row.benchmark}|${row.configuration}|${row.metric}`) ?? null;
      const { commit_ordinal: _ordinal, ...rest } = row;
      return { ...rest, parent_estimate: parent, ratio_to_parent: parent && row.estimate ? row.estimate / parent : null };
    });
  return { ...suiteFields(suite), commit, runs: runs.results, measurements };
}

/**
 * One character per commit from the first commit at or after the cutoff to
 * head: M measured, I inherited, U unavailable, P partially covered (some
 * benchmarks still lack a run), . unmeasured. Cell `i` describes ordinal
 * `first + i`.
 */
async function coverage(env: Bindings, url: URL): Promise<unknown> {
  const suite = await activeSuite(env, url);
  const machine = requireParam(url, "machine");
  const window = await commitWindow(env);
  const first = window.first ?? 0;
  const total = window.head === null ? 0 : window.head - first + 1;
  const rows = await env.DB.prepare(
    "SELECT commit_ordinal, COUNT(DISTINCT experiment_id) AS experiments, " +
      "SUM(CASE WHEN compiler_status = 'available' THEN 1 ELSE 0 END) AS available, " +
      "SUM(CASE WHEN inherited_from IS NOT NULL THEN 1 ELSE 0 END) AS inherited " +
      `FROM runs WHERE machine_id = ? AND commit_ordinal >= ? AND experiment_id IN (${experimentList(suite)}) GROUP BY commit_ordinal`,
  )
    .bind(machine, first, ...suite.experiments)
    .all<{ commit_ordinal: number; experiments: number; available: number; inherited: number }>();
  const cells = new Array<string>(total).fill(".");
  for (const row of rows.results) {
    const index = row.commit_ordinal - first;
    if (index < 0 || index >= total) continue;
    if (row.experiments < suite.experiments.length) cells[index] = "P";
    else if (row.available === 0) cells[index] = "U";
    else if (row.inherited === row.experiments) cells[index] = "I";
    else cells[index] = "M";
  }
  return { ...suiteFields(suite), machine, first, total, statuses: cells.join("") };
}
