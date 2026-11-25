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
    native = [("O0", 1504), ("O1", 25902), ("O2", 34734), ("Os", 4899)]
    llvm = [("O0", 375), ("O1", 14740), ("O2", 21891), ("Os", 4661)]
    wasm = [("O0", 292), ("O1", 5923), ("O2", 9276), ("Os", 1016)]
    withChecksum count = (,) count <$> lookup count checksums

checksums :: [(Int, Word64)]
checksums =
  [ (292, 1142319073169751143),
    (375, 6158949966968805830),
    (1016, 11700083183939117882),
    (1504, 9532587057381729586),
    (4661, 3187527830397009299),
    (4899, 4193044247811920425),
    (5923, 4638040400181265627),
    (9276, 4039269100543265233),
    (14740, 3726181757161635398),
    (21891, 2782116311488028193),
    (25902, 9391767469005012270),
    (34734, 7514577857681166478)
  ]

main :: IO ()
main = do
  args <- getArgs
  case args of
    ["--print", value] | Just count <- readMaybe value, count > 0 -> print (checksum count)
    [profile, backend] | Just (count, expected) <- work backend profile ->
      if checksum count == expected then putStrLn "ok" else die "checksum mismatch"
    _ -> die "usage: regex-log-extract <O0|O1|O2|Os> <native|llvm|wasm> | --print <positive line count>"
