{-# LANGUAGE BangPatterns #-}
module Main where

import Data.Array (elems)
import Data.Bits (xor)
import qualified Data.ByteString.Char8 as B
import Data.List (foldl')
import Data.Word (Word64)
import System.Environment (getArgs)
import System.Exit (die)
import Text.Read (readMaybe)
import Text.Regex.TDFA (Regex, makeRegex, matchOnce)

-- One compiled expression shared by all lines, with six capture groups.
regex :: Regex
regex = makeRegex ("^([0-9.]+) - - \\[([^]]+)\\] \"(GET|POST|PUT|DELETE) ([^ ]+) HTTP/1[.][01]\" ([0-9]{3}) ([0-9]+)$" :: String)

-- Vary field widths and values; every fifth line fails at its final byte.
-- Each input depends on its index, so no repeated pure match can be shared.
logLine :: Int -> B.ByteString
logLine i = B.pack $
  "192.168." ++ show (i `mod` 256) ++ "." ++ show ((i * 17) `mod` 256)
  ++ " - - [19/Oct/2026:12:34:56 +0000] \""
  ++ (["GET", "POST", "PUT", "DELETE"] !! (i `mod` 4))
  ++ " /api/items/" ++ show i ++ "?page=" ++ show (i `mod` 100)
  ++ " HTTP/1." ++ show (i `mod` 2) ++ "\" "
  ++ show (200 + i `mod` 400) ++ " " ++ show (i * 7919 `mod` 1000000)
  ++ if i `mod` 5 == 0 then "x" else ""

mix :: Word64 -> Int -> Word64
mix h x = (h `xor` fromIntegral x) * 1099511628211

-- Force every whole-match and capture offset/length, including failed matches.
checksum :: Int -> Word64
checksum count = go 1 14695981039346656037
  where
    go i !h
      | i > count = h
      | otherwise =
          let h' = case matchOnce regex (logLine i) of
                Nothing -> mix h (-1)
                Just captures -> foldl' (\a (offset, len) -> mix (mix a offset) len) (mix h 1) (elems captures)
           in go (i + 1) h'

-- Lines per profile and backend, calibrated on an Apple M4 Pro.
-- The GHC checksums force all captures and reject incorrect results.
-- See docs/regex-log-extract-calibration.json for measurements.
work :: String -> String -> Maybe (Int, Word64)
work backend profile
  | backend == "native" = lookup profile native >>= withChecksum
  | backend == "llvm" = lookup profile llvm >>= withChecksum
  | backend == "wasm" = lookup profile wasm >>= withChecksum
  | otherwise = Nothing
  where
    native = [("O0", 928), ("O1", 942), ("O2", 6391), ("Os", 2087)]
    llvm = [("O0", 324), ("O1", 900), ("O2", 5429), ("Os", 2088)]
    wasm = [("O0", 50), ("O1", 39), ("O2", 1160), ("Os", 386)]
    withChecksum count = (,) count <$> lookup count checksums

checksums :: [(Int, Word64)]
checksums =
  [ (39, 3993527110918714970),
    (50, 6871092716819489657),
    (324, 11383818978105109628),
    (386, 4065897987643536334),
    (900, 8994228263199998037),
    (928, 1661991329759633859),
    (942, 5055756946563895876),
    (1160, 6713720550506216183),
    (2087, 14323463360434935996),
    (2088, 1246508664477525179),
    (5429, 15825121225549780716),
    (6391, 7274849593290202218)
  ]

main :: IO ()
main = do
  args <- getArgs
  case args of
    ["--print", value] | Just count <- readMaybe value, count > 0 -> print (checksum count)
    [profile, backend] | Just (count, expected) <- work backend profile ->
      if checksum count == expected then putStrLn "ok" else die "checksum mismatch"
    _ -> die "usage: regex-log-extract <O0|O1|O2|Os> <native|llvm|wasm> | --print <positive line count>"
