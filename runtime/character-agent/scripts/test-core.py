import sys
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'packages'/'brain'),str(ROOT/'packages'/'conversation')]
from core import Plan,Step,parse_plan
from store import ConversationStore

class Tests(unittest.TestCase):
    def test_free_motion_and_extra_fields(self):
        p=Plan(reply='你好',emotion='happy',annotation='unused',steps=[{'action':'new_dance','prompt':'A woman dances gracefully.','emotion':'happy','extra':'ignored'}])
        self.assertEqual(p.steps[0].motion_prompt,'A woman dances gracefully.')
        self.assertEqual(p.steps[0].emotion,'happy')
    def test_error_has_field_path(self):
        with self.assertRaises(ValueError) as caught:Plan(reply='hello',steps=[{'prompt':'A person stands.','duration_s':-3}])
        self.assertIn('steps.0.duration_s',str(caught.exception))
    def test_unsupported_navigation_is_not_silently_faked(self):
        with self.assertRaises(ValueError):Step(action='move_to',target=[1,2])
    def test_history_survives_new_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'history.db'
            first=ConversationStore(path);first.append('user','hello');first.append('assistant','你好')
            second=ConversationStore(path)
            self.assertEqual([m['content'] for m in second.recent()],['hello','你好'])
            first.db.close();second.db.close()
    def test_fenced_plan(self):
        self.assertEqual(parse_plan('```json\n{"reply":"hello","mode":"keep","steps":[]}\n```').mode,'keep')
if __name__=='__main__':unittest.main()
