const COLORS = ["#c7f36b", "#ff8d5c", "#71c8e8", "#b9a1ff", "#f4f0df", "#e8ca71", "#63d7b0"];
const state = { catalog: null, payload: null, revisions: null, points: [], plotted: [] };
const ids = ["platform", "benchmark", "metric", "compiler", "version", "variant", "backend", "gc"];
const elements = Object.fromEntries(ids.map(id => [id, document.getElementById(`${id}-filter`)]));

document.addEventListener("DOMContentLoaded", start);

async function start() {
  ids.forEach(id => elements[id].addEventListener("change", () => filterChanged(id)));
  document.getElementById("reset-filters").addEventListener("click", resetFilters);
  window.addEventListener("resize", () => drawChart(state.points));
  document.getElementById("timeline").addEventListener("mousemove", showTooltip);
  document.getElementById("timeline").addEventListener("mouseleave", () => document.getElementById("tooltip").hidden = true);
  try {
    const query = new URLSearchParams(location.search);
    const catalogUrl = query.get("catalog") || "data/catalog.json";
    state.catalog = await fetchJson(catalogUrl);
    document.getElementById("generated-at").textContent = `Catalog generated ${formatDate(state.catalog.generated_at)}`;
    populatePrimaryFilters(query);
    await loadSelectedSeries(query);
    document.getElementById("status-line").textContent = "Published measurements loaded · lower is better";
  } catch (error) {
    document.getElementById("status-line").textContent = `Catalog unavailable: ${error.message}`;
    document.getElementById("empty-chart").hidden = false;
  }
}

function populatePrimaryFilters(query) {
  const series = state.catalog.series || [];
  setOptions(elements.platform, unique(series.map(item => item.platform)), query.get("platform"));
  refreshDimensions(query);
}

function refreshDimensions(query = new URLSearchParams(location.search)) {
  const series = (state.catalog.series || []).filter(item => item.platform === elements.platform.value && (!state.catalog.active_experiment || item.experiment_id === state.catalog.active_experiment));
  setOptions(elements.benchmark, unique(series.map(item => item.benchmark)), query.get("benchmark"));
  const scoped = series.filter(item => item.benchmark === elements.benchmark.value);
  setOptions(elements.metric, unique(scoped.map(item => item.metric)), query.get("metric") || "wall_time");
}

async function filterChanged(id) {
  if (id === "platform" || id === "benchmark") refreshDimensions();
  if (["platform", "benchmark", "metric"].includes(id)) await loadSelectedSeries();
  else applyPointFilters();
}

async function loadSelectedSeries(query = new URLSearchParams(location.search)) {
  const entry = (state.catalog.series || []).find(item =>
    item.platform === elements.platform.value && item.benchmark === elements.benchmark.value && item.metric === elements.metric.value &&
    (!state.catalog.active_experiment || item.experiment_id === state.catalog.active_experiment)
  );
  state.payload = entry ? await fetchJson(entry.url) : { points: [] };
  const revisionEntry = (state.catalog.revision_indexes || []).find(item =>
    item.platform === elements.platform.value && (!entry || item.experiment_id === entry.experiment_id)
  );
  state.revisions = revisionEntry ? await fetchJson(revisionEntry.url) : { revisions: [] };
  populatePointFilters(query);
  applyPointFilters();
  updateSummary();
}

function populatePointFilters(query) {
  const points = state.payload.points || [];
  const latest = state.revisions?.revisions?.at(-1);
  const outcomes = (latest?.outcomes || []).filter(outcome => outcome.benchmark === elements.benchmark.value);
  const dimensions = [...points, ...outcomes];
  setOptions(elements.compiler, unique(dimensions.map(point => point.compiler_family)), query.get("compiler"), "All compilers");
  setOptions(elements.version, unique(dimensions.map(point => point.compiler_version).filter(version => version.length < 20)), query.get("version"), "All versions");
  setOptions(elements.variant, unique(dimensions.map(variantOf)), query.get("variant"), "All variants");
  setOptions(elements.backend, unique(dimensions.map(point => point.backend)), query.get("backend"), "All backends");
  setOptions(elements.gc, unique(dimensions.map(point => point.gc)), query.get("gc"), "All collectors");
}

function applyPointFilters() {
  const points = state.payload?.points || [];
  state.points = points.filter(point =>
    matches(elements.compiler.value, point.compiler_family) &&
    matches(elements.version.value, point.compiler_version) &&
    matches(elements.variant.value, variantOf(point)) &&
    matches(elements.backend.value, point.backend) &&
    matches(elements.gc.value, point.gc)
  );
  document.getElementById("chart-heading").textContent = `${label(elements.benchmark.value)} · ${label(elements.metric.value)}`;
  document.getElementById("value-heading").textContent = label(elements.metric.value);
  updateUrl();
  drawChart(state.points);
  updateTable(state.points);
}

