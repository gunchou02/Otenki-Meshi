import unittest
from unittest.mock import patch

import recommender


class RecommenderTest(unittest.TestCase):
    def test_catalog_is_loaded_from_json_with_categories(self):
        self.assertGreaterEqual(len(recommender.CANDIDATES), 60)
        self.assertTrue(all("category" in c for c in recommender.CANDIDATES))

    def test_hot_humid_lunch_prefers_refreshing_keywords(self):
        with patch("recommender.random.random", return_value=0):
            result = recommender.recommend(
                temp=32,
                humidity=85,
                weather="Clear",
                hour=12,
                weekday=2,
                top_k=8,
            )

        self.assertIn(
            result["keyword"],
            {"冷麺", "冷やし中華", "そうめん", "タイ料理", "海鮮丼", "サラダボウル"},
        )
        self.assertIn("暑さ", result["reason"])
        self.assertIn("search_keywords", result)

    def test_rainy_lunch_uses_shorter_search_range(self):
        result = recommender.recommend(
            temp=18,
            humidity=75,
            weather="Rain",
            hour=12,
            weekday=1,
        )

        self.assertEqual(result["search_range"], 2)
        self.assertIn("ranked_keywords", result)
        self.assertEqual(
            len(result["search_keywords"]),
            len({recommender.get_candidate(k)["category"] for k in result["search_keywords"]}),
        )

    def test_recent_category_is_penalized(self):
        no_recent = recommender.recommend(
            temp=4,
            humidity=55,
            weather="Snow",
            hour=19,
            weekday=3,
            recent=[],
            top_k=5,
        )
        recent_top = no_recent["ranked_candidates"][0]
        with_recent = recommender.recommend(
            temp=4,
            humidity=55,
            weather="Snow",
            hour=19,
            weekday=3,
            recent=[recent_top["keyword"]],
            top_k=5,
        )

        self.assertNotEqual(
            recent_top["category"],
            with_recent["ranked_candidates"][0]["category"],
        )
        self.assertNotIn(
            recent_top["category"],
            {
                recommender.get_candidate(keyword)["category"]
                for keyword in with_recent["search_keywords"][:3]
            },
        )


if __name__ == "__main__":
    unittest.main()
