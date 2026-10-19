{-# LANGUAGE BangPatterns #-}
{-# LANGUAGE OverloadedStrings #-}
module Main (main) where

import Aihc.Parser.Syntax (Module)
import Aihc.Resolve
import Aihc.Tc
import Control.DeepSeq (rnf)
import Control.Exception (evaluate)
import Data.Bits (xor)
import Data.Char (ord)
import qualified Data.List as List
import qualified Data.Map.Strict as Map
import qualified Data.Text as T
import Data.Word (Word64)
import ModuleGraph
import Corpus
import PhaseTiming
import System.Exit (die)
import Wiring (primTcConfig, primTcWiring)

hashString :: Word64 -> String -> Word64
hashString = List.foldl' (\h c -> (h `xor` fromIntegral (ord c)) * 1099511628211)

-- Resolve and check each SCC together. Only dependency interfaces and the
-- primitive package are imported, rather than the state of unrelated units.
checkGraph :: Timings -> Package -> PackageId -> ModuleExports -> TcInterface -> [Unit T.Text ModuleUnit] -> IO ([Module], ModuleExports, TcInterface)
checkGraph timings package prim primitiveExports primitiveInterface = go Map.empty []
  where
    go interfaces results [] = do
      exports <- timed timings "export_collection" $ \() -> evaluate (primitiveExports <> mconcat (map fst (Map.elems interfaces)))
      types <- timed timings "interface_merging" $ \() -> evaluate (mergeTcInterfaces TrustMergedFacts (primitiveInterface : map snd (Map.elems interfaces)))
      pure (concat (reverse results), exports, types)
    go interfaces results (unit : rest) = do
      imported <- mapM (\dependency -> maybe (die "SCC dependency not checked") pure (Map.lookup dependency interfaces)) (unitDependencies unit)
      (members, visible) <- timed timings "export_collection" $ \() -> do
        let dependencies = primitiveExports <> mconcat (map fst imported)
            members = map snd (unitMembers unit)
            own = collectModuleExportsWithDeps dependencies members
            visible = own <> dependencies
        evaluate (rnf own)
        _ <- evaluate visible
        pure (members, visible)
      importedInterface <- timed timings "interface_merging" $ \() -> evaluate (mergeTcInterfaces TrustMergedFacts (primitiveInterface : map snd imported))
      resolved <- timed timings "name_resolution" $ \() ->
        case resolveUnit (builtins package visible builtinModules) visible members of
          Left failure -> die (show (failureErrors failure))
          Right (ResolvedUnit resolved) -> do
            _ <- forceResolved resolved
            pure resolved
      (checked, ownInterface) <- timed timings "type_checking" $ \() -> do
        let (checked, ownInterface) = typecheckModuleSccWithInterface (primTcConfig prim) importedInterface resolved
            inferred = concatMap (tcModuleBindings (primTcWiring prim)) checked
            forceBinding () binding = rnf (tbKey binding) `seq` rnf (tbDisplayName binding) `seq` rnf (tbType binding)
        if all tcModuleSuccess checked then pure () else die (show (concatMap tcModuleDiagnostics checked))
        -- Materialize new facts and dynamic inferred types here, so their
        -- evaluation cannot be charged to the later Show/FNV checksum.
        evaluate (rnf checked `seq` rnf ownInterface `seq` List.foldl' forceBinding () inferred)
        pure (checked, ownInterface)
      completeInterface <- timed timings "interface_merging" $ \() -> evaluate (mergeTcInterfaces TrustMergedFacts [importedInterface, ownInterface])
      go (Map.insert (unitId unit) (visible, completeInterface) interfaces) (checked : results) rest

main :: IO ()
main = runWorkload "tc" sweep

sweep :: FilePath -> [T.Text] -> IO String
sweep corpus roots = do
  timings <- newTimings
  packages <- loadPackages timings corpus roots
  let prim = PackageId "aihc-prim"
      go _ !modules !units !cyclic !bindings !checksum [] =
        pure ("packages=" ++ show (length packages) ++ " modules=" ++ show modules ++ " units=" ++ show units ++ " cyclic=" ++ show cyclic ++ " bindings=" ++ show bindings ++ " fnv1a=" ++ show checksum)
      go interfaces !modules !units !cyclic !bindings !checksum ((name, deps) : rest) = do
        imported <- mapM (\dependency -> maybe (die "package dependency not checked") pure (Map.lookup dependency interfaces)) deps
        parsed <- loadModules timings corpus name
        let package = Package name (PackageId name)
        dependencyExports <- timed timings "export_collection" $ \() -> evaluate (mconcat (map fst imported))
        dependencyTypes <- timed timings "interface_merging" $ \() -> evaluate (mergeTcInterfaces TrustMergedFacts (map snd imported))
        graph <- planModules timings package parsed
        (checked, exports, interface) <- checkGraph timings package prim dependencyExports dependencyTypes graph
        (n, h) <- timed timings "checksumming" $ \() -> do
          let inferred = concatMap (tcModuleBindings (primTcWiring prim)) checked
              result = List.foldl' (\(!count, !hash) binding -> (count + 1, hashString hash (show binding))) (bindings, checksum) inferred
          evaluate (rnf result `seq` result)
        go (Map.insert name (exports, interface) interfaces) (modules + length parsed) (units + length graph) (cyclic + length (filter unitIsCyclic graph)) n h rest
  result <- go Map.empty (0 :: Int) (0 :: Int) (0 :: Int) (0 :: Int) 14695981039346656037 packages
  evaluate (rnf result)
  reportTimings timings
  pure result
