import json
import unittest

from monitor import find_changes, maybe_simulate


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
                "president": ["Donald J. Trump"],
                "vice_president": ["JD Vance"],
                "speaker_of_the_house": ["Mike Johnson"],
                "chief_justice": ["John Roberts"],
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


if __name__ == "__main__":
    unittest.main()
