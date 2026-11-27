import argparse
import asyncio
import io
import random
import struct
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path
from PIL import Image
from meshcore import EventType
from meshcore.events import Event

import mcimg
import mcimg_palette as palette
import mcimg_protocol as proto
from mcimg import Receiver


def receiver_for(directory: str, **overrides):
    values = dict(id=None, report_every=10**9, save_every=10**9)
    values.update(overrides)
    return Receiver(argparse.Namespace(out=directory, **values))


def data_with_chunks(count: int):
    """Build deterministic data that produces exactly ``count`` packets."""
    length = count * 80 + 1
    for _ in range(2000):
        data = bytes((i * 29 + 11) % 256 for i in range(length))
        manifest = proto.make_manifest(data, "x.bin")
        if manifest.chunks == count:
            return data, manifest, proto.make_data_messages(data, manifest)
        length += 1 if manifest.chunks < count else -1
    raise AssertionError(f"konnte {count} Pakete nicht erreichen")


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


class TableFullTests(unittest.TestCase):
    def test_retry_on_table_full(self):
        calls = 0

        async def send_msg(_contact, _text):
            nonlocal calls
            calls += 1
            if calls <= 2:
                return Event(
                    EventType.ERROR,
                    {"error_code": 3, "code_string": "ERR_CODE_TABLE_FULL"},
                )
            return Event(EventType.MSG_SENT, {"expected_ack": b"\x01"})

        args = argparse.Namespace(
            transport="fake",
            ack=False,
            retries=3,
            table_full_wait=0.01,
            table_full_retries=5,
        )
        mc = types.SimpleNamespace(
            commands=types.SimpleNamespace(send_msg=send_msg)
        )
        self.assertTrue(asyncio.run(mcimg.send_text(mc, object(), args, "I1:D:x")))
        self.assertEqual(calls, 3)
        self.assertGreaterEqual(args._table_pause, 0.2)

    def test_table_full_gives_up(self):
        async def send_msg(_contact, _text):
            return Event(
                EventType.ERROR,
                {"error_code": 3, "code_string": "ERR_CODE_TABLE FULL"},
            )

        args = argparse.Namespace(
            transport="fake",
            ack=False,
            retries=3,
            table_full_wait=0.01,
            table_full_retries=3,
        )
        mc = types.SimpleNamespace(
            commands=types.SimpleNamespace(send_msg=send_msg)
        )
        self.assertFalse(asyncio.run(mcimg.send_text(mc, object(), args, "hi")))


