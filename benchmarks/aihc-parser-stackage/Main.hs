{-# LANGUAGE OverloadedStrings #-}

-- | Parse every module of the parser corpus with aihc-parser and print a tally.
--
-- The corpus directory (the flake's @parser-corpus@ package) says what to do:
-- @benchmark.tsv@ lists each module with the language edition and the
-- extensions its package declares, which are what a build hands the parser
-- before the module's own @LANGUAGE@ pragmas. This program only follows those
-- instructions, so the same corpus parses the same bytes whichever compiler
-- built the program.
--
-- Each module is read, decoded, parsed and forced completely -- the whole
-- syntax tree and the recovered parse errors -- before the next one is
-- started, which is what a compiler front end does with a parse tree.
--
-- @benchmark.tsv@ is a pool larger than any one run needs. The runner passes
-- the profile and the backend, and each pair parses its own number of
-- modules, an even stride through the pool: an AIHC @-O2@ build is many
-- times faster than an @-O0@ one, and a Wasm build slower than a native one.
-- The tally of those modules -- the module count, how many parsed without an
-- error, and the number of imports and declarations across every tree -- is
-- checked against the one recorded here, and the program prints @ok@. A
-- compiler that miscompiles aihc-parser changes one of those numbers.
--
-- With @--modules N@ the tally of N modules is printed, which is how the
-- recorded ones are made. With @--report@ the whole corpus (@modules.tsv@,
-- not the pool) is swept and every parse error is printed, which is how the
-- corpus's selection rules were checked.
module Main (main) where

import Aihc.Parser (ParserConfig (..), defaultConfig, formatParseErrors, parseModule)
import Aihc.Parser.Syntax
  ( Extension,
    LanguageEdition (..),
    Module (..),
    SourceSpan,
    applyExtensionSetting,
    languageEditionExtensions,
    parseExtensionSettingName,
    parseLanguageEdition,
  )
import Control.DeepSeq (rnf)
import Control.Exception (evaluate)
import qualified Data.ByteString as BS
import qualified Data.ByteString.Char8 as BS8
import Data.Maybe (fromMaybe, mapMaybe)
import qualified Data.Text as T
import qualified Data.Text.Encoding as TE
import System.Environment (getArgs)
import System.Exit (die, exitFailure)
import System.FilePath ((</>))
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
      hPutStrLn stderr "usage: aihc-parser-stackage <corpus directory> (<O0|Os|O1|O2> <native|llvm|wasm> | --modules <count> | --report)"
      exitFailure

-- | Modules per backend and profile, and the tally a correct build prints for
-- them. The counts are what take an AIHC build about half a second on an
-- Apple M4 Pro.
work :: String -> String -> Maybe (Int, String)
work backend profile
  | backend == "native" = lookup profile nativeWork >>= withTally
  | backend == "llvm" = lookup profile llvmWork >>= withTally
  | backend == "wasm" = lookup profile wasmWork >>= withTally
  | otherwise = Nothing
  where
    nativeWork = [("O0", 38), ("Os", 290), ("O1", 613), ("O2", 1257)]
    llvmWork = [("O0", 13), ("Os", 239), ("O1", 500), ("O2", 959)]
    wasmWork = [("O0", 5), ("Os", 26), ("O1", 40), ("O2", 300)]
    withTally count = (,) count <$> lookup count tallies

-- | The tally of each module count 'work' uses.
tallies :: [(Int, String)]
tallies =
  [ (5, "modules=5 ok=5 errored=0 imports=25 decls=57"),
    (13, "modules=13 ok=13 errored=0 imports=55 decls=116"),
    (26, "modules=26 ok=26 errored=0 imports=147 decls=354"),
    (38, "modules=38 ok=38 errored=0 imports=203 decls=458"),
    (40, "modules=40 ok=40 errored=0 imports=212 decls=469"),
    (239, "modules=239 ok=239 errored=0 imports=1436 decls=3227"),
    (290, "modules=290 ok=290 errored=0 imports=1763 decls=4135"),
    (300, "modules=300 ok=300 errored=0 imports=1880 decls=4285"),
    (500, "modules=500 ok=500 errored=0 imports=2969 decls=6690"),
    (613, "modules=613 ok=613 errored=0 imports=3676 decls=8623"),
    (959, "modules=959 ok=959 errored=0 imports=5973 decls=13239"),
    (1257, "modules=1257 ok=1257 errored=0 imports=7706 decls=17214")
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
data Entry = Entry
  { entryPath :: !FilePath,
    -- | The package's @default-language@, or empty for Cabal's default.
    entryLanguage :: !T.Text,
    -- | The package's @default-extensions@, as written.
    entryExtensions :: ![T.Text]
  }

data Tally = Tally
  { tallyModules :: !Int,
    tallyOk :: !Int,
    tallyErrored :: !Int,
    tallyImports :: !Int,
    tallyDecls :: !Int
  }

-- | Parse @count@ modules of the pool, or with 'Nothing' every module of the
-- corpus, and return the tally line.
sweep :: FilePath -> Maybe Int -> Bool -> IO String
sweep corpus count report = do
  listed <- parseEntries corpus <$> BS.readFile (corpus </> maybe "modules.tsv" (const "benchmark.tsv") count)
  entries <- case count of
    Nothing -> pure listed
    Just wanted -> maybe (die ("the pool has " <> show (length listed) <> " modules")) pure (spread wanted listed)
  tally <- go (Tally 0 0 0 0 0) entries
  pure
    ( "modules="
        <> show (tallyModules tally)
        <> " ok="
        <> show (tallyOk tally)
        <> " errored="
        <> show (tallyErrored tally)
        <> " imports="
        <> show (tallyImports tally)
        <> " decls="
        <> show (tallyDecls tally)
    )
  where
    go tally [] = pure tally
    go tally (entry : rest) = do
      (errors, source, parsed) <- parseEntry entry
      let tally' =
            Tally
              { tallyModules = tallyModules tally + 1,
                tallyOk = tallyOk tally + (if null errors then 1 else 0),
                tallyErrored = tallyErrored tally + (if null errors then 0 else 1),
                tallyImports = tallyImports tally + length (moduleImports parsed),
                tallyDecls = tallyDecls tally + length (moduleDecls parsed)
              }
      if report && not (null errors)
        then putStrLn (formatParseErrors (entryPath entry) (Just source) errors)
        else pure ()
      tally' `seq` go tally' rest

-- | Read, decode, parse and force one module. The extensions a build would
-- pass on the command line come from the entry; the module's own pragmas are
-- read by the parser, as they would be by any consumer.
parseEntry :: Entry -> IO ([(SourceSpan, T.Text)], T.Text, Module)
parseEntry entry = do
  bytes <- BS.readFile (entryPath entry)
  let source = TE.decodeUtf8Lenient bytes
      config =
        defaultConfig
          { parserSourceName = entryPath entry,
            parserExtensions = baseExtensions entry
          }
      (errors, parsed) = parseModule config source
  evaluate (rnf parsed `seq` rnf errors)
  pure (errors, source, parsed)

-- | What a build gives the parser before the module starts: the edition's
-- extensions with the package's @default-extensions@ applied over them.
-- Cabal's default edition is Haskell98.
baseExtensions :: Entry -> [Extension]
baseExtensions entry = foldr applyExtensionSetting editionExtensions settings
  where
    edition = fromMaybe Haskell98Edition (parseLanguageEdition (entryLanguage entry))
    editionExtensions = languageEditionExtensions edition
    settings = mapMaybe parseExtensionSettingName (entryExtensions entry)

-- | A module list: @package<tab>path<tab>language<tab>extensions@, the last
-- colon-separated, the path relative to the corpus root.
parseEntries :: FilePath -> BS.ByteString -> [Entry]
parseEntries corpus = foldr add [] . BS8.lines
  where
    add line rest = case BS8.split '\t' line of
      [_, path, language, extensions] | not (BS.null path) ->
        Entry
          { entryPath = corpus </> BS8.unpack path,
            entryLanguage = TE.decodeUtf8Lenient language,
            entryExtensions = map TE.decodeUtf8Lenient (filter (not . BS.null) (BS8.split ':' extensions))
          }
          : rest
      _ -> rest
