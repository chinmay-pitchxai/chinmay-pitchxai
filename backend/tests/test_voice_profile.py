import unittest
from unittest.mock import patch

from core.state import append_live_voice_style, resolved_live_voice_profile


class VoiceProfileTests(unittest.TestCase):
    def test_saved_voice_and_indian_delivery_style_are_resolved(self):
        state = {
            "vobiz": {
                "voice": "Sulafat",
                "voice_style": "Natural Indian English from Hyderabad; warm and human.",
            }
        }
        with patch("core.state.get_state", return_value=state):
            voice, style = resolved_live_voice_profile("sales_1")

        self.assertEqual(voice, "Sulafat")
        self.assertIn("Indian English", style)

    def test_unknown_voice_cannot_reach_live_api(self):
        state = {"vobiz": {"voice": "not-a-real-voice", "voice_style": ""}}
        with patch("core.state.get_state", return_value=state):
            voice, style = resolved_live_voice_profile("sales_1")

        self.assertEqual(voice, "Aoede")
        self.assertIn("Indian", style)
        self.assertIn("human", style.lower())

    def test_voice_delivery_is_appended_after_editable_business_prompt(self):
        result = append_live_voice_style("USER EDITED BUSINESS PROMPT", "Natural Indian accent")

        self.assertTrue(result.startswith("USER EDITED BUSINESS PROMPT"))
        self.assertTrue(result.endswith("Natural Indian accent"))
        self.assertIn("HIGHEST PRIORITY", result)

    def test_old_greeting_profile_cannot_pass_text_tolerance(self):
        from core.greeting_pcm import _greeting_meta_matches

        old_meta = {
            "text_hash": "legacy",
            "style_hash": "old-style",
            "voice": "Aoede",
        }
        with patch(
            "core.state.resolved_live_voice_profile",
            return_value=("Aoede", "New natural Indian human delivery"),
        ):
            self.assertFalse(_greeting_meta_matches(old_meta, "same greeting", "sales_1"))


if __name__ == "__main__":
    unittest.main()
