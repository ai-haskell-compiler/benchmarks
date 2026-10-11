module Main where

import Control.Exception (evaluate)
import Data.IORef (newIORef, readIORef)
import Fixtures (cases)
import Hull (convexHull)
import System.Environment (getArgs)
import System.Exit (die)

-- | Fixed input batches per profile.
-- Each count targets 0.5 seconds with native AIHC on an Apple M4 Pro.
workSize :: String -> Maybe Int
workSize "O0" = Just 184
workSize "Os" = Just 277
workSize "O1" = Just 205
workSize "O2" = Just 491
workSize _ = Nothing

main :: IO ()
main = do
  args <- getArgs
  count <- case args of
    [profile] | Just size <- workSize profile -> pure size
    _ -> die "usage: exact-convex-hull <O0|Os|O1|O2>"
  input <- newIORef cases
  let loop 0 = putStrLn "ok"
      loop remaining = do
        -- Compare all vertices before the next batch.
        batch <- readIORef input
        ok <- evaluate (all (\(points, expected) -> convexHull points == expected) batch)
        if ok then loop (remaining - 1) else die "fail"
  loop count