class FinalRepairTests(unittest.TestCase):
    def test_bad_checksum_is_repaired_after_last_packet(self):
        data = bytes((i * 17) % 256 for i in range(700))
        manifest = proto.make_manifest(data, "frame.mc4")
        messages = proto.make_data_messages(data, manifest)

        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                out=tmp,
                id=None,
                report_every=10**9,
                save_every=10**9,
                transport="fake",
                ack=False,
                retries=3,
                max_chars=proto.MAX_CHARS,
                gap_ms=0,
                pipeline=8,
                repair=True,
                repair_wait=0.1,
                repair_rounds=2,
                manifest_repeat=1,
                limit=None,
                table_full_wait=0.01,
                table_full_retries=5,
            )
            receiver = Receiver(args)
            state = {"subscribers": [], "corrupt": True}

            def deliver(text):
                for callback in list(state["subscribers"]):
                    callback(types.SimpleNamespace(payload={"text": text}))

            async def send_msg(_contact, text):
                parsed = proto.parse_message(text)
                if isinstance(parsed, (proto.Manifest, proto.DataChunk)):
                    if (
                        isinstance(parsed, proto.DataChunk)
                        and parsed.seq == 3
                        and state["corrupt"]
                    ):
                        state["corrupt"] = False
                        body = (
                            f"I1:D:{parsed.transfer_id}:{proto.b36(parsed.seq)}:"
                        )
                        damaged = bytes([parsed.data[0] ^ 0xFF]) + parsed.data[1:]
                        receiver.handle(body + proto.b64encode(damaged))
                    else:
                        receiver.handle(text)
                elif isinstance(parsed, proto.Query):
                    missing = [
                        seq
                        for seq in range(parsed.start, parsed.end + 1)
                        if seq not in receiver.chunks
                    ]
                    deliver(proto.encode_repair(parsed.transfer_id, missing))
                return types.SimpleNamespace(type=EventType.MSG_SENT, payload={})

            mc = types.SimpleNamespace(
                commands=types.SimpleNamespace(send_msg=send_msg),
                subscribe=lambda _event, cb: state["subscribers"].append(cb) or cb,
                unsubscribe=lambda cb: state["subscribers"].remove(cb),
            )
            receiver.feedback = deliver
            receiver.handle(proto.encode_manifest(manifest))
            code = asyncio.run(mcimg.send_prepared(mc, object(), args, manifest, messages))

        self.assertEqual(code, 0)
        self.assertIn(manifest.transfer_id, receiver.completed_ids)

    def test_resume_with_silent_block_queries_is_repaired(self):
        data, manifest, messages = data_with_chunks(81)
        with tempfile.TemporaryDirectory() as tmp:
            # Previous attempt left 54 packets in the hidden progress file.
            seed = receiver_for(tmp, save_every=1)
            seed.handle(proto.encode_manifest(manifest))
            for text in messages[:54]:
                seed.handle(text)
            self.assertTrue((Path(tmp) / manifest.progress_name).is_file())

            args = argparse.Namespace(
                out=tmp,
                id=None,
                report_every=10**9,
                save_every=10**9,
                transport="fake",
                ack=False,
                retries=3,
                max_chars=proto.MAX_CHARS,
                gap_ms=0,
                pipeline=8,
                repair=True,
                repair_wait=0.01,
                repair_rounds=2,
                manifest_repeat=1,
                limit=None,
                table_full_wait=0.01,
                table_full_retries=5,
                _final_wait_floor=0.05,
                _reply_quiet=0.02,
            )
            receiver = receiver_for(tmp)
            state = {
                "subscribers": [],
                "final_query": False,
                "block_queries": 0,
            }

            def deliver(text):
                for callback in list(state["subscribers"]):
                    callback(types.SimpleNamespace(payload={"text": text}))

            receiver.feedback = deliver

            async def send_msg(_contact, text):
                parsed = proto.parse_message(text)
                if isinstance(parsed, proto.Manifest):
                    receiver.handle(text)
                elif isinstance(parsed, proto.DataChunk):
                    # During the pipeline only duplicates arrive; the 27
                    # missing packets are delivered after the final query.
                    if state["final_query"] or parsed.seq < 54:
                        receiver.handle(text)
                elif isinstance(parsed, proto.Query):
                    if parsed.start == 0 and parsed.end == manifest.chunks - 1:
                        state["final_query"] = True
                        receiver.handle(text)
                    else:
                        state["block_queries"] += 1  # receiver does not answer
                return types.SimpleNamespace(type=EventType.MSG_SENT, payload={})

            mc = types.SimpleNamespace(
                commands=types.SimpleNamespace(send_msg=send_msg),
                subscribe=lambda _event, cb: state["subscribers"].append(cb) or cb,
                unsubscribe=lambda cb: state["subscribers"].remove(cb),
            )
            code = asyncio.run(mcimg.send_prepared(mc, object(), args, manifest, messages))

            self.assertEqual(code, 0)
            self.assertEqual(state["block_queries"], 2)
            self.assertIn(manifest.transfer_id, receiver.completed_ids)
            self.assertEqual((Path(tmp) / "x.bin").read_bytes(), data)
            self.assertFalse((Path(tmp) / manifest.progress_name).exists())

    def test_unsolicited_resend_all_survives_final_query(self):
        data = bytes((i * 17) % 256 for i in range(700))
        manifest = proto.make_manifest(data, "frame.bin")
        messages = proto.make_data_messages(data, manifest)

        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                out=tmp,
                id=None,
                report_every=10**9,
                save_every=10**9,
                transport="fake",
                ack=False,
                retries=3,
                max_chars=proto.MAX_CHARS,
                gap_ms=0,
                pipeline=8,
                repair=True,
                repair_wait=0.01,
                repair_rounds=2,
                manifest_repeat=1,
                limit=None,
                table_full_wait=0.01,
                table_full_retries=5,
                _final_wait_floor=0.05,
                _reply_quiet=0.02,
            )
            receiver = Receiver(args)
            state = {"subscribers": [], "corrupt": True}

            def deliver(text):
                for callback in list(state["subscribers"]):
                    callback(types.SimpleNamespace(payload={"text": text}))

            async def send_msg(_contact, text):
                parsed = proto.parse_message(text)
                if isinstance(parsed, proto.Manifest):
                    receiver.handle(text)
                elif isinstance(parsed, proto.DataChunk):
                    if parsed.seq == 3 and state["corrupt"]:
                        state["corrupt"] = False
                        body = (
                            f"I1:D:{parsed.transfer_id}:{proto.b36(parsed.seq)}:"
                        )
                        damaged = bytes([parsed.data[0] ^ 0xFF]) + parsed.data[1:]
                        receiver.handle(body + proto.b64encode(damaged))
                    else:
                        receiver.handle(text)
                elif isinstance(parsed, proto.Query):
                    pass  # only the unsolicited resend_all can save this run
                return types.SimpleNamespace(type=EventType.MSG_SENT, payload={})

            mc = types.SimpleNamespace(
                commands=types.SimpleNamespace(send_msg=send_msg),
                subscribe=lambda _event, cb: state["subscribers"].append(cb) or cb,
                unsubscribe=lambda cb: state["subscribers"].remove(cb),
            )
            receiver.feedback = deliver
            code = asyncio.run(mcimg.send_prepared(mc, object(), args, manifest, messages))

            self.assertEqual(code, 0)
            self.assertIn(manifest.transfer_id, receiver.completed_ids)
            self.assertEqual((Path(tmp) / "frame.bin").read_bytes(), data)


if __name__ == "__main__":
    unittest.main()
