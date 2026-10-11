module Main where

import Control.Exception (evaluate)
import Data.IORef (newIORef, readIORef)
import Fixtures (vectors)
import Rsa (checkVector)
import System.Environment (getArgs)
import System.Exit (die)

-- | Fixed input batches per profile.
-- Each count targets 0.5 seconds with native AIHC on an Apple M4 Pro.
workSize :: String -> Maybe Int
workSize "O0" = Just 4
workSize "Os" = Just 4
workSize "O1" = Just 5
workSize "O2" = Just 5
workSize _ = Nothing

main :: IO ()
main = do
  args <- getArgs
  count <- case args of
    [profile] | Just size <- workSize profile -> pure size
    _ -> die "usage: rsa-verify <O0|Os|O1|O2>"
  input <- newIORef vectors
  let loop 0 = putStrLn "ok"
      loop remaining = do
        -- Read the input for each batch to prevent reuse of an earlier result.
        batch <- readIORef input
        ok <- evaluate (all checkVector batch)
        if ok then loop (remaining - 1) else die "fail"
  loop count
