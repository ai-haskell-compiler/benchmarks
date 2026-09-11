import type { Bindings } from "./bindings";

const CACHE_SECONDS = 60;

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
        return cached(await overview(env, url));
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

async function activeExperiment(env: Bindings, url: URL): Promise<string> {
  const requested = url.searchParams.get("experiment");
  if (requested) return requested;
  const row = await env.DB.prepare("SELECT experiment_id FROM runs ORDER BY uploaded_at DESC LIMIT 1").first<{ experiment_id: string }>();
  if (!row) throw new HttpError(404, "no results have been uploaded");
  return row.experiment_id;
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
  const rows = await env.DB.prepare(
    "SELECT experiment_id, machine_id, COUNT(*) AS runs, MAX(uploaded_at) AS last_upload FROM runs GROUP BY experiment_id, machine_id ORDER BY last_upload DESC",
  ).all();
  const active = rows.results[0]?.experiment_id ?? null;
  return { active, experiments: rows.results };
}

async function overview(env: Bindings, url: URL): Promise<unknown> {
  const experiment = await activeExperiment(env, url);
  const window = await commitWindow(env);
  const machines = await env.DB.prepare(
    "SELECT m.machine_id, m.last_seen_at, " +
      "SUM(CASE WHEN r.inherited_from IS NULL THEN 1 ELSE 0 END) AS measured, " +
      "SUM(CASE WHEN r.inherited_from IS NOT NULL THEN 1 ELSE 0 END) AS inherited, " +
      "MAX(CASE WHEN r.inherited_from IS NULL AND r.compiler_status = 'available' THEN r.commit_ordinal END) AS latest_ordinal " +
      "FROM machines m LEFT JOIN runs r ON r.machine_id = m.machine_id AND r.experiment_id = ? GROUP BY m.machine_id ORDER BY m.machine_id",
  )
    .bind(experiment)
    .all<{ machine_id: string; last_seen_at: string | null; measured: number; inherited: number; latest_ordinal: number | null }>();

  const cards = [];
  for (const machine of machines.results) {
    let ratios: unknown[] = [];
    let latest: unknown = null;
    if (machine.latest_ordinal !== null) {
      latest = await env.DB.prepare("SELECT sha, ordinal, committed_at, subject FROM commits WHERE ordinal = ?").bind(machine.latest_ordinal).first();
      const rows = await env.DB.prepare(
        "SELECT benchmark, configuration, compiler_family, backend, optimization, baseline, estimate FROM measurements " +
          "WHERE machine_id = ? AND experiment_id = ? AND commit_ordinal = ? AND metric = 'wall_time' AND estimate IS NOT NULL",
      )
        .bind(machine.machine_id, experiment, machine.latest_ordinal)
        .all<{ benchmark: string; configuration: string; compiler_family: string; backend: string; optimization: string; baseline: number; estimate: number }>();
      ratios = ratioTable(rows.results);
    }
    cards.push({
      machine_id: machine.machine_id,
      last_seen_at: machine.last_seen_at,
      measured: machine.measured ?? 0,
      inherited: machine.inherited ?? 0,
      total_commits: window.total,
      latest,
      ratios,
    });
  }
  return { experiment, first_ordinal: window.first, head_ordinal: window.head, total_commits: window.total, machines: cards };
}

/** AIHC wall time divided by the baseline GHC wall time, per benchmark, backend and profile. */
function ratioTable(
  rows: Array<{ benchmark: string; configuration: string; compiler_family: string; backend: string; optimization: string; baseline: number; estimate: number }>,
): unknown[] {
  const baselines = new Map<string, number>();
  for (const row of rows) {
    if (row.compiler_family === "ghc" && row.baseline) baselines.set(`${row.benchmark}|${row.backend}|${row.optimization}`, row.estimate);
  }
  const table = [];
  for (const row of rows) {
    if (row.compiler_family !== "aihc") continue;
    const baseline = baselines.get(`${row.benchmark}|${row.backend}|${row.optimization}`) ?? null;
    table.push({
      benchmark: row.benchmark,
      configuration: row.configuration,
      backend: row.backend,
      optimization: row.optimization,
      wall_time: row.estimate,
      baseline_wall_time: baseline,
      ratio: baseline ? row.estimate / baseline : null,
    });
  }
  return table;
}

