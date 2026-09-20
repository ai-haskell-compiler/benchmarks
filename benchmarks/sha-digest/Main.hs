{-# LANGUAGE MagicHash #-}

module Main where

import Data.Digest.Pure.SHA (hmacSha256, sha1, sha256, sha512, showDigest)
import qualified Data.ByteString as Strict
import qualified Data.ByteString.Lazy as Lazy
import Data.Bits (shiftR, (.&.))
import Data.Word (Word8, Word64)
import GHC.Ptr (Ptr (..))
import System.IO (hPutBuf, stdout)

-- | A small, seedable linear congruential generator. Deterministic across
-- runs and platforms so the benchmark never depends on external data or
-- network access.
nextSeed :: Word64 -> Word64
nextSeed seed = seed * 6364136223846793005 + 1442695040888963407

-- | One 4 KiB chunk of PRNG bytes. It is built once with 'Strict.unfoldrN'
-- rather than through a list, so producing the message is a small fraction
-- of hashing it and the run measures the SHA library rather than 'base'.
chunk :: Strict.ByteString
chunk = fst (Strict.unfoldrN 4096 step 42)
  where
    step :: Word64 -> Maybe (Word8, Word64)
    step seed = Just (fromIntegral ((seed `shiftR` 33) .&. 0xff), nextSeed seed)

-- | The message every digest is taken over: the chunk repeated to
-- 'messageChunks' * 4 KiB, 128 KiB in all. SHA does not compress, so
-- repetition costs the library the same work as fresh bytes would. The chunk
-- count sizes the AIHC -O0 build to about four seconds on an Apple M4 Pro
-- (the -O0 run is the slowest of the profiles and must stay inside the
-- suite's per-process timeout); the GHC builds finish in milliseconds.
message :: Lazy.ByteString
message = Lazy.fromChunks (replicate messageChunks chunk)
  where
    messageChunks = 32 :: Int

-- | The digests the reference GHC build produces; a miscompiled library
-- fails the run rather than producing a number.
expected :: [String]
expected =
  [ "c28c7264ccb164ea926a2e9c2d3308c5dcc5f16b"
  , "f25eeadbecb60d846d34f7cec513947eb0a0baf2dc77072fa4edabcc995bfd72"
  , "d2bce245178897a7e434ee9a79fc9f3d590eb8a5ece7985d8e6e6921c8047c3f2536cc88b15f83e37d37ecd87095fd465c42f6823b9e77f1193aafd81ab69d40"
  , "61c255f49fe9dfb8882f4166910e2c7bf2bccba53911c359fe5aa44acaf29648"
  ]

main :: IO ()
main =
  let digests =
        [ showDigest (sha1 message)
        , showDigest (sha256 message)
        , showDigest (sha512 message)
        , showDigest (hmacSha256 (Lazy.fromStrict (Strict.take 32 chunk)) message)
        ]
   in if digests == expected
        then hPutBuf stdout (Ptr "ok\n"# :: Ptr ()) 3
        else hPutBuf stdout (Ptr "fail\n"# :: Ptr ()) 5
