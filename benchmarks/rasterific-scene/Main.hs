module Main where

import Codec.Picture (PixelRGBA8 (..))
import Codec.Picture.Types (pixelFold)
import Data.Bits (xor)
import Data.Word (Word64)
import Graphics.Rasterific
import Graphics.Rasterific.Texture (linearGradientTexture, radialGradientTexture, uniformTexture)
import Graphics.Rasterific.Transformations (Transformation (..))
import System.Environment (getArgs)
import System.Exit (die)
import Text.Read (readMaybe)

-- | @count@ shapes of four kinds on a @side@ x @side@ canvas: circles with
-- radial gradients, stroked cubic Beziers with round joins and caps, dashed
-- polylines with a linear gradient, and rounded rectangles under a fixed
-- rotation matrix. Every shape has its own position, size and colour.
-- Nothing calls sin, cos or atan2 (Rasterific does for 'rotate', arcs and
-- focused gradients), so no libm result can differ between platforms.
scene :: Float -> Int -> Drawing PixelRGBA8 ()
scene side count = mapM_ shape [0 .. count - 1]
  where
    coord i k = side * fromIntegral ((i * 7919 + k * 104729) `mod` 1009) / 1009
    colour i = PixelRGBA8 (fromIntegral (i * 37)) (fromIntegral (i * 91)) (fromIntegral (i * 53))
    point i k = V2 (coord i k) (coord i (k + 1))
    scale = side / 512
    shape i = case i `mod` 4 of
      0 ->
        withTexture (radialGradientTexture [(0, colour i 255), (1, colour (i + 1) 80)] (point i 0) (40 * scale)) $
          fill (circle (point i 0) (10 * scale + coord i 3 / 12))
      1 ->
        withTexture (uniformTexture (colour i 200)) $
          stroke (3 * scale) JoinRound (CapRound, CapRound) (CubicBezier (point i 0) (point i 2) (point i 4) (point i 6))
      2 ->
        withTexture (linearGradientTexture [(0, colour i 255), (1, colour (i + 3) 255)] (point i 0) (point i 2)) $
          dashedStroke [6 * scale, 3 * scale] (4 * scale) (JoinMiter 2) (CapStraight 0, CapStraight 0) (polyline [point i k | k <- [0, 2 .. 10]])
      _ ->
        let V2 x y = point i 0
         in withTexture (uniformTexture (colour i 160)) $
              withTransformation (Transformation 0.8 (-0.6) x 0.6 0.8 y) $
                fill (roundedRectangle (V2 0 0) (scale * 20 + coord i 2 / 10) (scale * 15 + coord i 4 / 12) (5 * scale) (5 * scale))

mix :: Word64 -> Int -> Word64
mix h x = (h `xor` fromIntegral x) * 1099511628211

-- | Render the scene and hash every channel of every pixel.
checksum :: Int -> Int -> Word64
checksum side count =
  let image = renderDrawing side side (PixelRGBA8 255 255 255 255) (scene (fromIntegral side) count)
      channel acc v = mix acc (fromIntegral v)
   in pixelFold (\acc _ _ (PixelRGBA8 r g b a) -> channel (channel (channel (channel acc r) g) b) a) 14695981039346656037 image

-- | Canvas side and shape count per profile and backend, calibrated on an
-- Apple M4 Pro for about half a second of AIHC. Unoptimised builds also get
-- a smaller canvas: clearing and hashing a large one alone would fill the
-- budget. The GHC checksums reject wrong output.
-- See docs/library-benchmarks-calibration.json for measurements.
work :: String -> String -> Maybe ((Int, Int), Word64)
work backend profile
  | backend == "native" = lookup profile native >>= withChecksum
  | backend == "llvm" = lookup profile llvm >>= withChecksum
  | backend == "wasm" = lookup profile wasm >>= withChecksum
  | otherwise = Nothing
  where
    native = [("O0", (128, 44)), ("O1", (512, 127)), ("O2", (512, 457)), ("Os", (512, 37))]
    llvm = [("O0", (128, 14)), ("O1", (512, 122)), ("O2", (512, 437)), ("Os", (512, 27))]
    wasm = [("O0", (128, 10)), ("O1", (512, 32)), ("O2", (512, 147)), ("Os", (128, 33))]
    withChecksum size = (,) size <$> lookup size checksums

checksums :: [((Int, Int), Word64)]
checksums =
  [ ((128, 10), 18297795739511574903),
    ((128, 14), 12881179206964656257),
    ((128, 33), 3293900672520536389),
    ((128, 44), 1533180141772541161),
    ((512, 27), 4455728164479878308),
    ((512, 32), 5028397493636706475),
    ((512, 37), 1183050844405603531),
    ((512, 122), 584517334351594943),
    ((512, 127), 13908656513540905950),
    ((512, 147), 3722399214175554460),
    ((512, 437), 4703117849756364425),
    ((512, 457), 2340685759538614846)
  ]

main :: IO ()
main = do
  args <- getArgs
  case args of
    ["--print", s, c] | Just side <- readMaybe s, Just count <- readMaybe c, side > 0, count >= 0 -> print (checksum side count)
    [profile, backend] | Just ((side, count), expected) <- work backend profile ->
      if checksum side count == expected then putStrLn "ok" else die "checksum mismatch"
    _ -> die "usage: rasterific-scene <O0|O1|O2|Os> <native|llvm|wasm> | --print <side> <shape count>"
