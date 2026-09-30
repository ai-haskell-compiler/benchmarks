{-# LANGUAGE MagicHash #-}

module Main where

import GHC.Ptr (Ptr (..))
import System.Environment (getArgs)
import System.Exit (die)
import System.IO (hPutBuf, stdout)

fibonacci :: Int -> Integer -> Integer -> Integer
fibonacci count older newer =
  case count of
    0 -> older
    _ -> fibonacci (count - 1) newer (older + newer)

-- | Additions per profile.
--
-- The runner passes the profile name. Each count is the amount of work that
-- takes an AIHC native build between half a second and a second on an Apple
-- M1: about 0.76s at @-O0@ (184674), 0.73s at @-Os@ (200000), 0.77s at @-O1@
-- (186184) and 0.61s at @-O2@ (187500).
workSize :: String -> Maybe Int
workSize "O0" = Just 184674
workSize "Os" = Just 200000
workSize "O1" = Just 186184
workSize "O2" = Just 187500
workSize _ = Nothing

main :: IO ()
main = do
  args <- getArgs
  count <- case args of
    [profile] | Just size <- workSize profile -> pure size
    _ -> die "usage: integer-fibonacci <O0|Os|O1|O2>"
  if fibonacci count 0 1 > 0
    then hPutBuf stdout (Ptr "ok\n"# :: Ptr ()) 3
    else hPutBuf stdout (Ptr "fail\n"# :: Ptr ()) 5
