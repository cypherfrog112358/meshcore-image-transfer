import argparse
import io
import random
import struct
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from PIL import Image

import mcimg_palette as palette
import mcimg_protocol as proto
from mcimg import Receiver


def receiver_for(directory: str, **overrides):
    values = dict(id=None, report_every=10**9, save_every=10**9)
    values.update(overrides)
    return Receiver(argparse.Namespace(out=directory, **values))


class ProtocolTests(unittest.TestCase):
    def test_chunk_size_and_roundtrip(self):
        rng = random.Random(42)
        for size in (0, 1, 82, 83, 84, 85, 86, 87, 1000, 40_000, 250_000):
            with self.subTest(size=size):
                data = bytes(rng.randrange(256) for _ in range(size))
                manifest = proto.make_manifest(data, "charmera_2026.jpg")
                messages = proto.make_data_messages(data, manifest)
                self.assertTrue(messages)
                self.assertTrue(all(len(m) <= proto.MAX_CHARS for m in messages))
                self.assertEqual(len(messages), manifest.chunks)

                with tempfile.TemporaryDirectory() as tmp:
                    receiver = receiver_for(tmp)
                    receiver.handle(proto.encode_manifest(manifest))
                    for text in messages:
                        receiver.handle(text)
                    self.assertIn(manifest.transfer_id, receiver.completed_ids)
                    rebuilt = b"".join(receiver.chunks[i] for i in range(manifest.chunks))
                    self.assertTrue(proto.verify(rebuilt, manifest))
                    self.assertEqual(rebuilt, data)

    def test_lossy_reorder(self):
        rng = random.Random(7)
        data = bytes(rng.randrange(256) for _ in range(12_345))
        manifest = proto.make_manifest(data, "img.png")
        messages = proto.make_data_messages(data, manifest)
        lost, delivered = messages[:10], messages[10:]
        random.Random(3).shuffle(delivered)

        with tempfile.TemporaryDirectory() as tmp:
            receiver = receiver_for(tmp)
            receiver.handle(proto.encode_manifest(manifest))
            for text in delivered:
                receiver.handle(text)
            self.assertLess(len(receiver.chunks), manifest.chunks)

            for text in lost:
                receiver.handle(text)
            rebuilt = b"".join(receiver.chunks[i] for i in range(manifest.chunks))
            self.assertEqual(rebuilt, data)

    def test_resume_from_progress(self):
        data = b"abc" * 100
        manifest = proto.make_manifest(data, "x.jpg")
        messages = proto.make_data_messages(data, manifest)

        with tempfile.TemporaryDirectory() as tmp:
            first = receiver_for(tmp, save_every=1)
            first.handle(proto.encode_manifest(manifest))
            for text in messages[:3]:
                first.handle(text)

            second = receiver_for(tmp, save_every=1)
            second.handle(proto.encode_manifest(manifest))
            self.assertEqual(len(second.chunks), 3)

    def test_existing_file_is_detected(self):
        data = b"already here" * 100
        manifest = proto.make_manifest(data, "vorhanden.jpg")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "vorhanden.jpg").write_bytes(data)
            receiver = receiver_for(tmp)
            receiver.handle(proto.encode_manifest(manifest))
            self.assertIn(manifest.transfer_id, receiver.completed_ids)
            self.assertEqual(receiver.chunks, {})

    def test_manifest_fits_and_filename(self):
        data = b"x" * 1000
        manifest = proto.make_manifest(data, "sehr_langer_dateiname_der_nicht_passt.jpg")
        text = proto.encode_manifest(manifest)
        self.assertLessEqual(len(text), proto.MAX_CHARS)
        parsed = proto.parse_message(text)
        self.assertIsInstance(parsed, proto.Manifest)
        self.assertEqual(parsed.transfer_id, manifest.transfer_id)
        self.assertEqual(parsed.ext, "jpg")

    def test_unrelated_messages(self):
        self.assertIsNone(proto.parse_message("Hallo Welt"))
        with self.assertRaises(proto.ProtocolError):
            proto.parse_message("I1:X:abc")

    def test_query_and_repair_roundtrip(self):
        tid = "deadbeef"
        query = proto.parse_message(proto.encode_query(tid, 7, 12))
        self.assertIsInstance(query, proto.Query)
        self.assertEqual((query.transfer_id, query.start, query.end), (tid, 7, 12))

        repair = proto.parse_message(proto.encode_repair(tid, [3, 7, 12, 3]))
        self.assertIsInstance(repair, proto.Repair)
        self.assertEqual(repair.missing, [3, 7, 12])
        self.assertFalse(repair.resend_all)

        empty = proto.parse_message(proto.encode_repair(tid, []))
        self.assertEqual(empty.missing, [])

        full = proto.parse_message(proto.encode_repair(tid, [], resend_all=True))
        self.assertTrue(full.resend_all)

    def test_repair_message_stays_short(self):
        tid = "deadbeef"
        text = proto.encode_repair(tid, list(range(500)), max_chars=40)
        self.assertLessEqual(len(text), 40)
        self.assertTrue(text.startswith(f"I1:R:{tid}:"))

    def test_receiver_answers_query(self):
        data = b"abc" * 300
        manifest = proto.make_manifest(data, "x.jpg")
        messages = proto.make_data_messages(data, manifest)
        answers = []

        with tempfile.TemporaryDirectory() as tmp:
            receiver = receiver_for(tmp)
            receiver.feedback = answers.append
            receiver.handle(proto.encode_manifest(manifest))
            receiver.handle(messages[0])
            receiver.handle(messages[2])
            receiver.handle(proto.encode_query(manifest.transfer_id, 0, 3))

        self.assertEqual(len(answers), 1)
        reply = proto.parse_message(answers[0])
        self.assertIsInstance(reply, proto.Repair)
        self.assertEqual(reply.missing, [1, 3])

    def test_bad_checksum_requests_full_resend(self):
        data = b"abc" * 300
        manifest = proto.make_manifest(data, "x.jpg")
        broken = replace(manifest, sha256="0" * 64)
        messages = proto.make_data_messages(data, manifest)
        answers = []

        with tempfile.TemporaryDirectory() as tmp:
            receiver = receiver_for(tmp)
            receiver.feedback = answers.append
            receiver.handle(proto.encode_manifest(broken))
            for text in messages:
                receiver.handle(text)

        self.assertEqual(len(answers), 1)
        reply = proto.parse_message(answers[0])
        self.assertTrue(reply.resend_all)


