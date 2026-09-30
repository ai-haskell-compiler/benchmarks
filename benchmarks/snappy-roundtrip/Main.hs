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
-- block count is per profile, because list construction dominates the run
-- under AIHC and the profiles do it at different speeds. On an Apple M1 an
-- AIHC native build takes about 0.77s at @-O0@ (2687 blocks), 0.77s at
-- @-O1@ (2744), 0.79s at @-Os@ (5987) and 0.71s at @-O2@ (11293).
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

-- | Blocks per profile. See 'payload'.
workSize :: String -> Maybe Int
workSize "O0" = Just 2687
workSize "Os" = Just 5987
workSize "O1" = Just 2744
workSize "O2" = Just 11293
workSize _ = Nothing

main :: IO ()
main = do
  args <- getArgs
  blockCount <- case args of
    [profile] | Just size <- workSize profile -> pure size
    _ -> die "usage: snappy-roundtrip <O0|Os|O1|O2>"
  let bytes = payload blockCount
      compressed = compress bytes
      ok = case decompress compressed of
        Right roundTripped -> roundTripped == bytes && ByteString.length compressed < ByteString.length bytes
        Left _ -> False
  if ok
    then hPutBuf stdout (Ptr "ok\n"# :: Ptr ()) 3
    else hPutBuf stdout (Ptr "fail\n"# :: Ptr ()) 5
