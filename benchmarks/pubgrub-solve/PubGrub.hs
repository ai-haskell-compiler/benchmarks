{-# LANGUAGE BangPatterns #-}

-- | A PubGrub dependency solver (Natalie Weizenbaum, 2018) over finite
-- version universes.
--
-- Each package's universe holds its versions plus 0, which stands for "not
-- selected". A term is then just the set of values it allows: the positive
-- term @foo ∈ S@ is @S@ and the negative term @not (foo ∈ S)@ is the
-- complement of @S@, which includes 0. Satisfaction, contradiction and the
-- resolution step of conflict analysis all become set operations.
module PubGrub
  ( Pkg,
    VSet,
    Problem (..),
    Outcome (..),
    root,
    solve,
  )
where

import qualified Data.IntMap.Strict as IM
import qualified Data.IntSet as IS
import Data.List (foldl')
import qualified Data.Map.Strict as M

type Pkg = Int

type VSet = IS.IntSet

-- | A problem: the universe of every package (versions plus 0) and the
-- dependencies of each version, as (package, allowed versions) pairs.
-- Package 'root' with universe {0, 1} must be selected.
data Problem = Problem
  { universe :: IM.IntMap VSet,
    dependencies :: Pkg -> Int -> [(Pkg, VSet)]
  }

data Outcome
  = -- | The selected version of every package, 0 for packages left out.
    Solved (IM.IntMap Int) !Int
  | -- | No solution; the number of conflicts resolved to prove it.
    Unsolvable !Int

root :: Pkg
root = 0

-- | Not all of these terms may hold at once.
type Incompat = M.Map Pkg VSet

data Assignment = Assignment
  { aPkg :: !Pkg,
    aSet :: !VSet,
    aLevel :: !Int,
    -- | The incompatibility it was derived from; Nothing for a decision.
    aCause :: !(Maybe Int)
  }

data State = State
  { -- | Newest first.
    assignments :: [Assignment],
    -- | The intersection of every assignment to a package.
    current :: IM.IntMap VSet,
    level :: !Int,
    incompats :: IM.IntMap Incompat,
    byPackage :: IM.IntMap [Int],
    nextId :: !Int,
    conflictCount :: !Int
  }

universeOf :: Problem -> Pkg -> VSet
universeOf p k = IM.findWithDefault IS.empty k (universe p)

allowed :: Problem -> State -> Pkg -> VSet
allowed p st k = IM.findWithDefault (universeOf p k) k (current st)

assign :: Problem -> Assignment -> State -> State
assign p a st =
  st
    { assignments = a : assignments st,
      current = IM.insert (aPkg a) (IS.intersection (allowed p st (aPkg a)) (aSet a)) (current st)
    }

addIncompat :: Incompat -> State -> (Int, State)
addIncompat inc st =
  let i = nextId st
   in ( i,
        st
          { incompats = IM.insert i inc (incompats st),
            byPackage = foldl' (\m k -> IM.insertWith (++) k [i] m) (byPackage st) (M.keys inc),
            nextId = i + 1
          }
      )

data Relation = Satisfied | AlmostSatisfied !Pkg | Contradicted | Inconclusive

relation :: Problem -> State -> Incompat -> Relation
relation p st = go Nothing . M.toList
  where
    go open [] = maybe Satisfied AlmostSatisfied open
    go open ((k, t) : rest)
      | cur `IS.isSubsetOf` t = go open rest
      | IS.disjoint cur t = Contradicted
      | Nothing <- open = go (Just k) rest
      | any (\(k', t') -> IS.disjoint (allowed p st k') t') rest = Contradicted
      | otherwise = Inconclusive
      where
        cur = allowed p st k

-- | Derive the negation of the one unsatisfied term of an incompatibility.
derive :: Problem -> Pkg -> Int -> State -> State
derive p k i st =
  let t = incompats st IM.! i M.! k
   in assign p (Assignment k (IS.difference (universeOf p k) t) (level st) (Just i)) st

-- | Unit propagation, starting from the packages whose assignments changed.
propagate :: Problem -> [Pkg] -> State -> Either Int State
propagate _ [] st = Right st
propagate p (k : changed) st0 = scan (IM.findWithDefault [] k (byPackage st0)) changed st0
  where
    scan [] queue st = propagate p queue st
    scan (i : is) queue st = case relation p st (incompats st IM.! i) of
      Satisfied -> do
        (learned, st') <- resolve p i st
        case relation p st' (incompats st' IM.! learned) of
          AlmostSatisfied q -> propagate p [q] (derive p q learned st')
          _ -> error "a learned incompatibility must be almost satisfied"
      AlmostSatisfied q -> scan is (q : queue) (derive p q i st)
      _ -> scan is queue st

-- | Conflict resolution. Returns the incompatibility to propagate from, after
-- backjumping, or the conflict count once the root itself is ruled out.
resolve :: Problem -> Int -> State -> Either Int (Int, State)
resolve p i0 st0 = loop (incompats st0 IM.! i0) True st0 {conflictCount = conflictCount st0 + 1}
  where
    loop inc original s
      | M.null inc || (M.keys inc == [root] && IS.member 1 (inc M.! root)) = Left (conflictCount s)
      | otherwise =
          let chronological = reverse (assignments s)
              -- The length of the shortest prefix that satisfies a term,
              -- optionally starting from an extra assignment.
              prefixFor k t start = go 0 start chronological
                where
                  go !n cur xs
                    | cur `IS.isSubsetOf` t = n
                    | (a : rest) <- xs = go (n + 1) (if aPkg a == k then IS.intersection cur (aSet a) else cur) rest
                    | otherwise = n
              prefixes = [(prefixFor k t (universeOf p k), k) | (k, t) <- M.toList inc]
              (satisfierIndex, package) = maximum prefixes
              satisfier = chronological !! (satisfierIndex - 1)
              term = inc M.! package
              -- The satisfier's term may already have been satisfied in part
              -- before the satisfier; find where, given the satisfier itself.
              before = take (satisfierIndex - 1) chronological
              partial = go 0 (IS.intersection (universeOf p package) (aSet satisfier)) before
                where
                  go !n cur xs
                    | cur `IS.isSubsetOf` term = n
                    | (a : rest) <- xs = go (n + 1) (if aPkg a == package then IS.intersection cur (aSet a) else cur) rest
                    | otherwise = n
              previous = maximum (partial : [n | (n, k) <- prefixes, k /= package])
              previousLevel = if previous == 0 then 0 else aLevel (chronological !! (previous - 1))
           in case aCause satisfier of
                Just c
                  | previousLevel == aLevel satisfier ->
                      -- Resolve on the satisfier's package: the remaining
                      -- terms of both, with the package's terms united.
                      let cause = incompats s IM.! c
                          merged =
                            M.insert package (IS.union term (cause M.! package)) $
                              M.unionWith IS.intersection (M.delete package inc) (M.delete package cause)
                          learned = M.filterWithKey (\k t -> t /= universeOf p k) merged
                       in loop learned False s
                _ ->
                  let kept = filter (\a -> aLevel a <= previousLevel) (assignments s)
                      rebuilt = foldr (assign p) s {assignments = [], current = IM.empty} kept
                      s' = rebuilt {level = previousLevel}
                   in if original
                        then Right (i0, s')
                        else Right (addIncompat inc s')

-- | Solve a problem, or prove that it has no solution.
solve :: Problem -> Outcome
solve p = either Unsolvable finish (go [root] start)
  where
    (_, start) = addIncompat (M.singleton root (IS.singleton 0)) (State [] IM.empty 0 IM.empty IM.empty 0 0)
    go changed st = do
      st' <- propagate p changed st
      let decided = IS.fromList [aPkg a | a <- assignments st', Nothing <- [aCause a]]
          candidates =
            [ (IS.size s, k)
              | k <- IM.keys (universe p),
                not (IS.member k decided),
                let s = allowed p st' k,
                not (IS.member 0 s)
            ]
      case candidates of
        [] -> Right st'
        _ ->
          let k = snd (minimum candidates)
              v = IS.findMax (allowed p st' k)
              addDependency s (q, r) = snd (addIncompat (M.fromList [(k, IS.singleton v), (q, IS.difference (universeOf p q) r)]) s)
              withDependencies = foldl' addDependency st' (dependencies p k v)
              next = level withDependencies + 1
           in go [k] (assign p (Assignment k (IS.singleton v) next Nothing) withDependencies {level = next})
    finish st = Solved (IM.mapWithKey (\k _ -> selected k) (universe p)) (conflictCount st)
      where
        selected k
          | [v] <- IS.toList (allowed p st k), v /= 0 = v
          | IS.member 0 (allowed p st k) = 0
          | otherwise = error "an undecided package must allow being left out"
