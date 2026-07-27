import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from connector.FiFWebClient import FiFWebClient


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Request:
    def __init__(self, payload):
        self._payload = payload

    def fetch(self, *args, **kwargs):
        return _Response(self._payload)


class _Page:
    def __init__(self, payload):
        self.request = _Request(payload)


class LevelAnswerTest(unittest.TestCase):
    def test_t5_only_returns_the_selected_candidate_role(self) -> None:
        payload = {
            "status": 1,
            "data": {
                "content": {
                    "moshi": [
                        {
                            "name": "挑战",
                            "showtype": "t5",
                            "question": {
                                "qcontent": {
                                    "sample": "m4",
                                    "item": [
                                        {
                                            "questions": [
                                                {
                                                    "title": "examiner question",
                                                    "photo": "m1",
                                                    "sample": "m1",
                                                },
                                                {
                                                    "title": "candidate answer",
                                                    "photo": "m4",
                                                    "sample": "m4",
                                                },
                                            ]
                                        }
                                    ],
                                }
                            },
                        }
                    ]
                }
            },
        }
        client = FiFWebClient.__new__(FiFWebClient)
        client.user_auth = {"token": "test", "source": "test"}

        answers = client.get_level_answer(_Page(payload), "level-id")

        self.assertEqual(answers, ["candidate answer"])

    def test_t12_answers_follow_the_role_swap_order(self) -> None:
        questions = []
        for line in range(1, 5):
            questions.extend(
                [
                    {
                        "title": f"role-w6-line-{line}",
                        "photo": "w6",
                        "sample": "w6",
                    },
                    {
                        "title": f"role-m2-line-{line}",
                        "photo": "m2",
                        "sample": "m2",
                    },
                ]
            )

        payload = {
            "status": 1,
            "data": {
                "content": {
                    "moshi": [
                        {
                            "name": "挑战",
                            "showtype": "t12",
                            "question": {
                                "qcontent": {
                                    "sample": "w6#m2",
                                    "item": [{"questions": questions}],
                                }
                            },
                        }
                    ]
                }
            },
        }
        client = FiFWebClient.__new__(FiFWebClient)
        client.user_auth = {"token": "test", "source": "test"}

        answers = client.get_level_answer(_Page(payload), "level-id")

        expected = [f"role-w6-line-{line}" for line in range(1, 5)]
        expected.extend(f"role-m2-line-{line}" for line in range(1, 5))
        self.assertEqual(answers, expected)

    def test_t8_all_sentences_are_combined_into_one_recording(self) -> None:
        items = [
            {
                "questions": [
                    {"title": "opening sentence"},
                    {"title": "contrast sentence"},
                ]
            },
            {"questions": [{"title": "analysis sentence"}]},
            {"questions": [{"title": "summary sentence"}]},
        ]
        payload = {
            "status": 1,
            "data": {
                "content": {
                    "moshi": [
                        {
                            "name": "挑战",
                            "showtype": "t8",
                            "question": {"qcontent": {"item": items}},
                        }
                    ]
                }
            },
        }
        client = FiFWebClient.__new__(FiFWebClient)
        client.user_auth = {"token": "test", "source": "test"}

        answers = client.get_level_answer(_Page(payload), "level-id")

        self.assertEqual(
            answers,
            ["opening sentence contrast sentence analysis sentence summary sentence"],
        )

    def test_t9_answers_are_grouped_by_recording_time(self) -> None:
        counts = [1, 3, 4, 2, 1]
        items = []
        expected = []
        for group_index, count in enumerate(counts, start=1):
            titles = [f"group-{group_index}-line-{line}" for line in range(1, count + 1)]
            items.append(
                {
                    "questions": [
                        {
                            "title": title,
                            "recordingTime": f"{group_index}#{group_index}",
                        }
                        for title in titles
                    ]
                }
            )
            expected.append(" ".join(titles))

        payload = {
            "status": 1,
            "data": {
                "content": {
                    "moshi": [
                        {
                            "name": "挑战",
                            "showtype": "t9",
                            "question": {"qcontent": {"item": items}},
                        }
                    ]
                }
            },
        }
        client = FiFWebClient.__new__(FiFWebClient)
        client.user_auth = {"token": "test", "source": "test"}

        answers = client.get_level_answer(_Page(payload), "level-id")

        self.assertEqual(answers, expected)


if __name__ == "__main__":
    unittest.main()
