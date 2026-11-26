module Main where

import Control.Exception (evaluate)
import Data.IORef (newIORef, readIORef)
import Fixtures (vectors)
import Rsa (checkVector)
import System.Environment (getArgs)
import System.Exit (die)

-- | Fixed input batches per backend and profile.
-- Each count targets 0.5 seconds with AIHC on an Apple M4 Pro.
workSize :: String -> String -> Maybe Int
workSize backend profile
  | backend == "native" = lookup profile [("O0", 716), ("Os", 989), ("O1", 1277), ("O2", 1353)]
  | backend == "llvm" = lookup profile [("O0", 264), ("Os", 949), ("O1", 1241), ("O2", 1344)]
  | backend == "wasm" = lookup profile [("O0", 112), ("Os", 152), ("O1", 173), ("O2", 183)]
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
