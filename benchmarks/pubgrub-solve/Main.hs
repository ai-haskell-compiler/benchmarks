module Main where

import Data.Bits (shiftR, xor)
import qualified Data.IntMap.Strict as IM
import qualified Data.IntSet as IS
import Data.List (foldl')
import Data.Word (Word64)
import PubGrub
import System.Environment (getArgs)
import System.Exit (die)
import Text.Read (readMaybe)

-- | A deterministic pseudo-random number in [0, 1000000) for a seed and
-- three coordinates. The arithmetic is in Word64 because GHC's Wasm target
-- has a 32-bit Int.
random :: Int -> Int -> Int -> Int -> Int
random seed a b c =
  let w = fromIntegral :: Int -> Word64
      x = w seed * 1000003 + w a * 7919 + w b * 104729 + w c * 15485863
   in fromIntegral (((x * 6364136223846793005 + 1442695040888963407) `shiftR` 33) `mod` 1000000)

-- | N queens as a registry: row @r@ picks a column as its version, and that
-- version depends on its column and both diagonals each recording row @r@
-- as theirs. The root bans one column per row, chosen from the seed, so
-- some instances are solvable and some are not.
queens :: Int -> Int -> Problem
queens n seed = Problem (IM.fromList (rootEntry : rows ++ lines')) deps
  where
    rootEntry = (root, IS.fromList [0, 1])
    rows = [(r, IS.fromList [0 .. n]) | r <- [1 .. n]]
    lines' = [(k, IS.fromList [0 .. n]) | k <- [column c | c <- [1 .. n]] ++ [rising d | d <- [2 .. 2 * n]] ++ [falling d | d <- [1 .. 2 * n - 1]]]
    column c = 100 + c
    rising d = 200 + d
    falling d = 300 + d
    banned r = 1 + random seed 1 r 0 `mod` n
    deps k v
      | k == root = [(r, IS.delete (banned r) (IS.fromList [1 .. n])) | r <- [1 .. n]]
      | k <= n = [(column v, IS.singleton k), (rising (k + v), IS.singleton k), (falling (k - v + n), IS.singleton k)]
      | otherwise = []

-- | Pigeonhole: @holes + 1@ pigeons each pick a hole, and a hole records its
-- occupant. Each pigeon may use a seeded subset of the holes, which varies
-- the instance but keeps it unsolvable.
pigeonhole :: Int -> Int -> Problem
pigeonhole holes seed = Problem (IM.fromList ((root, IS.fromList [0, 1]) : pigeons ++ holeEntries)) deps
  where
    count = holes + 1
    pigeons = [(j, IS.insert 0 (choices j)) | j <- [1 .. count]]
    holeEntries = [(hole h, IS.fromList [0 .. count]) | h <- [1 .. holes]]
    -- At least two holes per pigeon, so propagation alone cannot decide it.
    choices j = IS.fromList (take (max 2 (length kept)) (kept ++ [h | h <- [1 .. holes], h `notElem` kept]))
      where
        kept = [h | h <- [1 .. holes], random seed 2 j h `mod` 4 /= 0]
    hole h = 1000 + h
    deps k v
      | k == root = [(j, choices j) | j <- [1 .. count]]
      | k <= count = [(hole v, IS.singleton k)]
      | otherwise = []

-- | A registry of 25 packages with 6 versions each, generated from a seed.
-- Every version depends on up to three later packages within a version
-- range, so the graph is acyclic but ranges conflict often enough that the
-- solver backtracks.
registry :: Int -> Problem
registry seed = Problem (IM.fromList ((root, IS.fromList [0, 1]) : [(k, IS.fromList [0 .. versions]) | k <- [1 .. packages]])) deps
  where
    packages = 25
    versions = 6
    range k v j =
      let lo = 1 + random seed k v (10 + j) `mod` versions
          hi = lo + 1 + random seed k v (20 + j) `mod` 4
       in IS.fromList [lo .. min versions hi]
    deps k v
      | k == root = [(1 + random seed 0 0 j `mod` 8, range 0 0 j) | j <- [1 .. 3]]
      | otherwise =
          [ (q, range k v j)
            | j <- [1 .. random seed k v 0 `mod` 4],
              let q = k + 1 + random seed k v j `mod` 6,
              q <= packages
          ]

-- | The @i@th instance of the sequence: queens, two registries and a
-- pigeonhole problem in turn, each family seeded by its own counter, so the
-- sequence never repeats a problem.
instance' :: Int -> Problem
instance' i = case i `mod` 4 of
  0 -> queens (5 + j `mod` 3) j
  3 -> pigeonhole (3 + j `mod` 2) j
  _ -> registry (2 * j + i `mod` 4)
  where
    j = i `div` 4

-- | Check a solution against every dependency of every selected version.
valid :: Problem -> IM.IntMap Int -> Bool
valid p selection =
  IM.findWithDefault 0 root selection == 1
    && and
      [ IS.member (IM.findWithDefault 0 q selection) r
        | (k, v) <- IM.toList selection,
          v /= 0,
          (q, r) <- dependencies p k v
      ]

mix :: Word64 -> Int -> Word64
mix h x = (h `xor` fromIntegral x) * 1099511628211

-- | Solve the first @count@ instances, check every solution, and hash the
-- outcomes, conflict counts and selected versions.
checksum :: Int -> Either String Word64
checksum count = foldl' step (Right 14695981039346656037) [0 .. count - 1]
  where
    step acc i = do
      h <- acc
      let p = instance' i
      case solve p of
        Solved selection conflicts
          | valid p selection -> Right (IM.foldlWithKey' (\a k v -> mix (mix a k) v) (mix (mix h 1) conflicts) selection)
          | otherwise -> Left ("invalid solution for instance " ++ show i)
        Unsolvable conflicts -> Right (mix (mix h 0) conflicts)

-- | Instances per profile and backend, calibrated on an Apple M4 Pro for
-- about half a second of AIHC. The GHC checksums reject wrong output.
-- AIHC could not build the Os profile when this was calibrated (a call
-- arity mismatch in Lir generation), so Os reuses the O1 counts until it can.
-- See docs/library-benchmarks-calibration.json for measurements.
work :: String -> String -> Maybe (Int, Word64)
work backend profile
  | backend == "native" = lookup profile native >>= withChecksum
  | backend == "llvm" = lookup profile llvm >>= withChecksum
  | backend == "wasm" = lookup profile wasm >>= withChecksum
  | otherwise = Nothing
  where
    native = [("O0", 84), ("O1", 1820), ("O2", 2385), ("Os", 1820)]
    llvm = [("O0", 21), ("O1", 1802), ("O2", 2413), ("Os", 1802)]
    wasm = [("O0", 16), ("O1", 590), ("O2", 873), ("Os", 590)]
    withChecksum n = (,) n <$> lookup n checksums

checksums :: [(Int, Word64)]
checksums =
  [ (16, 1286861817638841957),
    (21, 14314783863564026284),
    (84, 1814792474558324814),
    (590, 13963330651850972121),
    (873, 17626551918583531882),
    (1802, 12359792640443182431),
    (1820, 2928145655469896954),
    (2385, 4322252904419971611),
    (2413, 4545819611987932962)
  ]

main :: IO ()
main = do
  args <- getArgs
  case args of
    ["--print", value] | Just n <- readMaybe value, n > 0 -> either die print (checksum n)
    ["--stats", value] | Just n <- readMaybe value, n > 0 -> mapM_ (print . stats) [0 .. n - 1]
    [profile, backend] | Just (n, expected) <- work backend profile ->
      if checksum n == Right expected then putStrLn "ok" else die "checksum mismatch"
    _ -> die "usage: pubgrub-solve <O0|O1|O2|Os> <native|llvm|wasm> | --print <positive instance count> | --stats <positive instance count>"
  where
    stats i = case solve (instance' i) of
      Solved _ conflicts -> (i, True, conflicts)
      Unsolvable conflicts -> (i, False, conflicts)
