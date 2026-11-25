{-# LANGUAGE BangPatterns #-}
module Main where

import Codec.Picture (Image, PixelRGB8 (..), convertRGB8, generateImage, imageHeight, imageWidth)
import Codec.Picture.Jpg (decodeJpeg, encodeJpegAtQuality)
import Codec.Picture.Types (convertImage, pixelFold)
import Data.Bits (shiftR, xor, (.&.))
import qualified Data.ByteString as B
import qualified Data.ByteString.Lazy as BL
import Data.Word (Word64)
import System.Environment (getArgs)
import System.Exit (die)
import Text.Read (readMaybe)

-- | A square picture with smooth gradients, hard-edged checkers and hashed
-- noise, so every 8x8 block carries a different mix of low and high
-- frequencies. Integer arithmetic only: no libm result can differ between
-- platforms.
picture :: Int -> Image PixelRGB8
picture n = generateImage pixel n n
  where
    pixel x y =
      let ring = ((x - n `div` 2) * (x - n `div` 2) + (y - n `div` 3) * (y - n `div` 3)) `div` 37
          checker = if even ((x `div` 24) + (y `div` 24)) then 40 else 0
          noise = ((x * 7919) `xor` (y * 104729) `xor` (x * y)) .&. 63
       in PixelRGB8
            (fromIntegral ((ring + checker) .&. 255))
            (fromIntegral (((x + y) `shiftR` 1 + noise) .&. 255))
            (fromIntegral ((y * 255 `div` n + checker + noise `div` 2) .&. 255))

mix :: Word64 -> Int -> Word64
mix h x = (h `xor` fromIntegral x) * 1099511628211

-- | Encode at quality 85, decode the result, and hash the encoded bytes and
-- every decoded pixel.
checksum :: Int -> Either String Word64
checksum n = do
  let encoded = BL.toStrict (encodeJpegAtQuality 85 (convertImage (picture n)))
      !h = B.foldl' (\acc b -> mix acc (fromIntegral b)) 14695981039346656037 encoded
  decoded <- convertRGB8 <$> decodeJpeg encoded
  let pixels = pixelFold (\acc _ _ (PixelRGB8 r g b) -> mix (mix (mix acc (fromIntegral r)) (fromIntegral g)) (fromIntegral b)) h decoded
  pure (mix (mix pixels (imageWidth decoded)) (imageHeight decoded))

-- | Picture side in pixels per profile and backend, calibrated on an Apple
-- M4 Pro for about half a second of AIHC. The GHC checksums reject wrong
-- output.
-- AIHC's Os builds decoded wrong pixels when this was calibrated; their
-- sizes are timed, and the GHC checksums make those runs fail until fixed.
-- See docs/library-benchmarks-calibration.json for measurements.
work :: String -> String -> Maybe (Int, Word64)
work backend profile
  | backend == "native" = lookup profile native >>= withChecksum
  | backend == "llvm" = lookup profile llvm >>= withChecksum
  | backend == "wasm" = lookup profile wasm >>= withChecksum
  | otherwise = Nothing
  where
    native = [("O0", 153), ("O1", 823), ("O2", 1052), ("Os", 242)]
    llvm = [("O0", 79), ("O1", 795), ("O2", 991), ("Os", 250)]
    wasm = [("O0", 51), ("O1", 399), ("O2", 543), ("Os", 116)]
    withChecksum n = (,) n <$> lookup n checksums

checksums :: [(Int, Word64)]
checksums =
  [ (51, 17327841684380781116),
    (79, 6432628390414677616),
    (116, 9963726336211401238),
    (153, 7741349171955059706),
    (242, 2094742272865963923),
    (250, 9686957625668733955),
    (399, 15691629327700031035),
    (543, 7946276770325464214),
    (795, 11307517862223659461),
    (823, 9180217659370280404),
    (991, 5662575810681577606),
    (1052, 16098142942866226738)
  ]

main :: IO ()
main = do
  args <- getArgs
  case args of
    ["--print", value] | Just n <- readMaybe value, n > 0 -> either die print (checksum n)
    [profile, backend] | Just (n, expected) <- work backend profile ->
      if checksum n == Right expected then putStrLn "ok" else die "checksum mismatch"
    _ -> die "usage: juicypixels-jpeg <O0|O1|O2|Os> <native|llvm|wasm> | --print <positive side length>"
