{-# LANGUAGE BangPatterns #-}
module Main where

import Data.Bits (xor)
import Data.Char (ord)
import Data.List (foldl')
import Data.Word (Word64)
import Happy.Backend.LALR.ProduceCode (produceParser)
import Happy.Frontend (parseYFileContents)
import Happy.Frontend.AbsSyn (BookendedAbsSyn (..))
import Happy.Frontend.Mangler (mangler)
import Happy.Tabular (Tables (..), genTables, select_first_reduction)
import System.Environment (getArgs)
import System.Exit (die)
import Text.Read (readMaybe)

-- | A grammar with @n@ statement forms. Each form has its own five-level
-- expression language over shared tokens, so the LR(0) automaton, the
-- lookahead propagation and the generated tables all grow with @n@, and no
-- two families are the same nonterminals.
grammar :: Int -> String
grammar n =
  unlines $
    [ "{",
      "module Parser where",
      "}",
      "%name parse",
      "%tokentype { Tok }",
      "%error { parseError }",
      "%token",
      "  num { TNum }",
      "  id { TId }",
      "  '+' { TPlus }",
      "  '-' { TMinus }",
      "  '*' { TStar }",
      "  '/' { TSlash }",
      "  '(' { TOpen }",
      "  ')' { TClose }",
      "  ';' { TSemi }",
      "  '=' { TEq }",
      "  ',' { TComma }"
    ]
      ++ ["  kw" ++ show i ++ " { TKw " ++ show i ++ " }" | i <- [1 .. n]]
      ++ [ "%%",
           "prog : stmts { $1 }",
           "stmts : stmt { [$1] } | stmts ';' stmt { $3 : $1 }",
           "stmt : " ++ alternatives ["s" ++ show i ++ " { $1 }" | i <- [1 .. n]]
         ]
      ++ concatMap family [1 .. n]
  where
    alternatives = foldr1 (\a b -> a ++ " | " ++ b)
    family i =
      let k = show i
          nt x = x ++ k
       in [ nt "s" ++ " : kw" ++ k ++ " id '=' " ++ nt "e" ++ " { $4 } | kw" ++ k ++ " " ++ nt "args" ++ " { 0 }",
            nt "args" ++ " : " ++ nt "e" ++ " { [$1] } | " ++ nt "args" ++ " ',' " ++ nt "e" ++ " { $3 : $1 }",
            nt "e" ++ " : " ++ nt "e" ++ " '+' " ++ nt "t" ++ " { $1 + $3 } | " ++ nt "e" ++ " '-' " ++ nt "t" ++ " { $1 - $3 } | " ++ nt "t" ++ " { $1 }",
            nt "t" ++ " : " ++ nt "t" ++ " '*' " ++ nt "u" ++ " { $1 * $3 } | " ++ nt "t" ++ " '/' " ++ nt "u" ++ " { $1 } | " ++ nt "u" ++ " { $1 }",
            nt "u" ++ " : '-' " ++ nt "u" ++ " { negate $2 } | " ++ nt "a" ++ " { $1 }",
            nt "a" ++ " : num { 1 } | id { 2 } | id '(' " ++ nt "args" ++ " ')' { 3 } | '(' " ++ nt "e" ++ " ')' { $2 }"
          ]

mix :: Word64 -> Int -> Word64
mix h x = (h `xor` fromIntegral x) * 1099511628211

-- | Run happy's pipeline (parse the .y source, mangle, build the LALR tables
-- and produce the parser module) and hash every character of the output.
checksum :: Int -> Either String Word64
checksum n = do
  BookendedAbsSyn _ header abssyn trailer <- parseYFileContents (grammar n)
  (g, attributes, directives) <- either (Left . unlines) Right (mangler "bench.y" abssyn)
  let tables = genTables select_first_reduction g
      (_, (shiftReduce, reduceReduce)) = conflicts tables
      parser = produceParser g attributes directives (actionTable tables) (gotoTable tables) [] header trailer False False
      !h = foldl' (\acc c -> mix acc (ord c)) 14695981039346656037 parser
  pure (mix (mix (mix h (length (lr0items tables))) shiftReduce) reduceReduce)

-- | Grammar families per profile and backend, calibrated on an Apple M4 Pro
-- for about half a second of AIHC. The GHC checksums reject wrong output.
-- See docs/library-benchmarks-calibration.json for measurements.
work :: String -> String -> Maybe (Int, Word64)
work backend profile
  | backend == "native" = lookup profile native >>= withChecksum
  | backend == "llvm" = lookup profile llvm >>= withChecksum
  | backend == "wasm" = lookup profile wasm >>= withChecksum
  | otherwise = Nothing
  where
    native = [("O0", 20), ("O1", 70), ("O2", 86), ("Os", 35)]
    llvm = [("O0", 10), ("O1", 69), ("O2", 75), ("Os", 35)]
    wasm = [("O0", 8), ("O1", 35), ("O2", 42), ("Os", 16)]
    withChecksum n = (,) n <$> lookup n checksums

checksums :: [(Int, Word64)]
checksums =
  [ (8, 1174115982843427320),
    (10, 4106818691694562354),
    (16, 2044818632734809475),
    (20, 2139914060307180272),
    (35, 6547974318304267630),
    (42, 3409190070512865098),
    (69, 10625813991079395451),
    (70, 4069862085209729981),
    (75, 4522139065345634902),
    (86, 357067594247069190)
  ]

main :: IO ()
main = do
  args <- getArgs
  case args of
    ["--print", value] | Just n <- readMaybe value, n > 0 -> either die print (checksum n)
    [profile, backend] | Just (n, expected) <- work backend profile ->
      if checksum n == Right expected then putStrLn "ok" else die "checksum mismatch"
    _ -> die "usage: happy-codegen <O0|O1|O2|Os> <native|llvm|wasm> | --print <positive family count>"
