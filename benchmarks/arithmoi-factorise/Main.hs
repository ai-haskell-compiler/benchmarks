module Main where

import Control.Exception (evaluate)
import Factorise (checkCase)
import Fixtures (cases)
import System.Environment (getArgs)
import System.Exit (die)

-- | Each backend and profile has its own fixed case list, run once.
-- The lists differ in the size of the numbers, not in repetition: a repeated
-- batch is work a sufficiently clever compiler could share.
main :: IO ()
main = do
  args <- getArgs
  batch <- case args of
    [profile, backend] | Just batch <- cases backend profile -> pure batch
    _ -> die "usage: arithmoi-factorise <O0|Os|O1|O2> <native|llvm|wasm>"
  ok <- evaluate (all checkCase batch)
  if ok then putStrLn "ok" else die "fail"
