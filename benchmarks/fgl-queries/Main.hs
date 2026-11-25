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
-- Each family targets 0.1 seconds with AIHC on an Apple M4 Pro, except where
-- the fixture tables (300 entries, 48 flow pairs) cap it and the other
-- families make up the difference.
workSize :: String -> String -> Maybe Sizes
workSize backend profile
  | backend == "native" =
      lookup profile [("O0", Sizes 32 23 4 12 2), ("Os", Sizes 80 50 10 27 4), ("O1", Sizes 211 156 23 78 8), ("O2", Sizes 300 171 46 98 28)]
  | backend == "llvm" =
      lookup profile [("O0", Sizes 1 1 1 1 1), ("Os", Sizes 76 47 9 25 4), ("O1", Sizes 196 145 22 72 7), ("O2", Sizes 300 161 43 93 27)]
  | backend == "wasm" =
      lookup profile [("O0", Sizes 1 1 1 1 1), ("Os", Sizes 14 8 2 4 2), ("O1", Sizes 37 28 9 9 9), ("O2", Sizes 125 58 16 32 10)]
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
