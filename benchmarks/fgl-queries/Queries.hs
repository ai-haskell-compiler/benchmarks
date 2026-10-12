module Queries
  ( Graph,
    buildGraph,
    shortestPathSum,
    levelSum,
    dominatorSum,
    componentSum,
    flowValue,
  )
where

import Data.Graph.Inductive.Graph (LPath (..), Node, delNode, mkGraph)
import Data.Graph.Inductive.PatriciaTree (Gr)
import Data.Graph.Inductive.Query.BFS (level)
import Data.Graph.Inductive.Query.DFS (scc)
import Data.Graph.Inductive.Query.Dominators (iDom)
import Data.Graph.Inductive.Query.MaxFlow (maxFlow)
import Data.Graph.Inductive.Query.SP (spTree)

type Graph = Gr () Int

buildGraph :: Int -> [(Int, Int, Int)] -> Graph
buildGraph count edges = mkGraph [(node, ()) | node <- [0 .. count - 1]] edges

-- | Sum (node + 1) * distance over the shortest path tree from a source.
shortestPathSum :: Graph -> Node -> Int
shortestPathSum graph source =
  sum [(node + 1) * distance | LP ((node, distance) : _) <- spTree source graph]

-- | Sum (node + 1) * depth over the breadth-first levels from a source.
levelSum :: Graph -> Node -> Int
levelSum graph source = sum [(node + 1) * depth | (node, depth) <- level source graph]

-- | Sum (node + 1) * (immediate dominator + 1) over the nodes a root reaches.
dominatorSum :: Graph -> Node -> Int
dominatorSum graph root = sum [(node + 1) * (dominator + 1) | (node, dominator) <- iDom graph root]

-- | Count the strongly connected components of the graph without one node,
-- plus the sum of size * (least node + 1) over the components.
componentSum :: Graph -> Node -> Int
componentSum graph removed =
  length groups + sum [length group * (minimum group + 1) | group <- groups]
  where
    groups = scc (delNode removed graph)

-- | The maximum flow between a source and a sink.
flowValue :: Graph -> (Node, Node) -> Int
flowValue graph (source, sink) = maxFlow graph source sink
