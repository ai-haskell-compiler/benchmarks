{-# LANGUAGE PatternSynonyms #-}
{-# OPTIONS_GHC -Werror=incomplete-patterns #-}
module TypeHash (hashBinding, hashType) where

import Aihc.Tc (TcBindingResult (..))
import Aihc.Tc.Types
import Data.Char (ord)
import Data.Word (Word64)
import StructuralHash

hashBinding :: Word64 -> TcBindingResult -> Word64
hashBinding h binding = hashType (hashText (hashEntity h (tbKey binding)) (tbDisplayName binding)) (tbType binding)

hashUnique :: Word64 -> Unique -> Word64
hashUnique h (Unique n) = hashInt h n

hashVar :: Word64 -> TyVarId -> Word64
hashVar h var = hashType (hashUnique (hashText h (tvName var)) (tvUnique var)) (tvKind var)

hashCon :: Word64 -> TyCon -> Word64
hashCon h con = hashInt (hashGlobal h (tyConKey con)) (tyConArity con)

-- Visit all fields, including invisible kinds, quantified predicates and
-- arbitrary-precision literals. Never render types or drop their payloads.
hashType :: Word64 -> TcType -> Word64
hashType h ty = case ty of
  TcTyVar var -> hashVar (mix h 0) var
  TcMetaTv unique -> hashUnique (mix h 1) unique
  TcTyCon con args -> hashList hashType (hashCon (mix h 2) con) args
  TcArrowTy -> mix h 3
  TcFunTy arg result -> hashType (hashType (mix h 4) arg) result
  TcForAllTy var body -> hashType (hashVar (mix h 5) var) body
  TcQualTy predicates body -> hashType (hashList hashPred (mix h 6) predicates) body
  TcAppTy fun arg -> hashType (hashType (mix h 7) fun) arg
  TcTyLit literal -> case literal of
    TyLitNat n -> hashInteger (mix (mix h 8) 0) n
    TyLitSymbol text -> hashText (mix (mix h 8) 1) text
    TyLitChar c -> hashInt (mix (mix h 8) 2) (ord c)
  TcKindedTyCon con args -> hashList hashType (hashCon (mix h 9) con) args

hashPred :: Word64 -> Pred -> Word64
hashPred h predicate = case predicate of
  ClassPred con args -> hashList hashType (hashCon (mix h 0) con) args
  EqPred left right -> hashType (hashType (mix h 1) left) right
  QuantifiedPred vars context body -> hashPred (hashList hashPred (hashList hashVar (mix h 2) vars) context) body
  IParamPred name ty -> hashType (hashText (mix h 3) name) ty
  IrredPred ty -> hashType (mix h 4) ty
