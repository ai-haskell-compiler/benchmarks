import type { Bindings } from "./bindings";

const SCHEMA_VERSION = 2;
const CACHE_SECONDS = 60;
const STATEMENT_BATCH = 40;

type Json = Record<string, unknown>;

interface Machine {
  machine_id: string;
  display_name: string | null;
}

interface Envelope {
  schema_version: number;
  run_id: string;
  created_at: string;
  experiment_id: string;
  platform: string;
  machine_id: string;
  environment: { id: string } & Json;
  aihc_commit: { sha: string; ordinal: number; committed_at: string; subject: string; tree_key?: string };
  compiler_status: string;
  unavailable_reason: string | null;
  inherited_from?: string;
  results: Array<{
    benchmark: string;
    configuration: string;
    compiler_family: string;
    compiler_version: string;
    backend: string;
    optimization: string;
    baseline?: boolean;
    measurement: { status: string; metrics?: Array<{ metric: string; unit: string; status?: string; estimate: number | null; samples?: unknown[] }> };
  }>;
}

class HttpError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function handleApi(request: Request, env: Bindings, url: URL, ctx: ExecutionContext): Promise<Response> {
  try {
    const segments = url.pathname.split("/").filter(Boolean).slice(1);
    const head = segments[0] ?? "";
    if (request.method === "POST") {
      if (head === "machines" && segments.length === 1) return await registerMachine(request, env);
      if (head === "commits" && segments.length === 1) return await uploadCommits(request, env);
      if (head === "upload" && segments.length === 1) return await uploadRun(request, env);
      throw new HttpError(404, "unknown endpoint");
    }
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
      case "compare":
        return cached(await compare(env, url));
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

async function sha256(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function randomToken(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function bearer(request: Request): string | null {
  const header = request.headers.get("authorization") ?? "";
  const match = /^Bearer\s+(\S+)$/i.exec(header);
  return match ? match[1] : null;
}

async function readJson<T>(request: Request): Promise<T> {
  let body: ReadableStream<Uint8Array> | null = request.body;
  if (!body) throw new HttpError(400, "missing body");
  if ((request.headers.get("content-encoding") ?? "").toLowerCase() === "gzip") {
    body = body.pipeThrough(new DecompressionStream("gzip"));
  }
  try {
    return (await new Response(body).json()) as T;
  } catch {
    throw new HttpError(400, "body is not valid JSON");
  }
}

async function gzip(text: string): Promise<ArrayBuffer> {
  const stream = new Blob([text]).stream().pipeThrough(new CompressionStream("gzip"));
  return new Response(stream).arrayBuffer();
}

async function authenticateMachine(request: Request, env: Bindings): Promise<Machine> {
  const token = bearer(request);
  if (!token) throw new HttpError(401, "missing bearer token");
  const row = await env.DB.prepare("SELECT machine_id, display_name FROM machines WHERE token_hash = ?")
    .bind(await sha256(token))
    .first<Machine>();
  if (!row) throw new HttpError(403, "unknown token");
  return row;
}

function requireAdmin(request: Request, env: Bindings): void {
  const token = bearer(request);
  if (!env.ADMIN_TOKEN || !token || token !== env.ADMIN_TOKEN) throw new HttpError(403, "admin token required");
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

async function runBatches(env: Bindings, statements: D1PreparedStatement[]): Promise<void> {
  for (let index = 0; index < statements.length; index += STATEMENT_BATCH) {
    await env.DB.batch(statements.slice(index, index + STATEMENT_BATCH));
  }
}

// ---------------------------------------------------------------------------
// Writes

async function registerMachine(request: Request, env: Bindings): Promise<Response> {
  requireAdmin(request, env);
  const body = await readJson<{ machine_id?: string; display_name?: string }>(request);
  const machineId = body.machine_id ?? "";
  if (!/^[a-z0-9][a-z0-9-]{1,62}$/.test(machineId)) throw new HttpError(400, "invalid machine_id");
  const token = randomToken();
  await env.DB.prepare(
    "INSERT INTO machines(machine_id, token_hash, display_name, created_at) VALUES (?, ?, ?, ?) " +
      "ON CONFLICT(machine_id) DO UPDATE SET token_hash = excluded.token_hash, display_name = COALESCE(excluded.display_name, machines.display_name)",
  )
    .bind(machineId, await sha256(token), body.display_name ?? null, new Date().toISOString())
    .run();
  return json({ machine_id: machineId, token }, 201);
}

async function uploadCommits(request: Request, env: Bindings): Promise<Response> {
  await authenticateMachine(request, env);
  const body = await readJson<{ commits?: Envelope["aihc_commit"][] }>(request);
  const commits = Array.isArray(body.commits) ? body.commits : [];
  const statement = env.DB.prepare(
    "INSERT INTO commits(sha, ordinal, committed_at, subject, tree_key) VALUES (?, ?, ?, ?, ?) " +
      "ON CONFLICT(sha) DO UPDATE SET ordinal = excluded.ordinal, committed_at = excluded.committed_at, " +
      "subject = excluded.subject, tree_key = COALESCE(excluded.tree_key, commits.tree_key)",
  );
  const statements = commits.map((commit) => {
    if (typeof commit.sha !== "string" || typeof commit.ordinal !== "number") throw new HttpError(400, "invalid commit");
    return statement.bind(commit.sha, commit.ordinal, commit.committed_at ?? "", commit.subject ?? "", commit.tree_key ?? null);
  });
  await runBatches(env, statements);
  return json({ upserted: statements.length });
}

async function uploadRun(request: Request, env: Bindings): Promise<Response> {
  const machine = await authenticateMachine(request, env);
  const envelope = await readJson<Envelope>(request);
  if (envelope.schema_version !== SCHEMA_VERSION) throw new HttpError(400, `expected schema_version ${SCHEMA_VERSION}`);
  if (envelope.machine_id !== machine.machine_id) throw new HttpError(403, "envelope belongs to another machine");
  const commit = envelope.aihc_commit;
  if (!commit?.sha || typeof commit.ordinal !== "number") throw new HttpError(400, "envelope lacks a commit");
  const inheritedFrom = envelope.inherited_from ?? null;
  const runId = inheritedFrom ? `${envelope.run_id}~${commit.sha.slice(0, 12)}` : envelope.run_id;

  const existing = await env.DB.prepare("SELECT run_id FROM runs WHERE run_id = ?").bind(runId).first();
  if (existing) return json({ run_id: runId, inserted: false });

  const now = new Date().toISOString();
  let envelopeKey = `raw/v2/${machine.machine_id}/${commit.sha}/${envelope.run_id}.json.gz`;
  if (inheritedFrom) {
    const source = await env.DB.prepare("SELECT envelope_key FROM runs WHERE run_id = ?").bind(envelope.run_id).first<{ envelope_key: string }>();
    if (source) envelopeKey = source.envelope_key;
    else envelopeKey = `raw/v2/${machine.machine_id}/${commit.sha}/${runId}.json.gz`;
  }
  if (!inheritedFrom || !(await env.RAW.head(envelopeKey))) {
    await env.RAW.put(envelopeKey, await gzip(JSON.stringify(envelope)), {
      httpMetadata: { contentType: "application/json", contentEncoding: "gzip", cacheControl: "public, max-age=31536000, immutable" },
    });
  }

  const statements: D1PreparedStatement[] = [
    env.DB.prepare(
      "INSERT INTO commits(sha, ordinal, committed_at, subject, tree_key) VALUES (?, ?, ?, ?, ?) " +
        "ON CONFLICT(sha) DO UPDATE SET ordinal = excluded.ordinal, committed_at = excluded.committed_at, subject = excluded.subject, " +
        "tree_key = COALESCE(excluded.tree_key, commits.tree_key)",
    ).bind(commit.sha, commit.ordinal, commit.committed_at ?? "", commit.subject ?? "", commit.tree_key ?? null),
    env.DB.prepare(
      "INSERT INTO environments(environment_id, machine_id, first_seen_at, record) VALUES (?, ?, ?, ?) ON CONFLICT(environment_id) DO NOTHING",
    ).bind(envelope.environment.id, machine.machine_id, now, JSON.stringify(envelope.environment)),
    env.DB.prepare(
      "INSERT INTO runs(run_id, machine_id, environment_id, experiment_id, commit_sha, commit_ordinal, compiler_status, unavailable_reason, " +
        "inherited_from, created_at, uploaded_at, envelope_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    ).bind(
      runId,
      machine.machine_id,
      envelope.environment.id,
      envelope.experiment_id,
      commit.sha,
      commit.ordinal,
      envelope.compiler_status,
      envelope.unavailable_reason ?? null,
      inheritedFrom,
      envelope.created_at,
      now,
      envelopeKey,
    ),
    env.DB.prepare("UPDATE machines SET last_seen_at = ? WHERE machine_id = ?").bind(now, machine.machine_id),
  ];
  const measurement = env.DB.prepare(
    "INSERT OR REPLACE INTO measurements(run_id, machine_id, experiment_id, commit_ordinal, benchmark, configuration, compiler_family, " +
      "compiler_version, backend, optimization, baseline, metric, unit, status, estimate, sample_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
  );
  let count = 0;
  for (const result of envelope.results ?? []) {
    for (const metric of result.measurement?.metrics ?? []) {
      const status = metric.estimate === null || metric.estimate === undefined ? "unavailable" : (metric.status ?? "ok");
      statements.push(
        measurement.bind(
          runId,
          machine.machine_id,
          envelope.experiment_id,
          commit.ordinal,
          result.benchmark,
          result.configuration,
          result.compiler_family,
          result.compiler_version ?? "",
          result.backend,
          result.optimization ?? "O2",
          result.baseline ? 1 : 0,
          metric.metric,
          metric.unit,
          result.measurement.status === "converged" || result.measurement.status === "nonconverged" ? status : result.measurement.status,
          metric.estimate ?? null,
          Array.isArray(metric.samples) ? metric.samples.length : null,
        ),
      );
      count += 1;
    }
  }
  await runBatches(env, statements);
  return json({ run_id: runId, inserted: true, measurements: count, envelope_key: envelopeKey }, 201);
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
  const rows = await env.DB.prepare("SELECT sha, ordinal, committed_at, subject, tree_key FROM commits ORDER BY ordinal DESC LIMIT ?")
    .bind(limit)
    .all();
  return { commits: rows.results };
}

async function listMachines(env: Bindings): Promise<unknown> {
  const rows = await env.DB.prepare(
    "SELECT m.machine_id, m.display_name, m.created_at, m.last_seen_at, " +
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
  const total = await env.DB.prepare("SELECT COUNT(*) AS n, MAX(ordinal) AS head FROM commits").first<{ n: number; head: number | null }>();
  const machines = await env.DB.prepare(
    "SELECT m.machine_id, m.display_name, m.last_seen_at, " +
      "SUM(CASE WHEN r.inherited_from IS NULL THEN 1 ELSE 0 END) AS measured, " +
      "SUM(CASE WHEN r.inherited_from IS NOT NULL THEN 1 ELSE 0 END) AS inherited, " +
      "MAX(CASE WHEN r.inherited_from IS NULL AND r.compiler_status = 'available' THEN r.commit_ordinal END) AS latest_ordinal " +
      "FROM machines m LEFT JOIN runs r ON r.machine_id = m.machine_id AND r.experiment_id = ? GROUP BY m.machine_id ORDER BY m.machine_id",
  )
    .bind(experiment)
    .all<{ machine_id: string; display_name: string | null; last_seen_at: string | null; measured: number; inherited: number; latest_ordinal: number | null }>();

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
      display_name: machine.display_name,
      last_seen_at: machine.last_seen_at,
      measured: machine.measured ?? 0,
      inherited: machine.inherited ?? 0,
      total_commits: total?.n ?? 0,
      latest,
      ratios,
    });
  }
  return { experiment, head_ordinal: total?.head ?? null, machines: cards };
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

async function compare(env: Bindings, url: URL): Promise<unknown> {
  const experiment = await activeExperiment(env, url);
  const machine = requireParam(url, "machine");
  const a = await resolveCommit(env, requireParam(url, "a"));
  const b = await resolveCommit(env, requireParam(url, "b"));
  const rows = await env.DB.prepare(
    "SELECT m.benchmark, m.configuration, m.compiler_family, m.backend, m.optimization, m.metric, m.unit, m.status, m.estimate, m.commit_ordinal, " +
      "r.inherited_from IS NOT NULL AS inherited FROM measurements m JOIN runs r ON r.run_id = m.run_id " +
      "WHERE m.machine_id = ? AND m.experiment_id = ? AND m.commit_ordinal IN (?, ?) ORDER BY m.benchmark, m.configuration, m.metric",
  )
    .bind(machine, experiment, a.ordinal, b.ordinal)
    .all<{ benchmark: string; configuration: string; compiler_family: string; backend: string; optimization: string; metric: string; unit: string; status: string; estimate: number | null; commit_ordinal: number; inherited: number }>();
  const cells = new Map<string, Json>();
  for (const row of rows.results) {
    const key = `${row.benchmark}|${row.configuration}|${row.metric}`;
    const cell = cells.get(key) ?? {
      benchmark: row.benchmark,
      configuration: row.configuration,
      compiler_family: row.compiler_family,
      backend: row.backend,
      optimization: row.optimization,
      metric: row.metric,
      unit: row.unit,
      a: null,
      b: null,
    };
    const side = { estimate: row.estimate, status: row.status, inherited: row.inherited === 1 };
    if (row.commit_ordinal === a.ordinal) cell.a = side;
    if (row.commit_ordinal === b.ordinal) cell.b = side;
    cells.set(key, cell);
  }
  const results = [...cells.values()].map((cell) => {
    const left = cell.a as { estimate: number | null } | null;
    const right = cell.b as { estimate: number | null } | null;
    const ratio = left?.estimate && right?.estimate ? right.estimate / left.estimate : null;
    return { ...cell, ratio };
  });
  return { experiment, machine, a, b, results };
}

async function resolveCommit(env: Bindings, sha: string): Promise<{ sha: string; ordinal: number; subject: string }> {
  const commit = await env.DB.prepare("SELECT sha, ordinal, subject FROM commits WHERE sha = ? OR sha LIKE ? ORDER BY ordinal DESC LIMIT 1")
    .bind(sha, `${sha}%`)
    .first<{ sha: string; ordinal: number; subject: string }>();
  if (!commit) throw new HttpError(404, `unknown commit ${sha}`);
  return commit;
}

/** One character per commit ordinal: M measured, I inherited, U unavailable, . unmeasured. */
async function coverage(env: Bindings, url: URL): Promise<unknown> {
  const experiment = await activeExperiment(env, url);
  const machine = requireParam(url, "machine");
  const head = await env.DB.prepare("SELECT MAX(ordinal) AS head FROM commits").first<{ head: number | null }>();
  const total = head?.head === null || head?.head === undefined ? 0 : head.head + 1;
  const rows = await env.DB.prepare(
    "SELECT commit_ordinal, compiler_status, inherited_from FROM runs WHERE machine_id = ? AND experiment_id = ?",
  )
    .bind(machine, experiment)
    .all<{ commit_ordinal: number; compiler_status: string; inherited_from: string | null }>();
  const cells = new Array<string>(total).fill(".");
  for (const row of rows.results) {
    if (row.commit_ordinal < 0 || row.commit_ordinal >= total) continue;
    cells[row.commit_ordinal] = row.compiler_status !== "available" ? "U" : row.inherited_from ? "I" : "M";
  }
  return { experiment, machine, total, statuses: cells.join("") };
}
