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
-- @benchmark.tsv@ is a pool larger than any one run needs. The runner passes
-- the profile and the backend, and each pair preprocesses its own number of
-- modules, an even stride through the pool: an AIHC @-O2@ build is tens of
-- times faster than an @-O0@ one, and a Wasm build slower than a native one.
-- The tally of those modules -- the module count, how many preprocessed
-- without an error diagnostic, and the total output size -- is checked
-- against the one recorded here, and the program prints @ok@. A compiler
-- that miscompiles aihc-cpp changes one of those numbers.
--
-- With @--modules N@ the tally of N modules is printed, which is how the
-- recorded ones are made. With @--report@ the whole corpus (@modules.tsv@,
-- not the pool) is swept and every diagnostic is printed, which is how the
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
import System.Exit (die, exitFailure)
import System.FilePath (takeDirectory, (</>))
import System.IO (hPutStrLn, stderr)

main :: IO ()
main = do
  arguments <- getArgs
  case arguments of
    [corpus, profile, backend] | Just (count, expected) <- work backend profile -> do
      tally <- sweep corpus (Just count) False
      if tally == expected
        then putStrLn "ok"
        else hPutStrLn stderr ("expected " <> expected <> ", got " <> tally) >> putStrLn "fail"
    [corpus, "--modules", count] -> sweep corpus (Just (read count)) False >>= putStrLn
    [corpus, "--report"] -> sweep corpus Nothing True >>= putStrLn
    _ -> do
      hPutStrLn stderr "usage: aihc-cpp-stackage <corpus directory> (<O0|Os|O1|O2> <native|llvm|wasm> | --modules <count> | --report)"
      exitFailure

-- | Modules per backend and profile, and the tally a correct build prints for
-- them. The counts are what take an AIHC build about half a second on an
-- Apple M4 Pro; native and LLVM builds share the native counts.
work :: String -> String -> Maybe (Int, String)
work backend profile
  | backend `elem` ["native", "llvm"] = lookup profile nativeWork >>= withTally
  | backend == "wasm" = lookup profile wasmWork >>= withTally
  | otherwise = Nothing
  where
    nativeWork = [("O0", 15), ("Os", 56), ("O1", 26), ("O2", 701)]
    wasmWork = [("O0", 1), ("Os", 9), ("O1", 2), ("O2", 186)]
    withTally count = (,) count <$> lookup count tallies

-- | The tally of each module count 'work' uses.
tallies :: [(Int, String)]
tallies =
  [ (1, "modules=1 ok=1 errored=0 bytes=10017"),
    (2, "modules=2 ok=2 errored=0 bytes=31774"),
    (9, "modules=9 ok=9 errored=0 bytes=104369"),
    (15, "modules=15 ok=15 errored=0 bytes=207447"),
    (26, "modules=26 ok=26 errored=0 bytes=276888"),
    (56, "modules=56 ok=55 errored=1 bytes=894735"),
    (186, "modules=186 ok=185 errored=1 bytes=3030745"),
    (701, "modules=701 ok=698 errored=3 bytes=10502090")
  ]

-- | @count@ elements spread evenly over the list: the middle one of each of
-- @count@ equal stretches, so a small count is not always led by the first.
spread :: Int -> [a] -> Maybe [a]
spread count items
  | count < 1 || count > total = Nothing
  | otherwise = Just (pick [(2 * index + 1) * total `div` (2 * count) | index <- [0 .. count - 1]] (zip [0 ..] items))
  where
    total = length items
    pick (wanted : later) ((index, item) : rest)
      | wanted == index = item : pick later rest
      | otherwise = pick (wanted : later) rest
    pick _ _ = []

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

-- | Preprocess @count@ modules of the pool, or with 'Nothing' every module
-- of the corpus, and return the tally line.
sweep :: FilePath -> Maybe Int -> Bool -> IO String
sweep corpus count report = do
  macros <- parseMacros <$> BS.readFile (corpus </> "macros.tsv")
  listed <- parseModules corpus <$> BS.readFile (corpus </> maybe "modules.tsv" (const "benchmark.tsv") count)
  modules <- case count of
    Nothing -> pure listed
    Just wanted -> maybe (die ("the pool has " <> show (length listed) <> " modules")) pure (spread wanted listed)
  let config = defaultConfig {configMacros = M.union macros (configMacros defaultConfig)}
  tally <- go config (Tally 0 0 0 0) modules
  pure
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
