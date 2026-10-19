module Main where

import Control.Exception (evaluate)
import Fixtures
import Queries
import System.Environment (getArgs)
import System.Exit (die)

-- | How many queries of each family a profile runs. Every query is distinct:
-- a family never runs the same computation twice.
data Sizes = Sizes
  { pathSources :: Int,
    levelSources :: Int,
    dominatorRoots :: Int,
    removedNodes :: Int,
    flowPairs :: Int
  }

-- | Fixed query counts per backend and profile.
-- Each family targets 0.1 seconds with AIHC on an Apple M4 Pro; native and
-- LLVM builds share the native counts.
workSize :: String -> String -> Maybe Sizes
workSize backend profile
  | backend `elem` ["native", "llvm"] =
      lookup profile [("O0", Sizes 18 13 2 7 1), ("Os", Sizes 42 26 5 14 2), ("O1", Sizes 27 20 3 10 1), ("O2", Sizes 167 78 21 45 13)]
  | backend == "wasm" =
      lookup profile [("O0", Sizes 3 2 1 1 1), ("Os", Sizes 11 6 1 3 1), ("O1", Sizes 4 3 1 1 1), ("O2", Sizes 39 18 5 10 3)]
  | otherwise = Nothing

check :: String -> (query -> Int) -> [(query, Int)] -> IO ()
check name answer table = mapM_ one table
  where
    one (query, expected) = do
      actual <- evaluate (answer query)
      if actual == expected then pure () else die ("fail: " ++ name)

main :: IO ()
main = do
  args <- getArgs
  sizes <- case args of
    [profile, backend] | Just sizes <- workSize backend profile -> pure sizes
    _ -> die "usage: fgl-queries <O0|Os|O1|O2> <native|llvm|wasm>"
  let graph = buildGraph graphNodes graphEdges
      network = buildGraph flowNodes flowEdges
  check "shortest paths" (shortestPathSum graph) (take (pathSources sizes) shortestPaths)
  check "levels" (levelSum graph) (take (levelSources sizes) levels)
  check "dominators" (dominatorSum graph) (take (dominatorRoots sizes) dominators)
  check "components" (componentSum graph) (take (removedNodes sizes) components)
  check "flows" (flowValue network) (take (flowPairs sizes) flows)
  putStrLn "ok"
