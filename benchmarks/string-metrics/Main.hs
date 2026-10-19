module Main where

import Data.Bits (shiftR, xor, (.&.))
import Data.List (foldl')
import Data.Maybe (fromMaybe)
import Data.Ratio (Ratio, denominator, numerator)
import qualified Data.Text as T
import Data.Text (Text)
import Data.Text.Metrics
  ( damerauLevenshtein,
    damerauLevenshteinNorm,
    hamming,
    jaccard,
    jaro,
    jaroWinkler,
    levenshtein,
    levenshteinNorm,
    overlap,
  )
import Data.Word (Word64)
import System.Environment (getArgs)
import System.Exit (die)

-- | A small, seedable linear congruential generator. Deterministic across
-- runs and platforms so the benchmark never depends on external data or
-- network access.
nextSeed :: Word64 -> Word64
nextSeed seed = seed * 6364136223846793005 + 1442695040888963407

-- | The high bits of a seed, which are the well-mixed ones.
draw :: Word64 -> Int
draw seed = fromIntegral (seed `shiftR` 33)

-- | The alphabet words are drawn from: sixteen ASCII letters plus four
-- characters outside ASCII, so the library's UTF-8 iteration sees code
-- points of one, two and three bytes.
alphabet :: Text
alphabet = T.pack ("abcdefghijklmnop" ++ "\233\248\955\8594")

-- | The lengths a word can have, in characters. Only a few distinct lengths,
-- so 'hamming' often sees equal-length pairs and returns a distance rather
-- than 'Nothing'.
wordLengths :: [Int]
wordLengths = [40, 48, 56, 64]

-- | A fresh word from the stream of seeds: a length and then one character
-- per seed.
freshWord :: Word64 -> (Text, Word64)
freshWord seed0 =
  let len = wordLengths !! (draw seed0 `mod` length wordLengths)
      go 0 seed acc = (T.pack (reverse acc), seed)
      go n seed acc =
        let c = T.index alphabet (draw seed `mod` T.length alphabet)
         in go (n - 1 :: Int) (nextSeed seed) (c : acc)
   in go len (nextSeed seed0) []

-- | A word edited a few times: substitutions, adjacent transpositions,
-- insertions and deletions, so that every metric sees inputs which are
-- neither identical nor unrelated.
editWord :: Text -> Word64 -> (Text, Word64)
editWord word seed0 =
  let edits = 1 + draw seed0 `mod` 6
      go 0 seed w = (w, seed)
      go n seed w =
        let seed' = nextSeed seed
            seed'' = nextSeed seed'
            len = T.length w
            pos = draw seed' `mod` max 1 len
            c = T.index alphabet (draw seed'' `mod` T.length alphabet)
            (before, after) = T.splitAt pos w
            w' = case draw seed `mod` 4 of
              0 | not (T.null after) -> before <> T.cons c (T.drop 1 after)
              1 | T.length after >= 2 ->
                before <> T.cons (T.index after 1) (T.cons (T.index after 0) (T.drop 2 after))
              2 -> before <> T.cons c after
              _ | not (T.null after) -> before <> T.drop 1 after
              _ -> w
         in go (n - 1 :: Int) (nextSeed seed'') w'
   in go edits (nextSeed seed0) word

-- | The pairs the metrics are taken over. Each pair is a fresh word and an
-- edit of it, or, every third pair, two fresh words. Every function the
-- library exports runs on every pair, so the input stays small: the
-- Levenshtein, Damerau-Levenshtein and Jaro distances are quadratic in the
-- word length, 'overlap' and 'jaccard' build a 'Data.Map.Strict.Map' per
-- word, and 'hamming' is linear.
pairs :: Int -> [(Text, Text)]
pairs count = go count 42
  where
    go 0 _ = []
    go n seed =
      let (a, seed') = freshWord seed
          (b, seed'') =
            if n `mod` 3 == 0
              then freshWord seed'
              else editWord a seed'
       in (a, b) : go (n - 1) seed''

-- | Fold a value into a running FNV-1a style checksum.
mix :: Word64 -> Int -> Word64
mix h x = (h `xor` fromIntegral x) * 1099511628211

-- | Fold a ratio into the checksum, numerator first.
ratio :: Word64 -> Ratio Int -> Word64
ratio h r = mix (mix h (numerator r)) (denominator r)

-- | Every metric of one pair, folded into the checksum.
metrics :: Word64 -> (Text, Text) -> Word64
metrics h0 (a, b) =
  let h1 = mix h0 (levenshtein a b)
      h2 = ratio h1 (levenshteinNorm a b)
      h3 = mix h2 (damerauLevenshtein a b)
      h4 = ratio h3 (damerauLevenshteinNorm a b)
      h5 = ratio h4 (overlap a b)
      h6 = ratio h5 (jaccard a b)
      h7 = mix h6 (fromMaybe (-1) (hamming a b))
      h8 = ratio h7 (jaro a b)
   in ratio h8 (jaroWinkler a b)

checksum :: Int -> Word64
checksum count = foldl' metrics 14695981039346656037 (pairs count)

-- | Pairs per backend and profile, and the checksum a correct build produces.
-- A miscompiled library fails the run rather than producing a number.
--
-- The runner passes the profile and the backend, and each pair of them
-- measures a different number of pairs. The counts are what take an AIHC
-- build about half a second on an Apple M4 Pro; native and LLVM builds share
-- the native counts. @--print@ prints the checksum of a new count.
work :: String -> String -> Maybe (Int, Word64)
work backend profile
  | backend `elem` ["native", "llvm"] = lookup profile [("O0", 30), ("Os", 109), ("O1", 37), ("O2", 5765)] >>= withChecksum
  | backend == "wasm" = lookup profile [("O0", 6), ("Os", 22), ("O1", 6), ("O2", 1348)] >>= withChecksum
  | otherwise = Nothing
  where
    withChecksum count = (,) count <$> lookup count checksums

-- | The checksum of each pair count 'work' uses.
checksums :: [(Int, Word64)]
checksums =
  [ (6, 10622944386971976380),
    (22, 14766389033292212708),
    (30, 17580702691337827161),
    (37, 16124759367494292225),
    (109, 10081844726478726807),
    (1348, 11986428750995824896),
    (5765, 6182484483643650695)
  ]

main :: IO ()
main = do
  args <- getArgs
  (count, expected) <- case args of
    [profile, backend] | Just found <- work backend profile -> pure found
    ["--print", count] -> do
      print (checksum (read count))
      die "printed"
    _ -> die "usage: string-metrics <O0|Os|O1|O2> <native|llvm|wasm> | --print <pairs>"
  putStrLn (if checksum count == expected then "ok" else "fail")
