module Main where

import Data.Digest.Pure.SHA (hmacSha256, sha1, sha256, sha512, showDigest)
import qualified Data.ByteString as Strict
import qualified Data.ByteString.Lazy as Lazy
import Data.Bits (shiftR, (.&.))
import Data.Word (Word8, Word64)
import System.Environment (getArgs)
import System.Exit (die)

-- | A small, seedable linear congruential generator. Deterministic across
-- runs and platforms so the benchmark never depends on external data or
-- network access.
nextSeed :: Word64 -> Word64
nextSeed seed = seed * 6364136223846793005 + 1442695040888963407

-- | One 4 KiB chunk of PRNG bytes. It is built once with 'Strict.unfoldrN'
-- rather than through a list, so producing the message is a small fraction
-- of hashing it and the run measures the SHA library rather than 'base'.
chunk :: Strict.ByteString
chunk = fst (Strict.unfoldrN 4096 step 42)
  where
    step :: Word64 -> Maybe (Word8, Word64)
    step seed = Just (fromIntegral ((seed `shiftR` 33) .&. 0xff), nextSeed seed)

-- | The message every digest is taken over: the chunk repeated to
-- 'messageChunks' * 4 KiB. SHA does not compress, so repetition costs the
-- library the same work as fresh bytes would.
message :: Int -> Lazy.ByteString
message messageChunks = Lazy.fromChunks (replicate messageChunks chunk)

-- | Chunks of message per backend and profile. The runner passes the
-- profile and the backend, and each pair of them hashes a different amount.
-- The counts are what take an AIHC build about half a second on an Apple M4
-- Pro; native and LLVM builds share the native counts.
work :: String -> String -> Maybe (Int, [String])
work backend profile
  | backend `elem` ["native", "llvm"] = lookup profile [("O0", 29), ("Os", 105), ("O1", 33), ("O2", 6198)] >>= withDigests
  | backend == "wasm" = lookup profile [("O0", 4), ("Os", 16), ("O1", 5), ("O2", 4751)] >>= withDigests
  | otherwise = Nothing
  where
    withDigests count = (,) count <$> lookup count expectedDigests

-- | The SHA-1, SHA-256, SHA-512 and HMAC-SHA-256 digests a correct build
-- produces for each chunk count 'work' uses. A miscompiled library fails the
-- run rather than producing a number.
expectedDigests :: [(Int, [String])]
expectedDigests =
  [ ( 4,
      [ "eb4f8894672de3863f8b90c6495105501d4be3ce",
        "fb6d044d793cc55f66aca4cb2e39f11e27045ba7bb9603f494254ebcf6d0d502",
        "c45f4fd65dd89e7171f082b4c61390296d4f74e4d799124d3ae2698912891b7a7ee9c08f3bb46a24bca71be15a4285b3023dbcdfae7e23a0a7bbf1d890a8d187",
        "c77cf45e26a8527bcfcc7eb3028b1fc8696fb03be5e1019f93a3b0c9a63a8683"
      ]
    ),
    ( 5,
      [ "db2172ac4fef7c993c8b473215eea16beef757c3",
        "2c57b3fde9911eaae78ee043ca1a925f909c386d1caace5dc8aa5a896b8bae09",
        "2e7108c45fcc4c245ef5d7a50090f7e8bd7d2b11fa3bb428433aedd77f9f88c735f0eb5b79ef9cd1e00f385d1eb4852727d4bb2b5c66efe468c6473dd58e838f",
        "4c14f8357067ad279275103ba808ddee36347d4233cf174d6e04dfba6329b62a"
      ]
    ),
    ( 16,
      [ "dd813ebce913853756424e58dcc47a64d5cf51d0",
        "5bfcc5a057edebaf319910a81c22fac851b1addd290c61f6a49a7a3897694d50",
        "e9e1e4e0acfff4d5a48387b8eabf4d6ffc5fc218ed1dc7adb4d0f11cd9597d4b999eba4189817a1b8e3ae6be24b915023af2422d84eb67d36fba92cc4f4b3f08",
        "a48d98c6e513e9f79a34d677ace50bb4000dc0b9ddb7d3de041206166b4e70d3"
      ]
    ),
    ( 29,
      [ "7519c513e95fc20cae52066765411b0a85d9e71f",
        "b1ca44dad13e690be917ebba6ce451e2031b134fa3bbb142f9fd3600b75bd0ac",
        "3030eaad75998ef0a5ef975a9ccbd448c8908953cadbf606a0d70aa9f13afb127cd3ef7b5cc09aa1e82365d768dbe2da122b82cf20cae8615bffc98d4fe7550d",
        "ce988d8a6ef1c7f9ab2d5c4f6ef41a4edae0fc712a7d82752623d815538594d6"
      ]
    ),
    ( 33,
      [ "fd4981b1be7f896708cd418d978654017eee40f8",
        "0897df2d389a686a8777967d422030a81523cc3e66c51712fdb8ebf179a816dc",
        "9d8208ec8b9d9419424cd8d683d78d6690c461983f3cec24a5647292441a805dcd766c4e09494fe053fcf82e7c25d2b879d17b36cb47acb2828e0c0f282b993f",
        "15121f29fc1828c7b4afc97ce363ce93c9eb5a1a70867d366f1883b0396d740f"
      ]
    ),
    ( 105,
      [ "6526c2187b63fe2937e5850d2e6da2f9d4dc28d6",
        "3430adb25e908e4fbe68b2dfcd01cf4ec56fb4c6ab8290aa3527fbc51ee1150d",
        "e4122e3aa586e6b443c2dd5413a82be4e05e8d186690f826186b814557091c30fa157c6c13354495bde8126633ab12c118f8ce2a1c31d2062528e905fbada253",
        "56f22e3caecc0b63eb641eebc23f2c7adc3f9301eab57f8f43cd7b2424ab332b"
      ]
    ),
    ( 4751,
      [ "f095409e733958e92c183bf80d23bdfb4001430d",
        "1a428f1a5f64dbf78adf3a86dd51eeb28aada2029803e533c2ac4dde5c36ee8f",
        "3476d52c1b48ad76ceaaae35a090b0267d88e3be2978ac89a4cc4ff5e4968226efce73f79a2045e106854b84b822f8584bc5b44d11de43d96f4739ae546edbe7",
        "d7974eb1b6c91fb94f067601ad9e788f5f8d993af17a4b57905824a04558a193"
      ]
    ),
    ( 6198,
      [ "2ffbd3212323f8e84135c5cc419b88b3f05dc5b5",
        "7d72f9edb251431a275629d1de444af553470447df0326384645b4e179db4147",
        "c8d1cfa2d35dbad117603c765df375b462d85e419e818a2a24954039e969a43653a6b87a3db0205798463daee08bf902c2ebf2f7043a03dc643d5db6df0e9f39",
        "e167cad1c3d132a481c912c477c6f4c2a6393b8eb4c4bd913c6e36a8a344ea2a"
      ]
    )
  ]

main :: IO ()
main = do
  args <- getArgs
  (chunks, expected) <- case args of
    [profile, backend] | Just found <- work backend profile -> pure found
    _ -> die "usage: sha-digest <O0|Os|O1|O2> <native|llvm|wasm>"
  let body = message chunks
      digests =
        [ showDigest (sha1 body),
          showDigest (sha256 body),
          showDigest (sha512 body),
          showDigest (hmacSha256 (Lazy.fromStrict (Strict.take 32 chunk)) body)
        ]
  putStrLn (if digests == expected then "ok" else "fail")
