-- Migration number: 0002 	 2026-09-11T00:00:00.000Z
-- Uploads go through wrangler with the uploader's own Cloudflare login, so
-- machine tokens are no longer used. D1 cannot drop a table that other
-- tables reference, so the token_hash and display_name columns stay as
-- empty legacy columns; scrub whatever hashes were stored.

UPDATE machines SET token_hash = '', display_name = NULL
  WHERE token_hash <> '' OR display_name IS NOT NULL;
