{-# LANGUAGE BangPatterns #-}
module PhaseTiming (Timings, newTimings, timed, reportTimings) where

import Control.Exception (evaluate)
import Data.IORef (IORef, modifyIORef', newIORef, readIORef)
import Data.List (intercalate)
import qualified Data.Map.Strict as Map
import Data.Word (Word64)
import GHC.Clock (getMonotonicTimeNSec)
import System.Environment (lookupEnv)
import System.IO (hPutStrLn, stderr)

data Total = Total !Word64 !Int
data Timings = Disabled | Enabled !Word64 !(IORef (Map.Map String Total))

newTimings :: IO Timings
newTimings = do
  enabled <- lookupEnv "AIHC_BENCH_PHASE_TIMINGS"
  case enabled of
    Just "1" -> do
      start <- getMonotonicTimeNSec
      totals <- newIORef Map.empty
      pure (Enabled start totals)
    _ -> pure Disabled

-- Actions must force the results they produce before returning. Timed
-- actions never nest, so the reported phases are disjoint wall intervals.
-- The callback also defers construction of the IO action: a pure case or
-- strict let that selects an action must run after the start clock too.
{-# NOINLINE timed #-}
timed :: Timings -> String -> (() -> IO a) -> IO a
timed Disabled _ action = action ()
timed (Enabled _ totals) phase action = do
  start <- getMonotonicTimeNSec
  constructed <- evaluate (action ())
  value <- constructed
  end <- getMonotonicTimeNSec
  let !elapsed = end - start
      add (Total left n) (Total right m) = Total (left + right) (n + m)
  modifyIORef' totals (Map.insertWith add phase (Total elapsed 1))
  pure value

reportTimings :: Timings -> IO ()
reportTimings Disabled = pure ()
reportTimings (Enabled start totals) = do
  end <- getMonotonicTimeNSec
  entries <- Map.toAscList <$> readIORef totals
  let total = end - start
      accounted = sum [ns | (_, Total ns _) <- entries]
      phases = intercalate "," [show name ++ ":" ++ show ns | (name, Total ns _) <- entries]
      calls = intercalate "," [show name ++ ":" ++ show n | (name, Total _ n) <- entries]
  if accounted > total then error "overlapping phase timing intervals" else pure ()
  hPutStrLn stderr ("{\"total_ns\":" ++ show total
    ++ ",\"unattributed_ns\":" ++ show (total - accounted)
    ++ ",\"phases_ns\":{" ++ phases ++ "},\"calls\":{" ++ calls ++ "}}")
