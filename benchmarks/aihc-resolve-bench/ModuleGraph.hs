-- | Dependency-first SCC scheduling for the modules of one package.
-- Imports outside the package are supplied by previously processed packages.
module ModuleGraph (Unit (..), dependencyUnits) where

import Data.Graph (SCC (..), stronglyConnComp)
import Data.List (sortOn)
import qualified Data.Map.Strict as Map
import qualified Data.Set as Set

data Unit key a = Unit
  { unitId :: Int
  , unitDependencies :: [Int]
  , unitMembers :: [(key, a)]
  , unitIsCyclic :: Bool
  } deriving (Eq, Show)

dependencyUnits :: Ord key => [(key, [key], a)] -> Either String [Unit key a]
dependencyUnits inputs
  | Map.size byKey /= length inputs = Left "duplicate module in package graph"
  | otherwise = Right (zipWith makeUnit [0 ..] components)
  where
    byKey = Map.fromList [(key, (dependencies, value)) | (key, dependencies, value) <- inputs]
    nodes = [((key, value), key, dependencies) | (key, (dependencies, value)) <- Map.toAscList byKey]
    components = stronglyConnComp nodes
    members (AcyclicSCC value) = [value]
    members (CyclicSCC values) = sortOn fst values
    owners = Map.fromList [(key, number) | (number, component) <- zip [0 ..] components, (key, _) <- members component]
    makeUnit number component = Unit number dependencies (members component) cyclic
      where
        cyclic = case component of
          AcyclicSCC _ -> False
          CyclicSCC _ -> True
        dependencies = Set.toAscList (Set.fromList
          [ owner
          | (key, _) <- members component
          , dependency <- fst (byKey Map.! key)
          , Just owner <- [Map.lookup dependency owners]
          , owner /= number
          ])
