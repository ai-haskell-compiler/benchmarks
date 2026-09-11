export interface Bindings {
  DB: D1Database;
  RAW: R2Bucket;
  ASSETS: Fetcher;
  /** ISO 8601 cutoff; commits committed before it are hidden. Matches `aihc_since` in benchmark.json. */
  AIHC_SINCE?: string;
}