function updateSummary() {
  const revisions = state.revisions?.revisions || [];
  const latest = revisions.at(-1);
  document.getElementById("coverage").textContent = revisions.length.toLocaleString();
  document.getElementById("latest-revision").textContent = latest ? latest.commit.sha.slice(0, 10) : "—";
  document.getElementById("latest-date").textContent = latest ? `${latest.compiler_status} · ${formatDate(latest.commit.committed_at)}` : "No terminal revisions";
  document.getElementById("environment").textContent = elements.platform.value || "—";
  document.getElementById("environment-detail").textContent = latest?.environment?.hardware_model || latest?.environment?.id || "No environment data";
}

function drawChart(points) {
  const canvas = document.getElementById("timeline");
  const wrap = canvas.parentElement;
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(320, wrap.clientWidth - 24), height = Math.max(260, wrap.clientHeight - 24);
  canvas.width = width * ratio; canvas.height = height * ratio;
  const ctx = canvas.getContext("2d"); ctx.scale(ratio, ratio); ctx.clearRect(0, 0, width, height);
  const empty = document.getElementById("empty-chart");
  if (!points.length) { empty.hidden = false; state.plotted = []; document.getElementById("legend").innerHTML = ""; return; }
  empty.hidden = true;
  const margin = { left: 74, right: 24, top: 25, bottom: 45 };
  const innerW = width - margin.left - margin.right, innerH = height - margin.top - margin.bottom;
  const xValues = points.map(p => p.commit.ordinal), yValues = points.map(p => p.estimate);
  const xMin = Math.min(...xValues), xMax = Math.max(...xValues);
  let yMin = Math.min(...yValues), yMax = Math.max(...yValues); const padding = (yMax - yMin || yMax * .1 || 1) * .12; yMin = Math.max(0, yMin - padding); yMax += padding;
  const x = value => margin.left + ((value - xMin) / (xMax - xMin || 1)) * innerW;
  const y = value => margin.top + innerH - ((value - yMin) / (yMax - yMin || 1)) * innerH;
  ctx.font = "11px SFMono-Regular, monospace"; ctx.strokeStyle = "#344136"; ctx.fillStyle = "#a9b0a6"; ctx.lineWidth = 1;
  for (let i = 0; i <= 5; i++) { const value = yMin + ((yMax - yMin) * i / 5); const py = y(value); ctx.beginPath(); ctx.moveTo(margin.left, py); ctx.lineTo(width - margin.right, py); ctx.stroke(); ctx.fillText(formatValue(value, state.payload.metric), 8, py + 4); }
  ctx.fillText(`#${xMin + 1}`, margin.left, height - 15); ctx.fillText(`#${xMax + 1}`, width - margin.right - 48, height - 15);
  const groups = groupBy(points, point => `${point.compiler_family} ${shortVersion(point.compiler_version)} · ${variantOf(point)} · ${point.backend} · ${point.gc}`);
  const legend = document.getElementById("legend"); legend.innerHTML = ""; state.plotted = [];
  [...groups.entries()].forEach(([name, values], index) => {
    const color = COLORS[index % COLORS.length]; values.sort((a,b) => a.commit.ordinal - b.commit.ordinal);
    ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath();
    let previousEnvironment = null;
    values.forEach((point, i) => { const px = x(point.commit.ordinal), py = y(point.estimate); if (!i || point.environment_id !== previousEnvironment) ctx.moveTo(px, py); else ctx.lineTo(px, py); previousEnvironment = point.environment_id; state.plotted.push({ x: px, y: py, point, color, name }); }); ctx.stroke();
    const item = document.createElement("span"); item.innerHTML = `<i style="background:${color}"></i>${escapeHtml(name)}`; legend.appendChild(item);
  });
}

function showTooltip(event) {
  if (!state.plotted.length) return;
  const canvas = event.currentTarget, rect = canvas.getBoundingClientRect(); const px = event.clientX - rect.left, py = event.clientY - rect.top;
  let nearest = null, distance = Infinity;
  state.plotted.forEach(item => { const d = Math.hypot(item.x - px, item.y - py); if (d < distance) { distance = d; nearest = item; } });
  const tooltip = document.getElementById("tooltip"); if (!nearest || distance > 28) { tooltip.hidden = true; return; }
  tooltip.hidden = false; tooltip.style.left = `${Math.min(px + 18, rect.width - 220)}px`; tooltip.style.top = `${Math.max(10, py - 65)}px`;
  tooltip.innerHTML = `<strong style="color:${nearest.color}">${escapeHtml(nearest.name)}</strong><br>${nearest.point.commit.sha.slice(0,12)} · #${nearest.point.commit.ordinal + 1}<br>${formatValue(nearest.point.estimate, state.payload.metric)}`;
}

