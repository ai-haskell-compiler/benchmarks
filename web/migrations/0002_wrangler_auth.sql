-- Migration number: 0002 	 2026-09-11T00:00:00.000Z
-- Uploads go through wrangler with the uploader's own Cloudflare login, so
-- machine tokens are no longer used. D1 cannot drop a table that other
-- tables reference, so the token_hash and display_name columns stay as
-- empty legacy columns; scrub whatever hashes were stored. display_name is
-- never written by the uploader and is left untouched.

UPDATE machines SET token_hash = '' WHERE length(token_hash) > 0;
