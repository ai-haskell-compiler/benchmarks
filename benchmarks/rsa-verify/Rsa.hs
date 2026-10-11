{-# LANGUAGE BangPatterns #-}

module Rsa (Vector (..), checkVector) where

import Data.List (foldl')

-- | The final two fields give the complete reference result and the verdict.
data Vector = Vector Int Integer Integer [Int] (Maybe Integer) Bool

-- | Use the same modular exponentiation algorithm with both compilers.
powMod :: Integer -> Int -> Integer -> Integer
powMod base exponent modulus = go base exponent 1
  where
    go !value !power !accumulator
      | power == 0 = accumulator
      | power == 1 = accumulator * value `rem` modulus
      | otherwise =
          let !next = value * value `rem` modulus
              !result = if odd power then accumulator * value `rem` modulus else accumulator
           in go next (power `quot` 2) result

-- | Encode a SHA-256 digest as a complete PKCS #1 v1.5 block.
-- RFC 8017, section 9.2: https://www.rfc-editor.org/rfc/rfc8017#section-9.2
encodedDigest :: Int -> [Int] -> Integer
encodedDigest width digest =
  foldl' (\value byte -> value * 256 + fromIntegral byte) 0 bytes
  where
    bytes = [0, 1] ++ replicate (width - 54) 255 ++ [0] ++ digestInfo ++ digest
    digestInfo = [48, 49, 48, 13, 6, 9, 96, 134, 72, 1, 101, 3, 4, 2, 1, 5, 0, 4, 32]

checkVector :: Vector -> Bool
checkVector (Vector width modulus signature digest expected verdict) =
  recovered == expected && accepted == verdict
  where
    recovered
      | signature < 0 || signature >= modulus = Nothing
      | otherwise = Just (powMod signature 65537 modulus)
    accepted = recovered == Just (encodedDigest width digest)
