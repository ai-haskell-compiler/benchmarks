module Main where

import Data.Digest.Pure.SHA (hmacSha256, sha1, sha256, sha512, showDigest)
import qualified Data.ByteString as Strict
import qualified Data.ByteString.Lazy as Lazy
import Data.Bits (shiftR, (.&.))
import Data.Word (Word8, Word64)
import System.Environment (getArgs)
import System.Exit (die)

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
-- 'messageChunks' * 4 KiB. SHA does not compress, so repetition costs the
-- library the same work as fresh bytes would.
message :: Int -> Lazy.ByteString
message messageChunks = Lazy.fromChunks (replicate messageChunks chunk)

-- | Chunks of message, and the SHA-1, SHA-256, SHA-512 and HMAC-SHA-256
-- digests a correct build produces for that message. A miscompiled library
-- fails the run rather than producing a number.
--
-- The runner passes the profile name, and each profile hashes a different
-- amount. The counts are what take an AIHC native build between half a
-- second and a second on an Apple M1: about 0.69s at @-O0@ (18 chunks),
-- 0.73s at @-O1@ (22), 0.65s at @-Os@ (70) and 0.77s at @-O2@ (1864). @-O2@
-- is far faster than @-O0@, so it is given about a hundred times the message.
work :: String -> Maybe (Int, [String])
work "O0" =
  Just
    ( 18,
      [ "0137f3ae5b0632c3653dde88eb52406a59f952d7",
        "4ab9a224c927cdb1b34d652d2cb892c0d22468cbafbb42e03b205abca864905f",
        "4b0969da1cba854a1d836adede2349257593ab4654f6b3eea875f7ed29895ae8340914118f317616ed7efc877684ead4d152dce686526a381a376b9d7c33c416",
        "43c5fc37ac6845b339829892b6fdd8ddb5b66ac5b6ccc2ac63702d74cdfbc6e4"
      ]
    )
work "Os" =
  Just
    ( 70,
      [ "48576917a810fec52b81e241063a58fdc4a33a2b",
        "343c70101e7d80ed7bc2f3e1ac6b64249715cced8be394b3af5c51cbac5b6fb7",
        "f865898c7c6b3464ac638da9e3a7feab39d6d05682409482e2bcc37e5c92b3b3a2cb6ef97677715de3ac5a2e745d647bb5da4be7746b8858fbcda3e1ab566f38",
        "d3d0343dcbbd47a6692167a94acbd0149c3457dea4c586ae69749a6267432e92"
      ]
    )
work "O1" =
  Just
    ( 22,
      [ "590a7b767d62b7e4fb3529e23778abeb7b747f5a",
        "45ce1b45fc506d930540cc0ee0e1b11c648d8da000a1543f2c0ae9cdac963e90",
        "939306a5ac7faf2bcbd53cb826b5489dcd1f6e15d9a4b092e4e55243ed3d06c470354466b4b1e281acea7981f2bf056d80aa68ff79b0f8f346ee7da6d195c55e",
        "83439f957903406e3fa1bdbbb1e22deebe33c1b0aa39c88565b3b84b87f51bbf"
      ]
    )
work "O2" =
  Just
    ( 1864,
      [ "eca9ff8be304df71ee89f5336adf2b1b96696a1c",
        "d98f3a89929ace0db06ecc1715bd37c7008bf851055bea7cfbc6dea1a750bb96",
        "baf146997f25cf98c8e88a224707b5d3090177143d608927d460c65870e152e84c0d9f92153b8cd75f13cd57c8b23dfd848b73ba4473c5fb29c8e9d5b2174432",
        "bc6837ee2767cf0d669c86ea2621a57cc9a0443c8ab7aaaad8a7e41822f7eb18"
      ]
    )
work _ = Nothing

main :: IO ()
main = do
  args <- getArgs
  (chunks, expected) <- case args of
    [profile] | Just found <- work profile -> pure found
    _ -> die "usage: sha-digest <O0|Os|O1|O2>"
  let body = message chunks
      digests =
        [ showDigest (sha1 body),
          showDigest (sha256 body),
          showDigest (sha512 body),
          showDigest (hmacSha256 (Lazy.fromStrict (Strict.take 32 chunk)) body)
        ]
  putStrLn (if digests == expected then "ok" else "fail")
