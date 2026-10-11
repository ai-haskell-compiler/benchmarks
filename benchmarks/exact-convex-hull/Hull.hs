{-# LANGUAGE BangPatterns #-}

module Hull (Point, convexHull) where

import Data.List (foldl', group, sort)

type Point = (Integer, Integer)

cross :: Point -> Point -> Point -> Integer
cross (ax, ay) (bx, by) (cx, cy) =
  (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)

-- | Return counterclockwise vertices from the least point.
-- Remove duplicate points and interior points on each straight edge.
convexHull :: [Point] -> [Point]
convexHull points =
  case unique of
    [] -> []
    [point] -> [point]
    _ -> init (half unique) ++ init (half (reverse unique))
  where
    unique = [point | point : _ <- group (sort points)]
    half = reverse . foldl' add []
    add (b : a : rest) !point
      | cross a b point <= 0 = add (a : rest) point
    add stack !point = point : stack