async function series(env: Bindings, url: URL): Promise<unknown> {
  const experiment = await activeExperiment(env, url);
  const machine = requireParam(url, "machine");
  const benchmark = requireParam(url, "benchmark");
  const metric = url.searchParams.get("metric") ?? "wall_time";
  const profile = url.searchParams.get("profile");
  const rows = await env.DB.prepare(
    "SELECT m.configuration, m.compiler_family, m.compiler_version, m.backend, m.optimization, m.baseline, m.unit, m.commit_ordinal, m.estimate, m.status, " +
      "r.inherited_from IS NOT NULL AS inherited FROM measurements m JOIN runs r ON r.run_id = m.run_id " +
      "WHERE m.machine_id = ? AND m.experiment_id = ? AND m.benchmark = ? AND m.metric = ? AND (? IS NULL OR m.optimization = ?) " +
      "ORDER BY m.configuration, m.commit_ordinal",
  )
    .bind(machine, experiment, benchmark, metric, profile, profile)
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
    "SELECT MIN(commit_ordinal) AS ordinal, environment_id FROM runs WHERE machine_id = ? AND experiment_id = ? GROUP BY environment_id ORDER BY ordinal",
  )
    .bind(machine, experiment)
    .all();
  return { experiment, machine, benchmark, metric, profile, series: [...grouped.values()], environments: environments.results };
}

async function commitDetail(env: Bindings, sha: string, url: URL): Promise<unknown> {
  const experiment = await activeExperiment(env, url);
  const commit = await env.DB.prepare("SELECT sha, ordinal, committed_at, subject, tree_key FROM commits WHERE sha = ? OR sha LIKE ?")
    .bind(sha, `${sha}%`)
    .first<{ sha: string; ordinal: number }>();
  if (!commit) throw new HttpError(404, "unknown commit");
  const runs = await env.DB.prepare(
    "SELECT run_id, machine_id, environment_id, compiler_status, unavailable_reason, inherited_from, created_at, uploaded_at, envelope_key " +
      "FROM runs WHERE commit_sha = ? AND experiment_id = ? ORDER BY machine_id",
  )
    .bind(commit.sha, experiment)
    .all();
  const rows = await env.DB.prepare(
    "SELECT machine_id, benchmark, configuration, compiler_family, backend, optimization, metric, unit, status, estimate, commit_ordinal " +
      "FROM measurements WHERE experiment_id = ? AND commit_ordinal IN (?, ?) ORDER BY machine_id, benchmark, configuration, metric",
  )
    .bind(experiment, commit.ordinal, commit.ordinal - 1)
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
  return { experiment, commit, runs: runs.results, measurements };
}

/**
 * One character per commit from the first commit at or after the cutoff to
 * head: M measured, I inherited, U unavailable, . unmeasured. Cell `i`
 * describes ordinal `first + i`.
 */
async function coverage(env: Bindings, url: URL): Promise<unknown> {
  const experiment = await activeExperiment(env, url);
  const machine = requireParam(url, "machine");
  const window = await commitWindow(env);
  const first = window.first ?? 0;
  const total = window.head === null ? 0 : window.head - first + 1;
  const rows = await env.DB.prepare(
    "SELECT commit_ordinal, compiler_status, inherited_from FROM runs WHERE machine_id = ? AND experiment_id = ? AND commit_ordinal >= ?",
  )
    .bind(machine, experiment, first)
    .all<{ commit_ordinal: number; compiler_status: string; inherited_from: string | null }>();
  const cells = new Array<string>(total).fill(".");
  for (const row of rows.results) {
    const index = row.commit_ordinal - first;
    if (index < 0 || index >= total) continue;
    cells[index] = row.compiler_status !== "available" ? "U" : row.inherited_from ? "I" : "M";
  }
  return { experiment, machine, first, total, statuses: cells.join("") };
}
