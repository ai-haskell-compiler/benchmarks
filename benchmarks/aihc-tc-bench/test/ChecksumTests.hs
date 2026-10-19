{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE PatternSynonyms #-}
module Main (main) where

import Aihc.Parser.Syntax (pattern SourceSpan, TupleFlavor (..))
import Aihc.Resolve
import Aihc.Tc.Types
import Control.Exception (SomeException, evaluate, try)
import Control.Monad (unless)
import Data.List (nub)
import Data.Word (Word64)
import StructuralHash
import TypeHash

distinct :: String -> [Word64] -> IO ()
distinct label values = unless (length (nub values) == length values) (error label)

mustForce :: String -> Word64 -> IO ()
mustForce label value = do
  outcome <- try (evaluate value) :: IO (Either SomeException Word64)
  case outcome of
    Left _ -> pure ()
    Right _ -> error (label ++ " was not forced")

main :: IO ()
main = do
  let con = mkTyConWithOrigin (PackageId "p") "M" "T" 1
      var = mkTyVarId "a" (Unique 7) TcArrowTy
      base = TcTyCon con []
      types = [ TcTyVar var, TcMetaTv (Unique 7), base, TcArrowTy
              , TcFunTy base base, TcForAllTy var base
              , TcQualTy [ClassPred con [base]] base, TcAppTy base base
              , TcTyLit (TyLitNat 7), TcTyLit (TyLitSymbol "7")
              , TcTyLit (TyLitChar '7'), TcKindedTyCon con [base] ]
      annotation = ResolutionAnnotation Nothing (IdentifierNamed "x") ResolutionNamespaceTerm EntitySyntax
      global = GlobalName "x" (PackageId "p") "M" ResolutionNamespaceTerm
  distinct "type constructors/literals" (map (hashType seed) types)
  distinct "predicate constructors" [hashType seed (TcQualTy [p] base) | p <-
    [ClassPred con [base], EqPred base base, QuantifiedPred [var] [] (EqPred base base), IParamPred "?x" base, IrredPred base]]
  distinct "type payloads" (map (hashType seed)
    [ TcTyCon con [base], TcTyCon con [TcArrowTy]
    , TcTyCon (mkTyConWithOrigin (PackageId "q") "M" "T" 1) [base]
    , TcTyCon (mkTyConWithOrigin (PackageId "p") "N" "T" 1) [base]
    , TcTyCon (mkTyConWithOrigin (PackageId "p") "M" "T" 2) [base]
    , TcTyVar var, TcTyVar (mkTyVarId "a" (Unique 7) base)
    , TcTyVar (mkTyVarId "a" (Unique 8) TcArrowTy) ])
  distinct "unbounded integer literals" (map (hashInteger seed) [0, 1, -1, 2^(100 :: Int), 2^(100 :: Int)+1, -(2^(100 :: Int))])
  distinct "text boundaries" [hashText (hashText seed "ab") "c", hashText (hashText seed "a") "bc"]
  distinct "resolution payloads" (map (hashResolution seed)
    [ annotation
    , annotation {resolutionSpan = Just (SourceSpan "M.hs" 1 1 1 2 0 1)}
    , annotation {resolutionSpan = Just (SourceSpan "M.hs" 1 1 1 2 0 2)}
    , annotation {resolutionIdentifier = IdentifierTuple Boxed 2}
    , annotation {resolutionIdentifier = IdentifierTuple Unboxed 2}
    , annotation {resolutionIdentifier = IdentifierList}
    , annotation {resolutionNamespace = ResolutionNamespaceType}
    , annotation {resolutionTarget = EntityLocal (LocalId 7)}
    , annotation {resolutionTarget = EntityGlobal global}
    , annotation {resolutionTarget = EntityGlobal (global {globalNamePackage = PackageId "q"})} ])
  mustForce "nested type" (hashType seed (TcTyCon con [TcFunTy base (error "type payload")]))
  mustForce "invisible variable kind" (hashType seed (TcTyVar (mkTyVarId "a" (Unique 7) (TcTyCon con [error "kind payload"]))))
  mustForce "quantified predicate" (hashType seed (TcQualTy [QuantifiedPred [var] [] (IrredPred (TcTyCon con [error "predicate payload"]))] base))
  mustForce "resolution target" (hashResolution seed (annotation {resolutionTarget = EntityGlobal (error "target payload")}))
  putStrLn "Structural checksum checks passed"
