import unittest
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from job_agent.intake import cheap_ats_signals
from job_agent.llm import build_intake_prompt, load_env_file
from job_agent.models import FieldEvidence, PageEvidence


class IntakeLlmTests(unittest.TestCase):
    def test_cheap_ats_signals_are_hints_only(self):
        signals = cheap_ats_signals("https://example.taleo.net/job/123")

        self.assertIn("oracle_taleo", signals["candidate_matches"])
        self.assertEqual(signals["source"], "cheap_signals_only_llm_must_confirm")

    def test_llm_prompt_includes_rich_field_evidence(self):
        evidence = PageEvidence(
            job_url="https://example.com/job",
            final_url="https://example.com/job",
            title="Senior Technology Leader",
            visible_text_sample="Apply now",
            ats_signals={"candidate_matches": {}},
            fields=[
                FieldEvidence(
                    field_id="candidate_name",
                    tag_name="input",
                    input_type="text",
                    label="Full legal name",
                    placeholder="Name",
                    aria_label=None,
                    required=True,
                    visible=True,
                    nearby_text="Personal information Full legal name",
                )
            ],
            buttons=["Next"],
        )

        prompt = build_intake_prompt(evidence)

        self.assertEqual(prompt["task"], "classify_ats_and_understand_form")
        self.assertEqual(prompt["fields"][0]["label"], "Full legal name")
        self.assertIn("required_output_schema", prompt)

    def test_env_loader_reads_gemini_key_shape(self):
        old_key = os.environ.pop("GEMINI_API_KEY", None)
        old_model = os.environ.pop("GEMINI_MODEL", None)
        with TemporaryDirectory() as tmp_dir:
            try:
                env_path = Path(tmp_dir) / ".env"
                env_path.write_text("GEMINI_API_KEY=test-key\nGEMINI_MODEL=gemini-test\n", encoding="utf-8")

                load_env_file(env_path)

                self.assertEqual(os.environ.get("GEMINI_API_KEY"), "test-key")
                self.assertEqual(os.environ.get("GEMINI_MODEL"), "gemini-test")
            finally:
                os.environ.pop("GEMINI_API_KEY", None)
                os.environ.pop("GEMINI_MODEL", None)
                if old_key is not None:
                    os.environ["GEMINI_API_KEY"] = old_key
                if old_model is not None:
                    os.environ["GEMINI_MODEL"] = old_model


if __name__ == "__main__":
    unittest.main()
