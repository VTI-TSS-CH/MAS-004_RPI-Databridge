import unittest

from mas004_rpi_databridge.machine_semantics import (
    action_for_button,
    button_led_plan,
    button_to_command,
    command_to_target_state,
    lamp_outputs_for_state,
    pack_label_status_word,
    parse_button_mask,
    settle_machine_state,
    state_actions,
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

    def test_start_pause_reset_uses_start_mask_action(self):
        self.assertEqual("start", action_for_button("start_pause", 21, reset_context=True))
        self.assertEqual("start", action_for_button("start_pause", 7))
        self.assertEqual("pause", action_for_button("start_pause", 5))

    def test_button_led_plan_mirrors_allowed_buttons(self):
        mask = parse_button_mask("1101111")
        leds = button_led_plan(9, mask, ts=0.0)

        self.assertFalse(leds["Q0.3"])

        mask = parse_button_mask("1111111")
        leds = button_led_plan(9, mask, ts=0.0)

        self.assertFalse(leds["Q0.3"])
        self.assertTrue(leds["Q0.4"])
        self.assertFalse(leds["Q0.7"])

    def test_pause_allows_only_start_and_stop(self):
        actions = state_actions(7)

        self.assertTrue(actions["start"])
        self.assertTrue(actions["stop"])
        self.assertFalse(actions["pause"])
        self.assertFalse(actions["setup"])
        self.assertFalse(actions["sync"])
        self.assertFalse(actions["empty"])
        self.assertFalse(actions["rewind"])

    def test_setup_transition_allows_stop_abort(self):
        actions = state_actions(2)

        self.assertTrue(actions["stop"])
        self.assertFalse(actions["start"])
        self.assertFalse(actions["setup"])

    def test_setup_mode_blinks_setup_led_instead_of_stop_led(self):
        mask = parse_button_mask("1111111")

        leds_on = button_led_plan(2, mask, ts=0.0)
        leds_off = button_led_plan(3, mask, ts=1.0)

        self.assertFalse(leds_on["Q0.3"])
        self.assertTrue(leds_on["Q0.4"])
        self.assertFalse(leds_off["Q0.3"])
        self.assertFalse(leds_off["Q0.4"])

    def test_setup_is_allowed_only_from_production_stop(self):
        self.assertTrue(state_actions(9)["setup"])
        for state in (1, 3, 5, 7, 11, 13, 19):
            self.assertFalse(state_actions(state)["setup"])

    def test_rewind_is_not_allowed_before_production_finished(self):
        self.assertFalse(state_actions(9)["rewind"])
        self.assertFalse(state_actions(13)["rewind"])
        self.assertTrue(state_actions(19)["rewind"])

    def test_light_curtain_pauses_production_and_rewind_only(self):
        for state in (5, 10, 11):
            new_state, source = settle_machine_state(
                state,
                state,
                estop_ok=True,
                light_curtain_ok=False,
                ups_ok=True,
                purge_active=False,
            )

            self.assertEqual(7, new_state)
            self.assertEqual("light_curtain_pause", source)

        for state in (3, 7, 9, 13, 19):
            new_state, source = settle_machine_state(
                state,
                state,
                estop_ok=True,
                light_curtain_ok=False,
                ups_ok=True,
                purge_active=False,
            )

            self.assertEqual(state, new_state)
            self.assertEqual("requested", source)

    def test_transition_states_show_steady_magenta_status_lamp(self):
        for state in (2, 4, 6, 8, 10, 12, 14, 16, 18):
            with self.subTest(state=state):
                self.assertEqual(
                    {"red": 1, "green": 0, "blue": 1},
                    lamp_outputs_for_state(state, warning_active=False, ts=0.0),
                )
                self.assertEqual(
                    {"red": 1, "green": 0, "blue": 1},
                    lamp_outputs_for_state(state, warning_active=False, ts=0.75),
                )

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
