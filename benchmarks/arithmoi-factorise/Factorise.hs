module Factorise (Case (..), checkCase) where

import Data.List (sortOn)
import Math.NumberTheory.Primes (factorise, nextPrime, unPrime)

-- | An integer with its complete factorisation, or a number with the prime that follows it.
data Case
  = Factor Integer [(Integer, Word)]
  | NextPrime Integer Integer

-- | Compare the complete result with the reference.
-- The order in which arithmoi reports the factors is not specified, so they are sorted first.
checkCase :: Case -> Bool
checkCase (Factor value expected) =
  sortOn fst [(unPrime prime, power) | (prime, power) <- factorise value] == expected
checkCase (NextPrime start expected) =
  unPrime (nextPrime start) == expected
