import json
import unittest

from monitor import (
    apply_current_officials,
    find_changes,
    maybe_simulate,
    parse_questions,
    _names_for_heading,
)


PDF_SNIPPET = """
29. Name your U.S. representative.
• Answers will vary.
30. What is the name of the Speaker of the House of Representatives now? *
• Visit uscis.gov/citizenship/testupdates for the name of the Speaker of the House of Representatives.
31. Who does a U.S. senator represent?
• Citizens of their state
• People of their state
37. The President of the United States can serve only two terms. Why?
• (Because of) the 22nd Amendment
38. What is the name of the President of the United States now? *
• Visit uscis.gov/citizenship/testupdates for the name of the President of the United States.
6 of 19
uscis.gov/citizenship
39. What is the name of the Vice President of the United States now? *
• Visit uscis.gov/citizenship/testupdates for the name of the Vice President of the United States.
40. If the president can no longer serve, who becomes president?
• The Vice President (of the United States)
56. Supreme Court justices serve for life. Why?
• To be independent (of politics)
57. Who is the Chief Justice of the United States now?
• Visit uscis.gov/citizenship/testupdates for the name of the Chief Justice of the United States.
58. Name one power that is only for the federal government.
• Print paper money
"""

HTML_2025 = """
<h4>2025 Naturalization Civics Test</h4>
<p>30. What is the name of the Speaker of the House of Representatives now?*</p>
<ul>
<li>Mike Johnson</li>
<li>Johnson</li>
<li>James Michael Johnson (birth name)</li>
</ul>
<p>38. What is the name of the President of the United States now?*</p>
<ul>
<li>Donald J. Trump</li>
<li>Donald Trump</li>
<li>Trump</li>
</ul>
<p>39. What is the name of the Vice President of the United States now?</p>
<ul>
<li>JD Vance</li>
<li>Vance</li>
</ul>
<p>57. Who is the Chief Justice of the United States now?</p>
<ul>
<li>John Roberts</li>
<li>John G. Roberts, Jr.</li>
</ul>
<p>39. How many justices are on the Supreme Court?</p>
<ul><li>nine (9)</li></ul>
"""


class CompareTests(unittest.TestCase):
    def test_simulated_ohio_governor_is_detected(self):
        baseline = {
            "civics_questions": {
                "47": {
                    "number": 47,
                    "question": "What is the political party of the President now?",
                    "answers": ["Republican (Party)"],
                    "senior": False,
                }
            },
            "seniors_questions": [2, 7],
            "executive": {
                "president": "Donald J. Trump",
                "vice_president": "JD Vance",
                "speaker_of_the_house": "Mike Johnson",
                "chief_justice": "John Roberts",
            },
            "governors": [
                {"state": "OH", "state_name": "Ohio", "name": "Mike DeWine"},
                {"state": "NY", "state_name": "New York", "name": "Kathy Hochul"},
            ],
            "senators": [],
            "representatives": [],
        }
        simulated = maybe_simulate(baseline)
        changes = find_changes(baseline, simulated)
        self.assertTrue(any("Governor of Ohio" in c for c in changes))
        self.assertTrue(any("SIMULATED TEST GOVERNOR" in c for c in changes))

    def test_question_answer_change_message(self):
        old = {
            "civics_questions": {
                "47": {
                    "number": 47,
                    "question": "Q",
                    "answers": ["old"],
                    "senior": False,
                }
            },
            "seniors_questions": [],
            "executive": {},
            "governors": [],
            "senators": [],
            "representatives": [],
        }
        new = json.loads(json.dumps(old))
        new["civics_questions"]["47"]["answers"] = ["new"]
        changes = find_changes(old, new)
        self.assertEqual(changes, ["Question 47 answer updated"])


class ParseTests(unittest.TestCase):
    def test_pdf_does_not_keep_footers_or_visit_links(self):
        parsed = parse_questions(PDF_SNIPPET)
        self.assertEqual(parsed["30"]["answers"], [])
        self.assertEqual(parsed["38"]["answers"], [])
        self.assertEqual(parsed["39"]["answers"], [])
        self.assertEqual(parsed["57"]["answers"], [])
        self.assertNotIn("6 of 19", " ".join(parsed["38"]["answers"]))
        self.assertNotIn("uscis.gov", " ".join(parsed["38"]["answers"]))
        self.assertEqual(
            parsed["40"]["answers"],
            ["The Vice President (of the United States)"],
        )
        self.assertEqual(parsed["58"]["answers"], ["Print paper money"])

    def test_executive_headings_are_not_cross_contaminated(self):
        president = _names_for_heading(
            HTML_2025, r"What is the name of the President of the United States now"
        )
        vp = _names_for_heading(
            HTML_2025,
            r"What is the name of the Vice President of the United States now",
        )
        speaker = _names_for_heading(
            HTML_2025,
            r"What is the name of the Speaker of the House of Representatives now",
        )
        cj = _names_for_heading(
            HTML_2025, r"Who is the Chief Justice of the United States now"
        )
        self.assertEqual(president, "Donald J. Trump")
        self.assertEqual(vp, "JD Vance")
        self.assertEqual(speaker, "Mike Johnson")
        self.assertEqual(cj, "John G. Roberts, Jr.")
        self.assertNotIn("Vance", president)
        self.assertNotIn("Trump", vp)

    def test_overlay_fills_current_official_questions(self):
        parsed = parse_questions(PDF_SNIPPET)
        executive = {
            "president": "Donald J. Trump",
            "vice_president": "JD Vance",
            "speaker_of_the_house": "Mike Johnson",
            "chief_justice": "John G. Roberts, Jr.",
        }
        apply_current_officials(parsed, executive)
        self.assertEqual(parsed["30"]["answers"], ["Mike Johnson"])
        self.assertEqual(parsed["38"]["answers"], ["Donald J. Trump"])
        self.assertEqual(parsed["39"]["answers"], ["JD Vance"])
        self.assertEqual(parsed["57"]["answers"], ["John G. Roberts, Jr."])


if __name__ == "__main__":
    unittest.main()
