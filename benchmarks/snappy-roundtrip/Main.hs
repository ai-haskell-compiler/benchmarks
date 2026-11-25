{-# LANGUAGE MagicHash #-}

module Main where

import Snappy (compress, decompress)
import qualified Data.ByteString as ByteString
import Data.ByteString (ByteString)
import Data.Bits (xor, shiftR, (.&.))
import Data.Word (Word8, Word64)
import GHC.Ptr (Ptr (..))
import System.Environment (getArgs)
import System.Exit (die)
import System.IO (hPutBuf, stdout)

-- | A small, seedable linear congruential generator. Deterministic across
-- runs and platforms so the benchmark never depends on external data or
-- network access.
nextSeed :: Word64 -> Word64
nextSeed seed = (seed * 6364136223846793005 + 1442695040888963407)

-- | A byte buffer with repeated structure (compressible, like real payloads)
-- rather than pure noise: each 64-byte block is a slice of the PRNG stream
-- XORed with its block index, then the block is repeated four times. The
-- block count is per backend and profile, because list construction
-- dominates the run under AIHC and the cells do it at different speeds.
payload :: Int -> ByteString
payload blockCount = ByteString.pack (concatMap block [0 .. blockCount - 1])
  where
    blockSize = 64 :: Int
    block index =
      let seeds = take blockSize (iterate nextSeed (fromIntegral index + 1))
          bytes = map (toByte (fromIntegral index)) seeds
       in concat (replicate 4 bytes)
    toByte :: Word8 -> Word64 -> Word8
    toByte salt seed = fromIntegral ((seed `shiftR` 33) .&. 0xff) `xor` salt

-- | Blocks per backend and profile. See 'payload'.
-- Each count targets 0.5 seconds with AIHC on an Apple M4 Pro.
workSize :: String -> String -> Maybe Int
workSize backend profile
  | backend == "native" = lookup profile [("O0", 5861), ("Os", 16754), ("O1", 65000), ("O2", 64866)]
  | backend == "llvm" = lookup profile [("O0", 1550), ("Os", 15886), ("O1", 59583), ("O2", 64464)]
  | backend == "wasm" = lookup profile [("O0", 1162), ("Os", 4395), ("O1", 35180), ("O2", 36392)]
  | otherwise = Nothing

main :: IO ()
main = do
  args <- getArgs
  blockCount <- case args of
    [profile, backend] | Just size <- workSize backend profile -> pure size
    _ -> die "usage: snappy-roundtrip <O0|Os|O1|O2> <native|llvm|wasm>"
  let bytes = payload blockCount
      compressed = compress bytes
      ok = case decompress compressed of
        Right roundTripped -> roundTripped == bytes && ByteString.length compressed < ByteString.length bytes
        Left _ -> False
  if ok
    then hPutBuf stdout (Ptr "ok\n"# :: Ptr ()) 3
    else hPutBuf stdout (Ptr "fail\n"# :: Ptr ()) 5