function updateTable(points) {
  const body = document.getElementById("detail-body");
  const latestByConfig = new Map(); points.forEach(point => { const key = point.configuration; if (!latestByConfig.has(key) || latestByConfig.get(key).commit.ordinal < point.commit.ordinal) latestByConfig.set(key, point); });
  const latestRevision = state.revisions?.revisions?.at(-1);
  const outcomes = (latestRevision?.outcomes || []).filter(outcome =>
    outcome.benchmark === elements.benchmark.value &&
    matches(elements.compiler.value, outcome.compiler_family) && matches(elements.version.value, outcome.compiler_version) &&
    matches(elements.variant.value, variantOf(outcome)) &&
    matches(elements.backend.value, outcome.backend) && matches(elements.gc.value, outcome.gc)
  );
  const rows = new Map(outcomes.map(outcome => [outcome.configuration, { outcome, point: latestByConfig.get(outcome.configuration) }]));
  latestByConfig.forEach((point, key) => { if (!rows.has(key)) rows.set(key, { point, outcome: null }); });
  if (!rows.size) { body.innerHTML = '<tr><td colspan="7">No measurements match these filters.</td></tr>'; return; }
  body.innerHTML = [...rows.entries()].sort((a,b) => a[0].localeCompare(b[0])).map(([, row]) => {
    const point = row.point, outcome = row.outcome; const source = point || outcome; const commit = point?.commit || latestRevision?.commit;
    const status = point?.status || outcome?.measurement_status || outcome?.compile_status || "unavailable";
    return `<tr><td>${escapeHtml(source.compiler_family)} ${escapeHtml(shortVersion(source.compiler_version))}</td><td>${escapeHtml(variantOf(source))}</td><td>${escapeHtml(source.backend)}</td><td>${escapeHtml(source.gc)}</td>
      <td>${commit ? `<a href="https://github.com/ai-haskell-compiler/aihc/commit/${commit.sha}">${commit.sha.slice(0,10)}</a>` : "—"}</td>
      <td>${point ? formatValue(point.estimate, state.payload.metric) : "—"}</td><td class="${status === 'converged' ? 'status-ok' : ''}">${escapeHtml(status)}</td></tr>`;
  }).join("");
}

function resetFilters() { ["compiler","version","variant","backend","gc"].forEach(id => elements[id].value = "all"); applyPointFilters(); }
function setOptions(select, values, preferred, allLabel) { const current = preferred || select.value; select.innerHTML = allLabel ? `<option value="all">${allLabel}</option>` : ""; values.forEach(value => select.add(new Option(label(value), value))); select.value = [...select.options].some(o => o.value === current) ? current : select.options[0]?.value || ""; }
function updateUrl() { const query = new URLSearchParams(); ids.forEach(id => { if (elements[id].value && elements[id].value !== "all") query.set(id, elements[id].value); }); history.replaceState(null, "", `${location.pathname}?${query}`); }
function matches(filter, value) { return filter === "all" || filter === value; }
function variantOf(point) { return point.compiler_variant || (point.compiler_family === "ghc" ? "gmp" : "default"); }
function unique(values) { return [...new Set(values)].sort(); }
function label(value) { return String(value || "").replaceAll("_", " ").replace(/\b\w/g, c => c.toUpperCase()); }
function shortVersion(value) { return value.length > 18 ? value.slice(0, 8) : value; }
function groupBy(values, key) { const map = new Map(); values.forEach(value => { const id = key(value); map.set(id, [...(map.get(id) || []), value]); }); return map; }
function formatValue(value, metric) { if (metric === "wall_time") return value >= 1e9 ? `${(value/1e9).toFixed(3)} s` : `${(value/1e6).toFixed(2)} ms`; if (metric === "peak_rss") return `${(value/1048576).toFixed(1)} MiB`; return Number(value).toLocaleString(); }
function formatDate(value) { return value ? new Date(value).toLocaleDateString(undefined, { year:"numeric", month:"short", day:"numeric" }) : "unknown"; }
function escapeHtml(value) { return String(value).replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char])); }
async function fetchJson(url) { const response = await fetch(url, { cache: "no-cache" }); if (!response.ok) throw new Error(`${response.status} ${response.statusText}`); return response.json(); }
