import unittest
from returnlight_app.returnlight import CaseError, validate_model_review

class CategorySchemaTests(unittest.TestCase):
    def test_non_string_categories_are_recoverable_validation_errors(self):
        for category in ([], {}, None, True, 1):
            with self.subTest(category=category), self.assertRaisesRegex(CaseError, "Invalid model category"):
                validate_model_review({"category": category, "summary": "The cup broke.", "quote": "The cup broke."}, "The cup broke.")

    def test_valid_category_retains_grounded_review(self):
        review = {"category": "damaged", "summary": "The cup broke.", "quote": "The cup broke."}
        self.assertEqual(validate_model_review(review, "The cup broke."), review)
