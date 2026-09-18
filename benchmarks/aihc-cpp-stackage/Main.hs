{-# LANGUAGE OverloadedStrings #-}

-- | Preprocess every module of the CPP corpus with aihc-cpp and print a tally.
--
-- The corpus directory (the flake's @cpp-corpus@ package) says what to do:
-- @benchmark.tsv@ lists each module with the headers to pre-include and the
-- directories to search for its includes, and @macros.tsv@ the macros GHC
-- would define on its command line. This program only follows those
-- instructions, so the same corpus preprocesses the same bytes whichever
-- compiler built the program.
--
-- The printed line is the benchmark's expected output: the module count, how
-- many preprocessed without an error diagnostic, and the total output size.
-- A compiler that miscompiles aihc-cpp changes one of those numbers.
--
-- With @--report@ the whole corpus (@modules.tsv@, not the benchmark's
-- sample) is swept and every diagnostic is printed, which is how the
-- corpus's missing headers and macros are found.
module Main (main) where

import Aihc.Cpp
  ( Config (..),
    Diagnostic (..),
    IncludeRequest (..),
    Result (..),
    Severity (..),
    Step (..),
    defaultConfig,
    preprocess,
  )
import Control.Exception (IOException, try)
import qualified Data.ByteString as BS
import qualified Data.ByteString.Char8 as BS8
import Data.List (foldl')
import qualified Data.Map.Strict as M
import qualified Data.Text as T
import System.Environment (getArgs)
import System.Exit (exitFailure)
import System.FilePath (takeDirectory, (</>))
import System.IO (hPutStrLn, stderr)

main :: IO ()
main = do
  arguments <- getArgs
  case arguments of
    [corpus] -> sweep corpus False
    [corpus, "--report"] -> sweep corpus True
    _ -> do
      hPutStrLn stderr "usage: aihc-cpp-stackage <corpus directory> [--report]"
      exitFailure

-- | One module of the corpus, as a line of @modules.tsv@ describes it.
data Module = Module
  { modulePath :: !FilePath,
    -- | Headers to include before the module, as a real build pre-includes
    -- @ghcversion.h@ and @cabal_macros.h@.
    modulePreludes :: ![FilePath],
    -- | Directories to search for @#include@, after the including file's own.
    moduleSearch :: ![FilePath]
  }

data Tally = Tally
  { tallyModules :: !Int,
    tallyOk :: !Int,
    tallyErrored :: !Int,
    tallyBytes :: !Int
  }

sweep :: FilePath -> Bool -> IO ()
sweep corpus report = do
  macros <- parseMacros <$> BS.readFile (corpus </> "macros.tsv")
  modules <- parseModules corpus <$> BS.readFile (corpus </> (if report then "modules.tsv" else "benchmark.tsv"))
  let config = defaultConfig {configMacros = M.union macros (configMacros defaultConfig)}
  tally <- go config (Tally 0 0 0 0) modules
  putStrLn
    ( "modules="
        <> show (tallyModules tally)
        <> " ok="
        <> show (tallyOk tally)
        <> " errored="
        <> show (tallyErrored tally)
        <> " bytes="
        <> show (tallyBytes tally)
    )
  where
    go _ tally [] = pure tally
    go config tally (m : rest) = do
      result <- preprocessModule config m
      let errored = any ((== Error) . diagSeverity) (resultDiagnostics result)
          tally' =
            Tally
              { tallyModules = tallyModules tally + 1,
                tallyOk = tallyOk tally + (if errored then 0 else 1),
                tallyErrored = tallyErrored tally + (if errored then 1 else 0),
                tallyBytes = tallyBytes tally + BS.length (resultOutput result)
              }
      if report then mapM_ (putStrLn . describe) (resultDiagnostics result) else pure ()
      tally' `seq` go config tally' rest
    describe diagnostic =
      diagFile diagnostic
        <> ":"
        <> show (diagLine diagnostic)
        <> ": "
        <> (case diagSeverity diagnostic of Error -> "error"; Warning -> "warning")
        <> ": "
        <> T.unpack (diagMessage diagnostic)

-- | Preprocess one module, reading its includes from the corpus as they are
-- requested. An include that no search directory holds is answered with
-- 'Nothing', which aihc-cpp reports as an error diagnostic and continues.
preprocessModule :: Config -> Module -> IO Result
preprocessModule config m = do
  source <- BS.readFile (modulePath m)
  let prelude =
        BS.concat
          [ "#include \"" <> BS8.pack path <> "\"\n"
          | path <- modulePreludes m
          ]
          <> "#line 1 \""
          <> BS8.pack (modulePath m)
          <> "\"\n"
  run (preprocess config {configInputFile = modulePath m} (prelude <> source))
  where
    run (Done result) = pure result
    run (NeedInclude request continue) = do
      contents <- firstReadable (candidates request)
      run (continue contents)
    candidates request =
      let from = takeDirectory (includeFrom request)
          own = if null from then takeDirectory (modulePath m) else from
       in [directory </> includePath request | directory <- own : moduleSearch m]
    firstReadable [] = pure Nothing
    firstReadable (candidate : rest) = do
      attempt <- try (BS.readFile candidate) :: IO (Either IOException BS.ByteString)
      case attempt of
        Right contents -> pure (Just contents)
        Left _ -> firstReadable rest

-- | @macros.tsv@: one @NAME<tab>VALUE@ per line.
parseMacros :: BS.ByteString -> M.Map BS.ByteString BS.ByteString
parseMacros = foldl' add M.empty . BS8.lines
  where
    add macros line = case BS8.split '\t' line of
      [name, value] | not (BS.null name) -> M.insert name value macros
      _ -> macros

-- | A module list: @package<tab>module<tab>preludes<tab>search dirs@, the
-- last two colon-separated, every path relative to the corpus root.
parseModules :: FilePath -> BS.ByteString -> [Module]
parseModules corpus = foldr add [] . BS8.lines
  where
    add line rest = case BS8.split '\t' line of
      [_, path, preludes, dirs] | not (BS.null path) ->
        Module
          { modulePath = corpus </> BS8.unpack path,
            modulePreludes = map ((corpus </>) . BS8.unpack) (fields preludes),
            moduleSearch = map ((corpus </>) . BS8.unpack) (fields dirs)
          }
          : rest
      _ -> rest
    fields = filter (not . BS.null) . BS8.split ':'
