module Main where

import Control.Exception (evaluate)
import Data.IORef (newIORef, readIORef)
import Fixtures (vectors)
import Rsa (checkVector)
import System.Environment (getArgs)
import System.Exit (die)

-- | Fixed input batches per backend and profile.
-- Each count targets 0.5 seconds with AIHC on an Apple M4 Pro; native and
-- LLVM builds share the native counts.
workSize :: String -> String -> Maybe Int
workSize backend profile
  | backend `elem` ["native", "llvm"] = lookup profile [("O0", 315), ("Os", 370), ("O1", 355), ("O2", 414)]
  | backend == "wasm" = lookup profile [("O0", 48), ("Os", 86), ("O1", 60), ("O2", 92)]
  | otherwise = Nothing

main :: IO ()
main = do
  args <- getArgs
  count <- case args of
    [profile, backend] | Just size <- workSize backend profile -> pure size
    _ -> die "usage: rsa-verify <O0|Os|O1|O2> <native|llvm|wasm>"
  input <- newIORef vectors
  let loop 0 = putStrLn "ok"
      loop remaining = do
        -- Read the input for each batch to prevent reuse of an earlier result.
        batch <- readIORef input
        ok <- evaluate (all checkVector batch)
        if ok then loop (remaining - 1) else die "fail"
  loop count
