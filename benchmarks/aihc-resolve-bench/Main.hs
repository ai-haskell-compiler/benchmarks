{-# LANGUAGE BangPatterns #-}
{-# LANGUAGE OverloadedStrings #-}
module Main (main) where

import Aihc.Resolve
import Aihc.Resolve.Traverse (annotationList)
import Control.DeepSeq (rnf)
import Control.Exception (evaluate)
import Data.Bits (xor)
import Data.Char (ord)
import qualified Data.List as List
import qualified Data.Map.Strict as Map
import Data.Maybe (mapMaybe)
import qualified Data.Text as T
import Data.Word (Word64)
import ModuleGraph
import Corpus
import System.Environment (getArgs)
import System.Exit (die)

hashString :: Word64 -> String -> Word64
hashString = List.foldl' (\h c -> (h `xor` fromIntegral (ord c)) * 1099511628211)

-- Export scopes are published only after a dependency unit has resolved.
-- The closure retained for each unit supplies transitive imported names.
resolveGraph :: Package -> ModuleExports -> [Unit T.Text ModuleUnit] -> IO ([ResolvedModule], ModuleExports)
resolveGraph package packageScopes = go Map.empty []
  where
    go scopes results [] = pure (concat (reverse results), packageScopes <> mconcat (Map.elems scopes))
    go scopes results (unit : rest) = do
      imported <- mapM (\dependency -> maybe (die "SCC dependency not resolved") pure (Map.lookup dependency scopes)) (unitDependencies unit)
      let dependencies = packageScopes <> mconcat imported
          members = map snd (unitMembers unit)
          own = collectModuleExportsWithDeps dependencies members
          visible = own <> dependencies
      case resolveUnit (builtins package visible builtinModules) visible members of
        Left failure -> die (show (failureErrors failure))
        Right (ResolvedUnit resolved) -> do
          evaluate (rnf (map (moduleUnitAst . resolvedModuleUnit) resolved))
          go (Map.insert (unitId unit) visible scopes) (resolved : results) rest

main :: IO ()
main = do
  args <- getArgs
  corpus <- case args of
    [directory] -> pure directory
    _ -> die "usage: aihc-resolve-bench <frontend corpus>"
  packages <- loadPackages corpus
  let go _ !modules !units !cyclic !references !checksum [] =
        putStrLn ("packages=" ++ show (length packages) ++ " modules=" ++ show modules ++ " units=" ++ show units ++ " cyclic=" ++ show cyclic ++ " resolutions=" ++ show references ++ " fnv1a=" ++ show checksum)
      go scopes !modules !units !cyclic !references !checksum ((name, deps) : rest) = do
        imported <- mapM (\dependency -> maybe (die "package dependency not resolved") pure (Map.lookup dependency scopes)) deps
        parsed <- loadModules corpus name
        let package = Package name (PackageId name)
        graph <- either die pure (moduleGraph (modulesInPackage package parsed))
        (resolved, exports) <- resolveGraph package (mconcat imported) graph
        let annotations = concatMap (mapMaybe annotationResolution . annotationList . moduleUnitAst . resolvedModuleUnit) resolved
            (!n, !h) = List.foldl' (\(!count, !hash) annotation -> (count + 1, hashString hash (show annotation))) (references, checksum) annotations
        go (Map.insert name exports scopes) (modules + length resolved) (units + length graph) (cyclic + length (filter unitIsCyclic graph)) n h rest
  go Map.empty (0 :: Int) (0 :: Int) (0 :: Int) (0 :: Int) 14695981039346656037 packages