class PaletteTests(unittest.TestCase):
    def test_pack_unpack(self):
        values = [0, 1, 2, 3, 0, 3, 2, 1]
        packed = palette.pack_indices(values)
        self.assertEqual(len(packed), 2)
        self.assertEqual(palette.unpack_indices(packed, len(values)), values)

    def test_levels(self):
        values = bytes((0, 42, 43, 127, 128, 212, 213, 255))
        result = palette.quantize(values, len(values), 1, "none")
        self.assertEqual(result, [0, 0, 1, 1, 2, 2, 3, 3])

    def test_image_to_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.png"
            image = Image.new("RGB", (23, 11))
            image.putdata(
                [((x * 11) % 256, (y * 23) % 256, (x + y) % 256) for y in range(11) for x in range(23)]
            )
            image.save(src)

            for dither in palette.DITHER_MODES:
                with self.subTest(dither=dither):
                    container = palette.image_to_container(src, dither)
                    width, height, mode, payload_mode, payload = palette.parse_container(container)
                    self.assertEqual((width, height), (23, 11))
                    self.assertEqual(mode, dither)
                    self.assertIn(payload_mode, (palette.MODE_RAW, palette.MODE_PNG))
                    if payload_mode == palette.MODE_RAW:
                        indices = palette.unpack_indices(payload, width * height)
                        self.assertEqual(len(indices), 23 * 11)
                    else:
                        self.assertTrue(payload.startswith(b"\x89PNG"))
                    with Image.open(io.BytesIO(palette.container_to_png(container))) as png:
                        self.assertEqual(png.size, (23, 11))
                        self.assertEqual(png.mode, "P")
                        colors = png.convert("RGB").getcolors(maxcolors=10)
                    self.assertIsNotNone(colors)
                    self.assertLessEqual(len(colors), 4)
                    for _count, color in colors:
                        self.assertEqual(color, (color[0],) * 3)
                        self.assertIn(color[0], palette.LEVELS)

    def test_adaptive_payload_choice(self):
        with tempfile.TemporaryDirectory() as tmp:
            flat = Path(tmp) / "flat.png"
            Image.new("L", (120, 120), 255).save(flat)
            flat_container = palette.image_to_container(flat, "none")
            self.assertEqual(palette.parse_container(flat_container)[3], palette.MODE_PNG)

            noisy = Path(tmp) / "noisy.png"
            image = Image.new("L", (120, 120))
            rng = random.Random(12)
            image.putdata([rng.randrange(4) * 85 for _ in range(120 * 120)])
            image.save(noisy)
            noisy_container = palette.image_to_container(noisy, "none")
            self.assertEqual(palette.parse_container(noisy_container)[3], palette.MODE_RAW)

    def test_receiver_writes_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.jpg"
            image = Image.new("RGB", (8, 6), (110, 130, 150))
            image.save(src)
            container = palette.image_to_container(src, "none")
            manifest = proto.make_manifest(container, "src.mc4")
            messages = proto.make_data_messages(container, manifest)

            receiver = receiver_for(tmp)
            receiver.handle(proto.encode_manifest(manifest))
            for text in messages:
                receiver.handle(text)
            output = Path(tmp) / "src.png"
            self.assertTrue(output.is_file())
            with Image.open(output) as result:
                self.assertEqual(result.size, (8, 6))
                self.assertEqual(result.mode, "P")

    def test_legacy_mc5_container(self):
        width, height = 4, 2
        packed = bytes((0, 25 * 4 + 5 * 3 + 2))
        container = struct.pack(
            "<4sBBII", palette.LEGACY_MAGIC, 1, 0, width, height
        ) + packed * 2
        png = palette.container_to_png(container)
        with Image.open(io.BytesIO(png)) as image:
            self.assertEqual(image.size, (width, height))
            self.assertLessEqual(len(image.convert("RGB").getcolors()), 5)


if __name__ == "__main__":
    unittest.main()
