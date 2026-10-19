{-# LANGUAGE BangPatterns #-}
{-# LANGUAGE OverloadedStrings #-}
module Main (main) where

import Aihc.Resolve
import Aihc.Resolve.Traverse (annotationList)
import Control.DeepSeq (rnf)
import Control.Exception (evaluate)
import qualified Data.List as List
import qualified Data.Map.Strict as Map
import qualified Data.Text as T
import ModuleGraph
import Data.Maybe (mapMaybe)
import Corpus
import StructuralHash
import PhaseTiming
import System.Exit (die)

-- Export scopes are published only after a dependency unit has resolved.
-- The closure retained for each unit supplies transitive imported names.
resolveGraph :: Timings -> Package -> ModuleExports -> [Unit T.Text ModuleUnit] -> IO ([ResolvedModule], ModuleExports)
resolveGraph timings package packageScopes = go Map.empty []
  where
    go scopes results [] = do
      exports <- timed timings "export_collection" $ \() -> evaluate (packageScopes <> mconcat (Map.elems scopes))
      pure (concat (reverse results), exports)
    go scopes results (unit : rest) = do
      (members, visible) <- timed timings "export_collection" $ \() -> do
        imported <- mapM (\dependency -> maybe (die "SCC dependency not resolved") pure (Map.lookup dependency scopes)) (unitDependencies unit)
        let dependencies = packageScopes <> mconcat imported
            members = map snd (unitMembers unit)
            own = collectModuleExportsWithDeps dependencies members
            visible = own <> dependencies
        evaluate (rnf own)
        _ <- evaluate visible
        pure (members, visible)
      resolved <- timed timings "name_resolution" $ \() ->
        case resolveUnit (builtins package visible builtinModules) visible members of
          Left failure -> die (show (failureErrors failure))
          Right (ResolvedUnit resolved) -> do
            _ <- forceResolved resolved
            pure resolved
      go (Map.insert (unitId unit) visible scopes) (resolved : results) rest

main :: IO ()
main = runWorkload "resolve" sweep

sweep :: FilePath -> PackageSelection -> IO String
sweep corpus roots = do
  timings <- newTimings
  packages <- loadPackages timings corpus roots
  let go _ !modules !units !cyclic !references !checksum [] =
        pure ("packages=" ++ show (length packages) ++ " modules=" ++ show modules ++ " units=" ++ show units ++ " cyclic=" ++ show cyclic ++ " resolutions=" ++ show references ++ " digest=" ++ show checksum)
      go scopes !modules !units !cyclic !references !checksum ((name, deps) : rest) = do
        imported <- mapM (\dependency -> maybe (die "package dependency not resolved") pure (Map.lookup dependency scopes)) deps
        parsed <- loadModules timings corpus name
        let package = Package name (PackageId name)
        graph <- planModules timings package parsed
        (resolved, exports) <- resolveGraph timings package (mconcat imported) graph
        (n, h) <- timed timings "checksumming" $ \() -> do
          let annotations = concatMap (mapMaybe annotationResolution . annotationList . moduleUnitAst . resolvedModuleUnit) resolved
              result = List.foldl' (\(!count, !hash) annotation -> (count + 1, hashResolution hash annotation)) (references, checksum) annotations
          evaluate (rnf result `seq` result)
        go (Map.insert name exports scopes) (modules + length parsed) (units + length graph) (cyclic + length (filter unitIsCyclic graph)) n h rest
  result <- go Map.empty (0 :: Int) (0 :: Int) (0 :: Int) (0 :: Int) seed packages
  evaluate (rnf result)
  reportTimings timings
  pure result
