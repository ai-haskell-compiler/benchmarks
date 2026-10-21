{-# LANGUAGE BangPatterns #-}
{-# LANGUAGE PatternSynonyms #-}
{-# OPTIONS_GHC -Werror=incomplete-patterns #-}
module StructuralHash
  ( seed, mix, hashInt, hashInteger, hashText, hashList
  , hashNamespace, hashEntity, hashGlobal, hashResolution
  ) where

import Aihc.Parser.Syntax (pattern SourceSpan, TupleFlavor (..))
import Aihc.Resolve
import Data.Bits (xor)
import Data.Char (ord)
import qualified Data.List as List
import qualified Data.Text as T
import Data.Word (Word64)

-- A deterministic word-wise FNV-style digest, not a serialized text hash.
-- Tags and lengths distinguish constructors and field/list boundaries.
seed :: Word64
seed = 14695981039346656037

mix :: Word64 -> Word64 -> Word64
mix !h !word = (h `xor` word) * 1099511628211

hashInt :: Word64 -> Int -> Word64
hashInt h = mix h . fromIntegral

hashInteger :: Word64 -> Integer -> Word64
hashInteger h value = go (mix h (if value < 0 then 1 else 0)) (abs value)
  where
    go !acc 0 = mix acc 0
    go !acc n = let (q, r) = n `quotRem` 4294967296
                in go (mix (mix acc 1) (fromInteger r)) q

hashText :: Word64 -> T.Text -> Word64
hashText h text = T.foldl' (\acc c -> mix acc (fromIntegral (ord c))) (hashInt h (T.length text)) text

hashList :: (Word64 -> a -> Word64) -> Word64 -> [a] -> Word64
hashList f h xs = List.foldl' f (hashInt h (length xs)) xs

hashNamespace :: Word64 -> ResolutionNamespace -> Word64
hashNamespace h ns = mix h $ case ns of
  ResolutionNamespaceTerm -> 0
  ResolutionNamespaceType -> 1
  ResolutionNamespaceModule -> 2

hashGlobal :: Word64 -> GlobalName -> Word64
hashGlobal h (GlobalName name (PackageId package) modu namespace) =
  hashNamespace (hashText (hashText (hashText h name) package) modu) namespace

hashEntity :: Word64 -> Entity -> Word64
hashEntity h entity = case entity of
  EntityGlobal name -> hashGlobal (mix h 0) name
  EntityLocal (LocalId unique) -> hashInt (mix h 1) unique
  EntitySyntax -> mix h 2

hashResolution :: Word64 -> ResolutionAnnotation -> Word64
hashResolution h annotation =
  hashEntity
    (hashNamespace
      (identifier (spanHash h (resolutionSpan annotation)) (resolutionIdentifier annotation))
      (resolutionNamespace annotation))
    (resolutionTarget annotation)
  where
    identifier acc value = case value of
      IdentifierNamed text -> hashText (mix acc 0) text
      IdentifierList -> mix acc 1
      IdentifierTuple flavor arity -> hashInt (mix (mix acc 2) (case flavor of Boxed -> 0; Unboxed -> 1)) arity
    spanHash acc Nothing = mix acc 0
    spanHash acc (Just (SourceSpan file sl sc el ec so eo)) =
      List.foldl' hashInt (hashText (mix acc 1) file) [sl, sc, el, ec, so, eo]
