{-# LANGUAGE BangPatterns #-}
{-# LANGUAGE OverloadedStrings #-}
module Main (main) where

import Aihc.Parser (ParserConfig (..), defaultConfig, parseModule)
import Aihc.Parser.Syntax (Extension (..), ImportDecl (..), LanguageEdition (..), Module (..), moduleName, languageEditionExtensions)
import Aihc.Resolve
import Aihc.Tc
import Control.DeepSeq (rnf)
import Control.Exception (evaluate)
import Data.Bits (xor)
import Data.Char (ord)
import qualified Data.List as List
import qualified Data.Map.Strict as Map
import Data.Maybe (fromMaybe)
import qualified Data.Text as T
import Data.Word (Word64)
import ModuleGraph
import System.Environment (getArgs)
import System.Exit (die)
import Wiring (primTcConfig, primTcWiring)

-- The generated package needs no Prelude interface. Its own algebraic
-- data types and classes exercise kind inference, recursive algebraic
-- data types, generalization, signatures and dictionaries.
source :: Int -> Int -> Int -> T.Text
source count groups index = T.unlines $
  ["module Infer" <> number index <> " where"]
  ++ ["import qualified Infer" <> number (index - 1) <> " as Prev" | index > 0]
  ++ ["import qualified Infer" <> number (index + 1) <> " as Peer" | index `mod` 4 == 1 && index + 1 < count]
  ++ [ "data Tree a = Tip a | Bin (Tree a) (Tree a)"
  , "data Option a = None | Some a"
  , "class Mapping f where { mapping :: (a -> b) -> f a -> f b }"
  , "instance Mapping Tree where { mapping f (Tip x) = Tip (f x); mapping f (Bin l r) = Bin (mapping f l) (mapping f r) }"
  , "instance Mapping Option where { mapping f None = None; mapping f (Some x) = Some (f x) }"
  ] ++ concatMap declarations [0 .. groups - 1]
  ++ ["imported x = Prev.identity0 x" | index > 0]
  ++ ["cycleLink x = Peer.identity0 x" | index `mod` 4 == 1 && index + 1 < count]
  where
    declarations i =
      let n = number i
       in ["identity0 :: a -> a" | i == 0]
          ++ [ "identity" <> n <> " x = x"
          , "compose" <> n <> " :: (b -> c) -> (a -> b) -> a -> c"
          , "compose" <> n <> " f g x = f (g x)"
          , "mapTree" <> n <> " f (Tip x) = Tip (f x)"
          , "mapTree" <> n <> " f (Bin l r) = Bin (mapTree" <> n <> " f l) (mapTree" <> n <> " f r)"
          , "bindOption" <> n <> " None f = None"
          , "bindOption" <> n <> " (Some x) f = f x"
          , "twice" <> n <> " f x = f (f x)"
          , "lifted" <> n <> " f x = mapping f x"
          , "pipeline" <> n <> " x = let ident y = y in mapTree" <> n <> " (compose" <> n <> " ident identity0) x"
          ]

number :: Int -> T.Text
number = T.pack . show

extensions :: [Extension]
extensions = filter (/= ImplicitPrelude) (languageEditionExtensions Haskell2010Edition)

parseOne :: Int -> Int -> Int -> IO Module
parseOne count groups index = do
  let config = defaultConfig {parserSourceName = "Infer" ++ show index, parserExtensions = extensions}
      (errors, parsed) = parseModule config (source count groups index)
  if null errors then evaluate (rnf parsed) >> pure parsed else die (show errors)

-- The wired kind of an ordinary type is TYPE ('BoxedRep 'Lifted). Supply
-- those declarations through the public interface, as an embedding compiler
-- would. They are fixture inputs, independent of the compiler under test.
primitiveSupport :: PackageId -> IO (ModuleExports, TcInterface)
primitiveSupport prim = do
  let primitiveExtensions = extensions ++ [DataKinds, KindSignatures, EmptyDataDecls]
      input = T.unlines
        [ "module GHC.Types where"
        , "data Levity = Lifted | Unlifted"
        , "data RuntimeRep = BoxedRep Levity"
        , "data TYPE (rep :: RuntimeRep)"
        ]
      config = defaultConfig {parserSourceName = "GHC.Types", parserExtensions = primitiveExtensions}
      (errors, parsed) = parseModule config input
      package = Package "benchmark-prim" prim
      units = modulesInPackage package [(parsed, primitiveExtensions)]
      exports = collectModuleExports units
  if null errors then pure () else die (show errors)
  case resolveUnit (builtins package exports []) exports units of
    Left failure -> die (show (failureErrors failure))
    Right (ResolvedUnit resolved) -> do
      let (checked, interface) = typecheckModuleSccWithInterface (primTcConfig prim) emptyTcInterface resolved
      if all tcModuleSuccess checked then pure () else die (show (concatMap tcModuleDiagnostics checked))
      evaluate (rnf interface)
      pure (exports, interface)

hashString :: Word64 -> String -> Word64
hashString = List.foldl' (\h c -> (h `xor` fromIntegral (ord c)) * 1099511628211)

-- Resolve and check each SCC together. Only dependency interfaces and the
-- primitive package are imported, rather than the state of unrelated units.
checkGraph :: Package -> PackageId -> ModuleExports -> TcInterface -> [Unit T.Text ModuleUnit] -> IO [Module]
checkGraph package prim primitiveExports primitiveInterface = go Map.empty []
  where
    go _ results [] = pure (concat (reverse results))
    go interfaces results (unit : rest) = do
      imported <- mapM (\dependency -> maybe (die "SCC dependency not checked") pure (Map.lookup dependency interfaces)) (unitDependencies unit)
      let dependencies = primitiveExports <> mconcat (map fst imported)
          importedInterface = mergeTcInterfaces CheckMergedFacts (primitiveInterface : map snd imported)
          members = map snd (unitMembers unit)
          own = collectModuleExportsWithDeps dependencies members
          visible = own <> dependencies
      case resolveUnit (builtins package visible []) visible members of
        Left failure -> die (show (failureErrors failure))
        Right (ResolvedUnit resolved) -> do
          let (checked, ownInterface) = typecheckModuleSccWithInterface (primTcConfig prim) importedInterface resolved
              completeInterface = mergeTcInterfaces CheckMergedFacts [importedInterface, ownInterface]
          if all tcModuleSuccess checked then pure () else die (show (concatMap tcModuleDiagnostics checked))
          evaluate (rnf checked `seq` rnf completeInterface)
          go (Map.insert (unitId unit) (visible, completeInterface) interfaces) (checked : results) rest

main :: IO ()
main = do
  args <- getArgs
  (count, groups) <- case args of
    [] -> pure (16, 10)
    ["--small"] -> pure (4, 2)
    _ -> die "usage: aihc-tc-bench [--small]"
  let prim = PackageId "benchmark-prim"
  (primitiveExports, primitiveInterface) <- primitiveSupport prim
  parsed <- mapM (parseOne count groups) [0 .. count - 1]
  let package = Package "tc-benchmark" (PackageId "tc-benchmark")
      units = modulesInPackage package [(modu, extensions) | modu <- parsed]
  graph <- either die pure (dependencyUnits
    [(fromMaybe "Main" (moduleName modu), map importDeclModule (moduleImports modu), unit)
    | unit <- reverse units, let modu = moduleUnitAst unit])
  checked <- checkGraph package prim primitiveExports primitiveInterface graph
  -- Force every inferred binding type. NFData of parser syntax alone
  -- does not evaluate the dynamic type annotations attached by TC.
  let bindings = concatMap (tcModuleBindings (primTcWiring prim)) checked
      (!countBindings, !checksum) = List.foldl' step (0 :: Int, 14695981039346656037) bindings
      step (!n, !h) binding = (n + 1, hashString h (show binding))
  evaluate (rnf checked)
  putStrLn ("modules=" ++ show (length checked) ++ " units=" ++ show (length graph) ++ " cyclic=" ++ show (length (filter unitIsCyclic graph)) ++ " bindings=" ++ show countBindings ++ " fnv1a=" ++ show checksum)
