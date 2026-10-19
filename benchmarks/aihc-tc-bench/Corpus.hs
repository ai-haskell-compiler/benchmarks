{-# LANGUAGE OverloadedStrings #-}
module Corpus (loadPackages, loadModules, moduleGraph, builtinModules, wiredTypeModules) where

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

-- These implicit dependencies match the pinned compiler's sourceModuleUnits:
-- syntax can refer to wired types and functions without a source import.
wiredTypeModules :: [T.Text]
wiredTypeModules = ["GHC.CString", "GHC.Classes", "GHC.Prim", "GHC.Prim.Base", "GHC.Prim.Enum", "GHC.Prim.MonadFail", "GHC.Prim.MonadFix", "GHC.Prim.Num", "GHC.Prim.Real", "GHC.Prim.String", "GHC.Tuple", "GHC.Types"]

builtinModules :: [T.Text]
builtinModules = "GHC.IsList" : wiredTypeModules

loadPackages :: FilePath -> IO [(T.Text, [T.Text])]
loadPackages corpus = do
  input <- TIO.readFile (corpus ++ "/packages.tsv")
  packages <- forM (T.lines input) $ \line -> case T.splitOn "\t" line of
    [name, deps] | not (T.null name) -> pure (name, filter (not . T.null) (T.splitOn ":" deps))
    _ -> die "invalid packages.tsv entry"
  let names = Map.fromList [(name, ()) | (name, _) <- packages]
  if all (all (`Map.member` names) . snd) packages then pure () else die "missing corpus package dependency"
  graph <- either die pure (dependencyUnits [(name, deps, deps) | (name, deps) <- packages])
  if any unitIsCyclic graph then die "cyclic corpus package dependency" else pure (concatMap unitMembers graph)

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
  if null parsed then die ("no modules for " ++ T.unpack package) else pure parsed

moduleGraph :: [ModuleUnit] -> Either String [Unit T.Text ModuleUnit]
moduleGraph units = dependencyUnits
  [(name, dependencies name unit, unit) | unit <- units, let name = fromMaybe "Main" (moduleName (moduleUnitAst unit))]
  where
    dependencies name unit = filter (/= name) wiredTypeModules
      ++ map importDeclModule (moduleImports (moduleUnitAst unit))
      ++ ["Prelude" | ImplicitPrelude `elem` moduleUnitExtensions unit, name /= "Prelude"]
