import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
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

        self.assertEqual(voice, "Leda")
        self.assertIn("Indian", style)
        self.assertIn("human", style.lower())

    def test_voice_delivery_is_appended_after_editable_business_prompt(self):
        result = append_live_voice_style("USER EDITED BUSINESS PROMPT", "Natural Indian accent")

        self.assertTrue(result.startswith("USER EDITED BUSINESS PROMPT"))
        self.assertTrue(result.endswith("Natural Indian accent"))
        self.assertIn("HIGHEST PRIORITY", result)

    def test_old_greeting_profile_cannot_pass_text_tolerance(self):
        import json

        from core.greeting_pcm import load_recorded_greeting_pcm

        old_meta = {
            "text_hash": "different-text-hash",
            "text": "Hi, this is Vernika from Technopolis.",
            "style_hash": "old-style",
            "voice": "Aoede",
            "intro_only": True,
            "sr": 16000,
        }
        with TemporaryDirectory() as tmp:
            pcm_path = Path(tmp) / "greeting.pcm"
            meta_path = Path(tmp) / "greeting.pcm.meta"
            pcm_path.write_bytes(b"\x00\x00" * 100)
            meta_path.write_text(json.dumps(old_meta), encoding="utf-8")
            with (
                patch("core.greeting_pcm.greeting_pcm_paths", return_value=(pcm_path, meta_path)),
                patch(
                    "core.state.resolved_live_voice_profile",
                    return_value=("Aoede", "New natural Indian human delivery"),
                ),
            ):
                result = load_recorded_greeting_pcm(
                    "sales_1",
                    greeting_text="Hi, this is Vernika from Technopolis.",
                )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
