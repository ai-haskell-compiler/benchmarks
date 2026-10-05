-- | Compile MicroHs with itself and print a digest of the C it generates.
--
-- The program is the MicroHs compiler, built from the release vendored under
-- @microhs/@. The corpus directory (the flake's @microhs-corpus@ package)
-- holds the sources of the same release: @src@ and @mhs@, the compiler as
-- MicroHs builds it, and @lib@, its own Prelude and base library. The run is
-- what MicroHs's @Makefile@ does to regenerate @generated/mhs.c@: every module
-- of the compiler and its library is parsed, type-checked, desugared and
-- translated to combinators, and the combinators are written out as one C
-- array.
--
-- The sources are named relative to the corpus, as the @Makefile@ names them,
-- because MicroHs records source locations in the code it generates. The
-- printed line is the benchmark's expected output: the size of the generated
-- C and its FNV-1a hash, so a compiler that miscompiles MicroHs fails the run
-- rather than producing a number.
module Main (main) where

import Control.Exception (finally)
import Data.Bits (xor)
import qualified Data.ByteString as BS
import Data.Word (Word64)
import qualified MicroHs.Main as MicroHs
import Numeric (showHex)
import System.Directory (getTemporaryDirectory, makeAbsolute, removeFile, setCurrentDirectory)
import System.Environment (getArgs, withArgs)
import System.Exit (exitFailure)
import System.IO (hClose, hPutStrLn, openTempFile, stderr)

main :: IO ()
main = do
  arguments <- getArgs
  case arguments of
    [corpus] -> selfCompile corpus
    _ -> do
      hPutStrLn stderr "usage: microhs-self-compile <corpus directory>"
      exitFailure

selfCompile :: FilePath -> IO ()
selfCompile corpus = do
  temporary <- getTemporaryDirectory >>= makeAbsolute
  (output, handle) <- openTempFile temporary "microhs-self-compile.c"
  hClose handle
  generated <- compile output `finally` removeFile output
  putStrLn ("bytes=" ++ show (BS.length generated) ++ " fnv1a=" ++ hex (fnv1a generated))
  where
    compile output = do
      setCurrentDirectory corpus
      -- -q keeps MicroHs from warning, on stdout, that it has no mhs.conf
      -- next to its executable.
      withArgs ["-q", "-imhs", "-isrc", "-ilib", "MicroHs.Main", "-o", output] MicroHs.main
      BS.readFile output

-- | The 64-bit FNV-1a hash.
fnv1a :: BS.ByteString -> Word64
fnv1a = BS.foldl' step 0xcbf29ce484222325
  where
    step hash byte = (hash `xor` fromIntegral byte) * 0x100000001b3

hex :: Word64 -> String
hex value = replicate (16 - length digits) '0' ++ digits
  where
    digits = showHex value ""
