{-# LANGUAGE MagicHash #-}

module Main where

import GHC.Ptr (Ptr (..))
import System.Environment (getArgs)
import System.Exit (die)
import System.IO (hPutBuf, stdout)

factorial :: Int -> Integer -> Integer -> Integer
factorial count factor accumulator =
  case count of
    0 -> accumulator
    _ -> factorial (count - 1) (factor + 1) (accumulator * factor)

-- | Multiplications per profile.
--
-- The runner passes the profile name. Each count is the amount of work that
-- takes an AIHC native build between half a second and a second on an Apple
-- M1: about 0.71s at @-O0@ (45000), 0.76s at @-Os@ (48000), 0.66s at @-O1@
-- (43750) and 0.83s at @-O2@ (50000). @-O0@ and @-Os@ run this loop at nearly
-- the same speed, so their counts stay close and both stay inside the window.
workSize :: String -> Maybe Int
workSize "O0" = Just 45000
workSize "Os" = Just 48000
workSize "O1" = Just 43750
workSize "O2" = Just 50000
workSize _ = Nothing

main :: IO ()
main = do
  args <- getArgs
  count <- case args of
    [profile] | Just size <- workSize profile -> pure size
    _ -> die "usage: integer-factorial <O0|Os|O1|O2>"
  if factorial count 1 1 > 0
    then hPutBuf stdout (Ptr "ok\n"# :: Ptr ()) 3
    else hPutBuf stdout (Ptr "fail\n"# :: Ptr ()) 5
