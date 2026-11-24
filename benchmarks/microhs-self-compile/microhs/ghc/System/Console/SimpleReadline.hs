-- The benchmark replaces the haskeline version of this module. Only the
-- MicroHs REPL uses it, and the benchmark does not run the REPL. Without
-- haskeline, the plan has no terminfo flag, so the plan does not depend on
-- the flags of a lock file.
module System.Console.SimpleReadline(
  getInputLineHist,
  getInputLineHistComp,
  ) where
import System.IO

getInputLineHist :: FilePath -> String -> IO (Maybe String)
getInputLineHist _ prompt = do
  putStr prompt
  hFlush stdout
  eof <- isEOF
  if eof then return Nothing else Just <$> getLine

getInputLineHistComp :: ((String, String) -> IO [String]) -> FilePath -> String -> IO (Maybe String)
getInputLineHistComp _ = getInputLineHist
