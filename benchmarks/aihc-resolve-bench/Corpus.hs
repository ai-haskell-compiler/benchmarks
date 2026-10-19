{-# LANGUAGE OverloadedStrings #-}
module Corpus (runWorkload, loadPackages, loadModules, moduleGraph, builtinModules, wiredTypeModules) where

import Aihc.Parser (ParserConfig (..), defaultConfig, parseModule)
import Aihc.Parser.Syntax
import Aihc.Parser.Token (readModuleHeaderPragmas)
import Aihc.Resolve
import Control.DeepSeq (rnf)
import Control.Exception (evaluate)
import Control.Monad (forM)
import qualified Data.Map.Strict as Map
import Data.Maybe (fromMaybe, mapMaybe)
import qualified Data.Text as T
import qualified Data.Text.IO as TIO
import ModuleGraph
import System.Exit (die)
import System.Environment (getArgs)
import Text.Read (readMaybe)
import qualified Data.Set as Set

-- These implicit dependencies match the pinned compiler's sourceModuleUnits:
-- syntax can refer to wired types and functions without a source import.
wiredTypeModules :: [T.Text]
wiredTypeModules = ["GHC.CString", "GHC.Classes", "GHC.Prim", "GHC.Prim.Base", "GHC.Prim.Enum", "GHC.Prim.MonadFail", "GHC.Prim.MonadFix", "GHC.Prim.Num", "GHC.Prim.Real", "GHC.Prim.String", "GHC.Tuple", "GHC.Types"]

builtinModules :: [T.Text]
builtinModules = "GHC.IsList" : wiredTypeModules

loadPackages :: FilePath -> [T.Text] -> IO [(T.Text, [T.Text])]
loadPackages corpus roots = do
  input <- TIO.readFile (corpus ++ "/packages.tsv")
  packages <- forM (T.lines input) $ \line -> case T.splitOn "\t" line of
    [name, deps] | not (T.null name) -> pure (name, filter (not . T.null) (T.splitOn ":" deps))
    _ -> die "invalid packages.tsv entry"
  let names = Map.fromList [(name, ()) | (name, _) <- packages]
  if all (all (`Map.member` names) . snd) packages then pure () else die "missing corpus package dependency"
  graph <- either die pure (dependencyUnits [(name, deps, deps) | (name, deps) <- packages])
  if any unitIsCyclic graph then die "cyclic corpus package dependency" else pure ()
  let byName = Map.fromList packages
      closure seen [] = pure seen
      closure seen (name : rest)
        | name `Set.member` seen = closure seen rest
        | otherwise = case Map.lookup name byName of
            Nothing -> die ("unknown corpus package: " ++ T.unpack name)
            Just deps -> closure (Set.insert name seen) (deps ++ rest)
  selected <- closure Set.empty ("aihc-base" : roots)
  pure [(name, deps) | (name, deps) <- concatMap unitMembers graph, name `Set.member` selected]

loadModules :: FilePath -> T.Text -> IO [(Module, [Extension])]
loadModules corpus package = do
  manifest <- TIO.readFile (corpus ++ "/modules.tsv")
  entries <- forM (T.lines manifest) $ \line -> case T.splitOn "\t" line of
    [name, path, edition, settings] -> pure (name, path, edition, settings)
    _ -> die "invalid modules.tsv entry"
  parsed <- forM [entry | entry@(name, _, _, _) <- entries, name == package] $ \(_, path, edition, settings) -> do
    input <- TIO.readFile (corpus ++ "/" ++ T.unpack path)
    let pragmas = readModuleHeaderPragmas input
        language = fromMaybe (fromMaybe Haskell98Edition (parseLanguageEdition edition)) (headerLanguageEdition pragmas)
        extensions = effectiveExtensions language (mapMaybe parseExtensionSettingName (T.splitOn ":" settings) ++ headerExtensionSettings pragmas)
        config = defaultConfig {parserSourceName = T.unpack path, parserExtensions = extensions}
        (errors, modu) = parseModule config input
    if null errors then evaluate (rnf modu) >> pure (modu, extensions) else die (T.unpack path ++ ": " ++ show errors)
  if package == "aihc-base" && not (any ((== Just "Prelude") . moduleName . fst) parsed)
    then die "the real Prelude is missing from the corpus"
    else pure ()
  if null parsed then die ("no modules for " ++ T.unpack package) else pure parsed

moduleGraph :: [ModuleUnit] -> Either String [Unit T.Text ModuleUnit]
moduleGraph units = dependencyUnits
  [(name, dependencies name unit, unit) | unit <- units, let name = fromMaybe "Main" (moduleName (moduleUnitAst unit))]
  where
    dependencies name unit = filter (/= name) wiredTypeModules
      ++ map importDeclModule (moduleImports (moduleUnitAst unit))
      ++ ["Prelude" | ImplicitPrelude `elem` moduleUnitExtensions unit, name /= "Prelude"]

-- Each cell selects complete packages and repeats a fresh frontend sweep.
-- Package choices and reference tallies live with the pinned corpus inputs.
runWorkload :: String -> (FilePath -> [T.Text] -> IO String) -> IO ()
runWorkload phase sweep = do
  args <- getArgs
  case args of
    [corpus, profile, backend] -> do
      manifest <- TIO.readFile (corpus ++ "/workloads.tsv")
      let matches = [fields | line <- T.lines manifest, let fields = T.splitOn "\t" line,
                     take 3 fields == map T.pack [phase, backend, profile]]
      case matches of
        [[_, _, _, iterations, roots, expected]] -> do
          count <- positiveCount (T.unpack iterations)
          _ <- repeatSweep corpus (T.splitOn "," roots) count (Just (T.unpack expected))
          putStrLn "ok"
        _ -> die "unknown or duplicate frontend workload"
    [corpus, "--packages", roots, "--repeat", iterations] -> do
      count <- positiveCount iterations
      result <- repeatSweep corpus (T.splitOn "," (T.pack roots)) count Nothing
      putStrLn result
    _ -> die "usage: frontend-bench <corpus> <O0|Os|O1|O2> <native|llvm|wasm> | <corpus> --packages <comma-separated names> --repeat <count>"
  where
    positiveCount :: String -> IO Int
    positiveCount value = case readMaybe value of
      Just count | count > 0 -> pure count
      _ -> die "iterations must be a positive integer"
    repeatSweep corpus roots count expected = do
      if null roots || any T.null roots then die "empty package selection" else pure ()
      let go 0 result = pure result
          go n _ = do
            result <- sweep corpus roots
            evaluate (rnf result)
            case expected of
              Just reference | result /= reference -> die ("expected " ++ reference ++ ", got " ++ result)
              _ -> pure ()
            go (n - 1) result
      go count ""
