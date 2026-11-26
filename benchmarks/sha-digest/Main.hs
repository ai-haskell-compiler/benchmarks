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
-- Pro.
work :: String -> String -> Maybe (Int, [String])
work backend profile
  | backend == "native" = lookup profile [("O0", 42), ("Os", 196), ("O1", 2343), ("O2", 9060)] >>= withDigests
  | backend == "llvm" = lookup profile [("O0", 11), ("Os", 200), ("O1", 3280), ("O2", 11161)] >>= withDigests
  | backend == "wasm" = lookup profile [("O0", 9), ("Os", 43), ("O1", 704), ("O2", 7070)] >>= withDigests
  | otherwise = Nothing
  where
    withDigests count = (,) count <$> lookup count expectedDigests

-- | The SHA-1, SHA-256, SHA-512 and HMAC-SHA-256 digests a correct build
-- produces for each chunk count 'work' uses. A miscompiled library fails the
-- run rather than producing a number.
expectedDigests :: [(Int, [String])]
expectedDigests =
  [ ( 9,
      [ "a313ff792b1543382a20c6ae7a4794d77b05462b",
        "6adb58c3bde9d645855675be6f0f4eebe8cb96dced843e9e16ebd72a920c5986",
        "5f5e69bad290fa0f8a6d230aec2ce0cfd260c01862db7780676500090e46c0fe9e0b7596c5094968986dc9c2cc5540c7ed41433c09a56d0e2ab53b6c2ae4a333",
        "348a3f17d5b0fde8216a9f5b21450f59499970d26683fed79b8b8b95dceb94be"
      ]
    ),
    ( 11,
      [ "20ce255a06c8eeff3109e2334ea051d826b10053",
        "d9ed4e80f7dee3bf85a202f3a577d5c74965aba77a15e2f64671058e53c58323",
        "49ad47bd9a46e35a349a720596fc87b014f107de6ff958a1baea73f5e49985718e825d4ec466a54c624896ea4a692c535183034ce1d2fbfdabadb629e77ae27d",
        "70d486ea9efd917b9df34955e214e8780f12adb6d0e30ffb4b25a1a6576dd8f6"
      ]
    ),
    ( 42,
      [ "ebb0eb456d93fd3232bb5670b5529f2aafd0a6fc",
        "f1f929c8723abb30949b2242d23f894b6a6918b37209cb83a2fb1216bbe218fb",
        "6422c253a83b738b37bb4755294e5becea41b135aab4e367b9ebf1ed887fd383bf05219d5fad44f43de168fa9fc2d6460aecd93b18fbaffbf23d9b46f2b8e818",
        "30bdccfa0b95485c8e3a9309b797d83cf22e7f7c0a30a75a09fbd6bde203c4a9"
      ]
    ),
    ( 43,
      [ "42e33e9b9e65c1e45cebd692919c40ce126b79f4",
        "24944455ff9fa5b7dbd5fb3e66b95ee31a516adbb18462f99007c844d37852cf",
        "f2854ff07ed9fd9d5ebd5e5200e7f82e408b064bc26f75c8f0076771c07c6bd226df999d6824b8a9957a98ce78e5d01f8f9794d4ef9ea7b1ede358185bb270e3",
        "cf6625e15b99e8914f534df0a3c065f29472b3410c19536e34d53418a88c38d3"
      ]
    ),
    ( 196,
      [ "a7509ea6f998338f7d2f2d11ee2e8432e01d4265",
        "e400e00d9681f3cbf5faabcf445c813e6e871af842ae600356743da8095cd288",
        "57c732f4397dfa0bb1e0171c12c718b74f969f62a331ba62fb604a3987162d0aa8b2dbc3a86d6b4d1078859475313ccb67eb2233eb6632fc333c661ed88a529a",
        "8d3468850a4d533523d03133199cb52ef36d18ce2a3e244ca10e5d4ab26e453f"
      ]
    ),
    ( 200,
      [ "929af002eb6c5e36a879b18e54d1f8d13f8bd482",
        "99eecbfd1cf1cebea121e5868965bb854fbb44ae4550d4116414da7d41f71128",
        "aef03198b036e50ef12c9967c9af2e569351294d8871c417659c3503b00b79c31833640eda98e29548192595f5d6fc7d7b1c51b2ae7c4885465cd2ffe943f3c3",
        "42dc471af259f4c2ab0dc1a83372b14d3434adb4866cfb77216f2d722ec30286"
      ]
    ),
    ( 704,
      [ "6a001757bdbdc512c2a27e6d30ac9d47eb76bf56",
        "b60aad93ce56f269f859e023a18de9a67def004ff1cbe1b3b3d86f4f51f9f4c9",
        "e44c205c0edf78c8db567b71ee728f0e31657eab5d548640a75fee101fab0441a2446002bc1b48503be6b9bef30f73780c47e0a8ef79fafa76f580f226f65eb9",
        "50575fe52d451019fcafd8936aabe854364c30c4ee1e5562bda3fe10a5b8a3f8"
      ]
    ),
    ( 2343,
      [ "13ea99183ec76d9dc7f643b8e6e3a290181affc1",
        "fc18d3f12d3e5ac733c57058d0f71693e78a8985f5391fed561f730eeb057aad",
        "f77e80a007aace561331f77d260a778ad270a6cac05f0ffdb9766dc294870ddb2318781aea7e7baf04486df76247b7436ce3fbe393ec9ab456692545a5762200",
        "85c7c58acabe8b254df3a089034c582ca9e8e56c7b8aae0b8fe13938f6085fd3"
      ]
    ),
    ( 3280,
      [ "34c263a58a4f943c9e9f366bbaa9d87862cba5cb",
        "f2fd81e0955b148ef94fb95a15a9470d02a2fd5f184d385ac5d37a081ea24a15",
        "16545f9354e5ffcd62eeadf91dac7c1bac95d51b0045f8f7a08b3fdbaf7d010915f3adbbb95d527247c56fad07b277e776b47804c8c5353cb654e260b8875109",
        "cded0bef2d2aff783416262e450e7169622e63d9dc05eb2f85e6ff5ba37d9f15"
      ]
    ),
    ( 7070,
      [ "a72deb7d81d8528f855068b1aad41a202556e8c7",
        "f97ad47f2a3be88be38a4fb8f09d878b47712cd10ca5ed57ec52505ad90c73b5",
        "7fb03f0daadc38566a49599401820b89ae741a01a35744301045ce5ba7f39517e9eabebfb9ec03dfb34f2623de6ae97979f594bc5dcb5b68492bbf11889553f0",
        "3e26f8a1b2f62c77537c677bc87395cd7ad9b782c63dab15ee7d2e3cf8e9c216"
      ]
    ),
    ( 9060,
      [ "2e7c0552bf77385745b1e7078e431c39677dac5e",
        "bca21619de2edf3754b40ac49f87d238a775d839387522cdfb57b78748d7dfea",
        "ddee1ad3eb43922628735ff7afc04ff03e3c27c4bd251da9b425c6c8932b7b9775cae093226c7593c4e3df4f7ad4ca992aed1c230cc21b5f2a6922909e28dc23",
        "62f852e33c18afc68cd900cf140971ad61b6ce98aa311b89091b214e01de456b"
      ]
    ),
    ( 11161,
      [ "5c2b878cc7d303f643a60c3e74bdb80278c10153",
        "4ea1d1652ddc7cbd7463cbe153854c301a35bb942351fbc65293cbfed064e7f2",
        "1d8dfe162ce2e2b3a919455048a42f88cbe23a2598c973054b7c2b8556a895d1ff511e269ed405d44fc5e268d891f4727c6006c7167582b96e2624f5d9b057e1",
        "5e4c9463610fe31d5ed1f0c53e5c7380a50800ddc4d1162948aa5d134b098688"
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
