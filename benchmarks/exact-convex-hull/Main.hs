module Main where

import Control.Exception (evaluate)
import Data.IORef (newIORef, readIORef)
import Fixtures (cases)
import Hull (convexHull)
import System.Environment (getArgs)
import System.Exit (die)

-- | Fixed input batches per backend and profile.
-- Each count targets 0.5 seconds with AIHC on an Apple M4 Pro; native and
-- LLVM builds share the native counts.
workSize :: String -> String -> Maybe Int
workSize backend profile
  | backend `elem` ["native", "llvm"] = lookup profile [("O0", 355), ("Os", 481), ("O1", 476), ("O2", 889)]
  | backend == "wasm" = lookup profile [("O0", 48), ("Os", 78), ("O1", 57), ("O2", 151)]
  | otherwise = Nothing

main :: IO ()
main = do
  args <- getArgs
  count <- case args of
    [profile, backend] | Just size <- workSize backend profile -> pure size
    _ -> die "usage: exact-convex-hull <O0|Os|O1|O2> <native|llvm|wasm>"
  input <- newIORef cases
  let loop 0 = putStrLn "ok"
      loop remaining = do
        -- Compare all vertices before the next batch.
        batch <- readIORef input
        ok <- evaluate (all (\(points, expected) -> convexHull points == expected) batch)
        if ok then loop (remaining - 1) else die "fail"
  loop count
