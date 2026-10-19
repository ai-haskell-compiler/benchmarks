{-# LANGUAGE BangPatterns #-}
{-# LANGUAGE OverloadedStrings #-}
module Main (main) where

import Aihc.Parser (ParserConfig (..), defaultConfig, parseModule)
import Aihc.Parser.Syntax (Extension (..), LanguageEdition (..), Module (..), languageEditionExtensions)
import Aihc.Resolve
import Aihc.Resolve.Traverse (annotationList)
import Control.DeepSeq (rnf)
import Control.Exception (evaluate)
import Data.Bits (xor)
import Data.Char (ord)
import qualified Data.List as List
import Data.Maybe (mapMaybe)
import qualified Data.Text as T
import Data.Word (Word64)
import System.Environment (getArgs)
import System.Exit (die)

-- Each module has fresh names and imports its predecessor. The references
-- exercise qualified imports, global and shadowed local binders, recursive
-- patterns, let groups, case alternatives, and operator fixities.
source :: Int -> Int -> T.Text
source groups index = T.unlines $
  ["module " <> owner <> " where"]
  ++ ["import qualified Resolve" <> number (index - 1) <> " as Prev" | index > 0]
  ++ [ "data Tree a = Tip a | Bin (Tree a) (Tree a)"
     , "infixr 5 <+>"
     , "(<+>) f g x = f (g x)"
     ]
  ++ concatMap declarations [0 .. groups - 1]
  ++ ["imported x = Prev.identity0 x" | index > 0]
  where
    owner = "Resolve" <> number index
    declarations i =
      let n = number i
       in [ "identity" <> n <> " x = x"
          , "compose" <> n <> " f g x = f (g x)"
          , "mapTree" <> n <> " f (Tip x) = Tip (f x)"
          , "mapTree" <> n <> " f (Bin l r) = Bin (mapTree" <> n <> " f l) (mapTree" <> n <> " f r)"
          , "scope" <> n <> " x = let apply f x = f x; keep y = x in apply keep x"
          , "choose" <> n <> " t = case t of { Tip x -> x; Bin l r -> choose" <> n <> " l }"
          , "pipeline" <> n <> " = identity" <> n <> " <+> identity0 <+> identity" <> n
          ]

number :: Int -> T.Text
number = T.pack . show

extensions :: [Extension]
extensions = filter (/= ImplicitPrelude) (languageEditionExtensions Haskell2010Edition)

parseOne :: Int -> Int -> IO Module
parseOne groups index = do
  let config = defaultConfig {parserSourceName = "Resolve" ++ show index, parserExtensions = extensions}
      (errors, parsed) = parseModule config (source groups index)
  if null errors then evaluate (rnf parsed) >> pure parsed else die (show errors)

hashString :: Word64 -> String -> Word64
hashString = List.foldl' (\h c -> (h `xor` fromIntegral (ord c)) * 1099511628211)

main :: IO ()
main = do
  args <- getArgs
  (count, groups) <- case args of
    [] -> pure (24, 20)
    ["--small"] -> pure (2, 2)
    _ -> die "usage: aihc-resolve-bench [--small]"
  parsed <- mapM (parseOne groups) [0 .. count - 1]
  let package = Package "resolve-benchmark" (PackageId "resolve-benchmark")
      units = modulesInPackage package [(modu, extensions) | modu <- parsed]
      exports = collectModuleExports units
  case resolveUnit (builtins package exports []) exports units of
    Left failure -> die (show (failureErrors failure))
    Right (ResolvedUnit resolved) -> do
      -- Parser NFData deliberately ignores dynamic annotations. Consume
      -- every resolution explicitly, including the entity it points to.
      let annotations = concatMap (mapMaybe annotationResolution . annotationList . moduleUnitAst . resolvedModuleUnit) resolved
          (!references, !checksum) = List.foldl' step (0 :: Int, 14695981039346656037) annotations
          step (!n, !h) annotation = (n + 1, hashString h (show annotation))
      evaluate (rnf (map (moduleUnitAst . resolvedModuleUnit) resolved))
      putStrLn ("modules=" ++ show (length resolved) ++ " resolutions=" ++ show references ++ " fnv1a=" ++ show checksum)
