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
import System.Environment (getArgs)
import System.Exit (die)
import Wiring (primTcConfig, primTcWiring)

hashString :: Word64 -> String -> Word64
hashString = List.foldl' (\h c -> (h `xor` fromIntegral (ord c)) * 1099511628211)

-- Resolve and check each SCC together. Only dependency interfaces and the
-- primitive package are imported, rather than the state of unrelated units.
checkGraph :: Package -> PackageId -> ModuleExports -> TcInterface -> [Unit T.Text ModuleUnit] -> IO ([Module], ModuleExports, TcInterface)
checkGraph package prim primitiveExports primitiveInterface = go Map.empty []
  where
    go interfaces results [] = pure (concat (reverse results), primitiveExports <> mconcat (map fst (Map.elems interfaces)), mergeTcInterfaces TrustMergedFacts (primitiveInterface : map snd (Map.elems interfaces)))
    go interfaces results (unit : rest) = do
      imported <- mapM (\dependency -> maybe (die "SCC dependency not checked") pure (Map.lookup dependency interfaces)) (unitDependencies unit)
      let dependencies = primitiveExports <> mconcat (map fst imported)
          importedInterface = mergeTcInterfaces TrustMergedFacts (primitiveInterface : map snd imported)
          members = map snd (unitMembers unit)
          own = collectModuleExportsWithDeps dependencies members
          visible = own <> dependencies
      case resolveUnit (builtins package visible builtinModules) visible members of
        Left failure -> die (show (failureErrors failure))
        Right (ResolvedUnit resolved) -> do
          let (checked, ownInterface) = typecheckModuleSccWithInterface (primTcConfig prim) importedInterface resolved
              completeInterface = mergeTcInterfaces TrustMergedFacts [importedInterface, ownInterface]
          if all tcModuleSuccess checked then pure () else die (show (concatMap tcModuleDiagnostics checked))
          evaluate (rnf checked `seq` rnf completeInterface)
          go (Map.insert (unitId unit) (visible, completeInterface) interfaces) (checked : results) rest

main :: IO ()
main = do
  args <- getArgs
  corpus <- case args of
    [directory] -> pure directory
    _ -> die "usage: aihc-tc-bench <frontend corpus>"
  packages <- loadPackages corpus
  let prim = PackageId "aihc-prim"
      go _ !modules !units !cyclic !bindings !checksum [] =
        putStrLn ("packages=" ++ show (length packages) ++ " modules=" ++ show modules ++ " units=" ++ show units ++ " cyclic=" ++ show cyclic ++ " bindings=" ++ show bindings ++ " fnv1a=" ++ show checksum)
      go interfaces !modules !units !cyclic !bindings !checksum ((name, deps) : rest) = do
        imported <- mapM (\dependency -> maybe (die "package dependency not checked") pure (Map.lookup dependency interfaces)) deps
        parsed <- loadModules corpus name
        let package = Package name (PackageId name)
            dependencyExports = mconcat (map fst imported)
            dependencyTypes = mergeTcInterfaces TrustMergedFacts (map snd imported)
        graph <- either die pure (moduleGraph (modulesInPackage package parsed))
        (checked, exports, interface) <- checkGraph package prim dependencyExports dependencyTypes graph
        let inferred = concatMap (tcModuleBindings (primTcWiring prim)) checked
            (!n, !h) = List.foldl' (\(!count, !hash) binding -> (count + 1, hashString hash (show binding))) (bindings, checksum) inferred
        go (Map.insert name (exports, interface) interfaces) (modules + length checked) (units + length graph) (cyclic + length (filter unitIsCyclic graph)) n h rest
  go Map.empty (0 :: Int) (0 :: Int) (0 :: Int) (0 :: Int) 14695981039346656037 packages
