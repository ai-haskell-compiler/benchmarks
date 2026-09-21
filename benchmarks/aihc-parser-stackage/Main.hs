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
-- started, which is what a compiler front end does with a parse tree. The
-- printed line is the benchmark's expected output: the module count, how
-- many parsed without an error, and the number of imports and declarations
-- across every tree. A compiler that miscompiles aihc-parser changes one of
-- those numbers.
--
-- With @--report@ the whole corpus (@modules.tsv@, not the benchmark's
-- sample) is swept and every parse error is printed, which is how the
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
import System.Exit (exitFailure)
import System.FilePath ((</>))
import System.IO (hPutStrLn, stderr)

main :: IO ()
main = do
  arguments <- getArgs
  case arguments of
    [corpus] -> sweep corpus False
    [corpus, "--report"] -> sweep corpus True
    _ -> do
      hPutStrLn stderr "usage: aihc-parser-stackage <corpus directory> [--report]"
      exitFailure

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

sweep :: FilePath -> Bool -> IO ()
sweep corpus report = do
  entries <- parseEntries corpus <$> BS.readFile (corpus </> (if report then "modules.tsv" else "benchmark.tsv"))
  tally <- go (Tally 0 0 0 0 0) entries
  putStrLn
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
