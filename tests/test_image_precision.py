"""Real 16-bit normalization and ICC regressions, without GPU inference."""
import array
import io
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from PIL import Image, ImageCms, TiffImagePlugin
import image_normalization as normal
import render_engine as engine


def linear_profile():
    # Keep the description deliberately unchanged. A profile called "sRGB"
    # must not be treated as sRGB merely because of its name.
    profile = bytearray(normal.srgb_profile())
    count = struct.unpack_from(">I", profile, 128)[0]
    for index in range(count):
        at = 132 + index * 12
        tag, offset, size = struct.unpack_from(">4sII", profile, at)
        if tag in {b"rTRC", b"gTRC", b"bTRC"}:
            curve = b"curv" + b"\0" * 4 + struct.pack(">IH", 1, 256)
            profile[offset:offset + len(curve)] = curve
            struct.pack_into(">I", profile, at + 8, len(curve))
    profile[84:100] = b"\0" * 16
    return bytes(profile)


class ImagePrecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dlss-image-precision-")
        self.root = Path(self.temp.name)
        self.ffmpeg = engine.find_tool("ffmpeg")

    def tearDown(self):
        self.temp.cleanup()

    def png(self, values, width, height, profile=None):
        raw = self.root / "source.raw"
        raw.write_bytes(array.array("H", values).tobytes())
        source = self.root / "source.png"
        normal.write_png16(raw, source, width, height, profile or normal.srgb_profile())
        return source

    def decode(self, path):
        result = subprocess.run([self.ffmpeg, "-v", "error", "-i", str(path), "-frames:v", "1",
                                 "-pix_fmt", "rgba64le", "-f", "rawvideo", "-"], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        values = array.array("H"); values.frombytes(result.stdout)
        return values

    def test_sixteen_bit_png_retains_low_bytes_and_alpha(self):
        values = [12345, 23456, 34567, 45678, 16384, 32768, 49152, 65535]
        source = self.png(values, 2, 1)
        output = self.root / "normalized.png"
        metadata = normal.normalize(source, output, self.ffmpeg)
        self.assertEqual(metadata["bit_depth"], 16)
        self.assertEqual(output.read_bytes()[24], 16)
        self.assertEqual(list(self.decode(output)), values)
        self.assertFalse(metadata["icc_converted"])

    @unittest.skipUnless(os.name == "nt", "Windows 16-bit ICM regression")
    def test_arbitrary_icc_is_transformed_at_sixteen_bit_precision(self):
        source = self.png([16384, 16384, 16384, 12345, 32768, 32768, 32768, 65535], 2, 1, linear_profile())
        output = self.root / "normalized.png"
        metadata = normal.normalize(source, output, self.ffmpeg)
        self.assertTrue(metadata["icc_converted"])
        values = self.decode(output)
        for value in values[:3]:
            self.assertLess(abs(value - 35199), 100)
        self.assertEqual(values[3], 12345)
        self.assertGreater(len({value % 257 for value in values[:3]}), 1)
        with Image.open(output) as image:
            self.assertTrue(normal.equivalent_profile(image.info["icc_profile"], normal.srgb_profile()))

    def test_tiff_sixteen_bit_orientation_is_baked_once(self):
        source = self.root / "portrait.tiff"
        values = array.array("H", [12345, 23456, 34567, 45678, 56789, 61234])
        image = Image.frombytes("I;16", (2, 3), values.tobytes())
        info = TiffImagePlugin.ImageFileDirectory_v2(); info[274] = 6
        image.save(source, tiffinfo=info)
        output = self.root / "normalized.png"
        metadata = normal.normalize(source, output, self.ffmpeg)
        self.assertEqual((metadata["width"], metadata["height"]), (3, 2))
        with Image.open(output) as image:
            self.assertEqual(image.size, (3, 2))
        self.assertEqual(list(self.decode(output)[0::4]), [56789, 34567, 12345, 61234, 45678, 23456])

    def test_eight_bit_icc_transforms_color_and_preserves_alpha(self):
        source = self.root / "small.png"
        Image.new("RGBA", (1, 1), (64, 64, 64, 111)).save(source, icc_profile=linear_profile())
        output = self.root / "normalized.png"
        metadata = normal.normalize(source, output, self.ffmpeg)
        with Image.open(output) as image:
            pixel = image.getpixel((0, 0))
            self.assertLess(abs(pixel[0] - 137), 2)
            self.assertEqual(pixel[3], 111)
        self.assertEqual(metadata["color_space"], "sRGB")


if __name__ == "__main__":
    unittest.main()
