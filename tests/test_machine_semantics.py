import unittest

from mas004_rpi_databridge.machine_semantics import (
    button_to_command,
    command_to_target_state,
    pack_label_status_word,
    parse_button_mask,
)


class MachineSemanticsTests(unittest.TestCase):
    def test_button_mask_is_padded_and_named(self):
        mask = parse_button_mask("101")
        self.assertEqual(
            {
                "start": True,
                "pause": False,
                "stop": True,
                "setup": True,
                "sync": True,
                "empty": True,
                "rewind": True,
            },
            mask,
        )

    def test_commands_map_to_target_states(self):
        self.assertEqual(5, command_to_target_state(1, 3))
        self.assertEqual(9, command_to_target_state(2, 5))
        self.assertEqual(3, command_to_target_state(3, 1))
        self.assertEqual(17, command_to_target_state(4, 3))
        self.assertEqual(13, command_to_target_state(5, 9))
        self.assertEqual(11, command_to_target_state(6, 7))
        self.assertEqual(7, command_to_target_state(7, 5))
        self.assertEqual(1, button_to_command("start_pause", 3))
        self.assertEqual(7, button_to_command("start_pause", 5))

    def test_pack_label_status_word_sets_expected_bits(self):
        word = pack_label_status_word(
            label_no=42,
            material_ok=True,
            print_ok=False,
            verify_ok=True,
            removed=True,
            production_ok=False,
        )
        self.assertEqual(42 | (1 << 16) | (1 << 18) | (1 << 19), word)


if __name__ == "__main__":
    unittest.main()
