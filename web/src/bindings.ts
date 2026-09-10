export interface Bindings {
  DB: D1Database;
  RAW: R2Bucket;
  ASSETS: Fetcher;
  /** Secret: bearer token that may register machines. */
  ADMIN_TOKEN?: string;
}
