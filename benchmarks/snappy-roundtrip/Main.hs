{-# LANGUAGE MagicHash #-}

module Main where

import Snappy (compress, decompress)
import qualified Data.ByteString as ByteString
import Data.ByteString (ByteString)
import Data.Bits (xor, shiftR, (.&.))
import Data.Word (Word8, Word64)
import GHC.Ptr (Ptr (..))
import System.IO (hPutBuf, stdout)

-- | A small, seedable linear congruential generator. Deterministic across
-- runs and platforms so the benchmark never depends on external data or
-- network access.
nextSeed :: Word64 -> Word64
nextSeed seed = (seed * 6364136223846793005 + 1442695040888963407)

-- | A byte buffer with repeated structure (compressible, like real payloads)
-- rather than pure noise: each 64-byte block is a slice of the PRNG stream
-- XORed with its block index, then the block is repeated four times.
payload :: ByteString
payload = ByteString.pack (concatMap block [0 .. blockCount - 1])
  where
    blockCount = 512 :: Int
    blockSize = 64 :: Int
    block index =
      let seeds = take blockSize (iterate nextSeed (fromIntegral index + 1))
          bytes = map (toByte (fromIntegral index)) seeds
       in concat (replicate 4 bytes)
    toByte :: Word8 -> Word64 -> Word8
    toByte salt seed = fromIntegral ((seed `shiftR` 33) .&. 0xff) `xor` salt

main :: IO ()
main =
  let compressed = compress payload
      ok = case decompress compressed of
        Right roundTripped -> roundTripped == payload && ByteString.length compressed < ByteString.length payload
        Left _ -> False
   in if ok
        then hPutBuf stdout (Ptr "ok\n"# :: Ptr ()) 3
        else hPutBuf stdout (Ptr "fail\n"# :: Ptr ()) 5
