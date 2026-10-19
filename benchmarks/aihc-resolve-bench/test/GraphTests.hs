module Main (main) where

import Control.Monad (unless)
import Data.List (sort)
import qualified Data.Map.Strict as Map
import ModuleGraph

assert :: String -> Bool -> IO ()
assert label ok = unless ok (fail label)

main :: IO ()
main = do
  -- A dependency leaf, a mutually recursive pair, a diamond join,
  -- an independent module with an external import, and a self cycle.
  let inputs = [("D", ["C", "B"], ()), ("C", ["B", "A"], ()),
                ("E", ["External"], ()), ("B", ["A", "C"], ()),
                ("S", ["S"], ()), ("A", [], ())]
  units <- either fail pure (dependencyUnits inputs)
  reversed <- either fail pure (dependencyUnits (reverse inputs))
  assert "input order must not change scheduling" (units == reversed)
  assert "every module must be scheduled once" (sort (concatMap (map fst . unitMembers) units) == ["A", "B", "C", "D", "E", "S"])
  assert "mutual imports must share one SCC" (length [unit | unit <- units, map fst (unitMembers unit) == ["B", "C"], unitIsCyclic unit] == 1)
  assert "self import is a cyclic singleton" (length [unit | unit <- units, map fst (unitMembers unit) == ["S"], unitIsCyclic unit] == 1)
  assert "dependencies must precede their users" (all (\unit -> all (< unitId unit) (unitDependencies unit)) units)
  let owners = Map.fromList [(key, unitId unit) | unit <- units, (key, _) <- unitMembers unit]
      unitOf key = head [unit | unit <- units, unitId unit == owners Map.! key]
  assert "diamond dependencies must be deduplicated" (unitDependencies (unitOf "D") == [owners Map.! "B"])
  assert "cyclic internal edges must not become unit dependencies" (unitDependencies (unitOf "B") == [owners Map.! "A"])
  assert "external imports are supplied outside this package" (null (unitDependencies (unitOf "E")))
  assert "duplicate module names must fail" (case dependencyUnits [("A", [], ()), ("A", [], ())] of Left _ -> True; Right _ -> False)
  assert "empty package graph" (dependencyUnits ([] :: [(String, [String], ())]) == Right [])
  putStrLn "SCC scheduling checks passed"
