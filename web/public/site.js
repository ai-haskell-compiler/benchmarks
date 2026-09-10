// Shared helpers for fast.aihc.app. Plain ES module, no build step.

export const GITHUB = "https://github.com/ai-haskell-compiler/aihc";

const TIME_METRICS = new Set(["wall_time", "cpu_time", "gc_time", "compile_time"]);
const BYTE_METRICS = new Set(["peak_rss", "peak_heap", "allocated_bytes", "artifact_size"]);

export const METRIC_LABELS = {
  wall_time: "Wall time",
  cpu_time: "CPU time",
  peak_rss: "Peak RSS",
  peak_heap: "Peak heap",
  allocated_bytes: "Bytes allocated",
  gc_count: "GC count",
  gc_time: "GC time",
  compile_time: "Compile time",
  artifact_size: "Artifact size",
};

/** Series identity: compiler family and backend decide the hue; the profile decides the dash. */
export const SERIES_SLOTS = [
  { key: "aihc-native", label: "AIHC native", css: "--series-aihc-native" },
  { key: "aihc-llvm", label: "AIHC LLVM", css: "--series-aihc-llvm" },
  { key: "aihc-wasm", label: "AIHC Wasm", css: "--series-aihc-wasm" },
  { key: "ghc-native", label: "GHC native", css: "--series-ghc-native" },
  { key: "ghc-llvm", label: "GHC LLVM", css: "--series-ghc-llvm" },
];

export function seriesKey(entry) {
  return `${entry.compiler_family}-${entry.backend}`;
}

export function seriesColor(entry) {
  const slot = SERIES_SLOTS.find((item) => item.key === seriesKey(entry));
  const name = slot ? slot.css : "--text-muted";
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
}

export async function api(path, params) {
  const url = new URL(path, location.origin);
  if (params) for (const [key, value] of Object.entries(params)) if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, value);
  const response = await fetch(url);
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body.error) message = body.error;
    } catch {}
    throw new Error(message);
  }
  return response.json();
}

let commitIndexPromise = null;
/** Ordinal -> commit, fetched once per page. */
export function commitIndex() {
  if (!commitIndexPromise) {
    commitIndexPromise = api("/api/commits").then((data) => {
      const byOrdinal = new Map();
      const bySha = new Map();
      for (const commit of data.commits) {
        byOrdinal.set(commit.ordinal, commit);
        bySha.set(commit.sha, commit);
      }
      return { byOrdinal, bySha, list: data.commits };
    });
  }
  return commitIndexPromise;
}

export function formatValue(value, metric) {
  if (value === null || value === undefined) return "—";
  if (TIME_METRICS.has(metric)) return formatDuration(value);
  if (BYTE_METRICS.has(metric)) return formatBytes(value);
  return Number(value).toLocaleString();
}

export function formatDuration(ns) {
  if (ns >= 1e9) return `${(ns / 1e9).toFixed(ns >= 1e10 ? 1 : 2)} s`;
  if (ns >= 1e6) return `${(ns / 1e6).toFixed(ns >= 1e8 ? 0 : 1)} ms`;
  if (ns >= 1e3) return `${(ns / 1e3).toFixed(1)} µs`;
  return `${ns} ns`;
}

export function formatBytes(bytes) {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} GiB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${bytes} B`;
}

export function formatRatio(ratio) {
  if (ratio === null || ratio === undefined || !Number.isFinite(ratio)) return "—";
  return `${ratio.toFixed(ratio >= 10 ? 1 : 2)}×`;
}

export function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return date.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function shortSha(sha) {
  return (sha || "").slice(0, 12);
}

export function commitLink(commit, text) {
  const link = el("a", { href: `/commit.html?sha=${commit.sha}` }, text ?? shortSha(commit.sha));
  link.classList.add("mono");
  return link;
}

/** Tiny DOM builder: el("td", {class: "num"}, "text", childNode, ...) */
export function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === undefined || child === null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function replaceChildren(node, ...children) {
  node.replaceChildren(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
}

/** Read and write filter state in the query string so every view is linkable. */
export function queryState(defaults) {
  const params = new URLSearchParams(location.search);
  const state = { ...defaults };
  for (const key of Object.keys(defaults)) if (params.has(key)) state[key] = params.get(key);
  return state;
}

export function pushQuery(state, defaults = {}) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(state)) {
    if (value === undefined || value === null || value === "" || value === false) continue;
    if (defaults[key] !== undefined && String(defaults[key]) === String(value)) continue;
    params.set(key, value === true ? "1" : value);
  }
  const query = params.toString();
  history.replaceState(null, "", query ? `${location.pathname}?${query}` : location.pathname);
}

export function fillSelect(select, values, current, labels = {}) {
  select.replaceChildren(...values.map((value) => el("option", { value }, labels[value] ?? value)));
  select.value = values.includes(current) ? current : values[0] ?? "";
  return select.value;
}

export function setStatus(text, isError = false) {
  const node = document.getElementById("status-line");
  if (!node) return;
  node.textContent = text;
  node.classList.toggle("worse", isError);
}

export function markCurrentNav() {
  const here = location.pathname === "/" ? "/index.html" : location.pathname;
  for (const link of document.querySelectorAll(".site-header nav a")) {
    if (link.getAttribute("href") === here || (here === "/index.html" && link.getAttribute("href") === "/")) link.setAttribute("aria-current", "page");
  }
}

export function describeConfiguration(entry) {
  const parts = [entry.compiler_family === "ghc" ? `GHC ${entry.compiler_version ?? ""}`.trim() : "AIHC", entry.backend];
  if (entry.configuration && entry.configuration.includes("native-bignum")) parts.push("native-bignum");
  if (entry.optimization) parts.push(entry.optimization);
  return parts.join(" · ");
}

export function machineLabel(machine) {
  return machine.display_name || machine.machine_id;
}

/** Ratio of b to a as a signed percentage string with a class for coloring; lower is better. */
export function changeCell(ratio) {
  if (ratio === null || ratio === undefined || !Number.isFinite(ratio)) return el("td", { class: "num muted" }, "—");
  const percent = (ratio - 1) * 100;
  const text = `${percent >= 0 ? "+" : ""}${percent.toFixed(1)}%`;
  const cls = Math.abs(percent) < 2 ? "num muted" : percent < 0 ? "num better" : "num worse";
  return el("td", { class: cls }, text);
}
