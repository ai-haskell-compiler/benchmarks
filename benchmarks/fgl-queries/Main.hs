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

-- | Fixed query counts per profile.
-- Each family targets 0.1 seconds with native AIHC on an Apple M4 Pro.
workSize :: String -> Maybe Sizes
workSize "O0" = Just (Sizes 18 13 2 7 1)
workSize "Os" = Just (Sizes 44 27 5 14 2)
workSize "O1" = Just (Sizes 23 17 3 9 1)
workSize "O2" = Just (Sizes 140 65 18 38 11)
workSize _ = Nothing

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
    [profile] | Just sizes <- workSize profile -> pure sizes
    _ -> die "usage: fgl-queries <O0|Os|O1|O2>"
  let graph = buildGraph graphNodes graphEdges
      network = buildGraph flowNodes flowEdges
  check "shortest paths" (shortestPathSum graph) (take (pathSources sizes) shortestPaths)
  check "levels" (levelSum graph) (take (levelSources sizes) levels)
  check "dominators" (dominatorSum graph) (take (dominatorRoots sizes) dominators)
  check "components" (componentSum graph) (take (removedNodes sizes) components)
  check "flows" (flowValue network) (take (flowPairs sizes) flows)
  putStrLn "ok"
