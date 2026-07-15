import unittest

from swiss_qr_reference import SwissQRReferenceGenerator, generate_next_qrr


class TestSwissQRReferenceGenerator(unittest.TestCase):
    def setUp(self):
        self.generator = SwissQRReferenceGenerator()

    def test_validate_known_reference(self):
        self.assertTrue(self.generator.validate("00 00000 00000 00000 00026 30181"))

    def test_from_sequence_produces_valid_27_digit_reference(self):
        ref = self.generator.from_sequence(26302)
        self.assertEqual(len(ref.storage), 27)
        self.assertTrue(ref.storage.isdigit())
        self.assertTrue(self.generator.validate(ref.storage))

    def test_next_from_sequence_compatible_with_legacy_shape(self):
        result = self.generator.next_from_sequence(26301)
        self.assertEqual(result["next_sequence_to_store"], 26302)
        self.assertEqual(len(result["storage_string"]), 27)
        self.assertIn(" ", result["visual_string"])
        self.assertTrue(self.generator.validate(result["storage_string"]))

    def test_next_from_reference_increments_payload(self):
        seed = "00 00000 00000 00000 00026 30181"
        nxt = self.generator.next_from_reference(seed)
        self.assertTrue(self.generator.validate(nxt.storage))
        self.assertEqual(int(nxt.payload), int(self.generator.parse(seed).payload) + 1)

    def test_next_from_reference_with_fixed_segment_keeps_positions_21_22(self):
        seed = "00 00000 00000 00000 00026 30181"
        nxt = self.generator.next_from_reference_with_fixed_segments(
            seed,
            fixed_segments=[{"start": 21, "value": "26"}],
        )
        self.assertTrue(self.generator.validate(nxt.storage))
        self.assertEqual(nxt.payload[20:22], "26")
        self.assertNotEqual(nxt.storage, self.generator.parse(seed).storage)

    def test_prefix_sequence_mode(self):
        ref = self.generator.from_prefix_and_sequence(prefix="123456", sequence=987)
        self.assertTrue(self.generator.validate(ref.storage))
        self.assertTrue(ref.storage.startswith("123456"))

    def test_parse_rejects_bad_reference(self):
        with self.assertRaises(ValueError):
            self.generator.parse("123")

    def test_generate_next_qrr_convenience(self):
        result = generate_next_qrr("00 00000 00000 00000 00026 30181")
        self.assertEqual(set(result.keys()), {"storage_string", "visual_string"})
        self.assertTrue(self.generator.validate(result["storage_string"]))


if __name__ == "__main__":
    unittest.main()
