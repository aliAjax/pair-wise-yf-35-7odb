import unittest


from src.domain import Actor, PermissionDenied, ValidationError
from src.rules import RuleEngine


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RuleEngine()
        self.admin = Actor("rule-tester", "admin")

    def test_rule_calculation_or_validation(self):
        athlete = self.rules.validate_create(self.admin, "athletes", {"name": "A", "discipline": "cycling"})
        self.assertEqual(athlete["discipline"], "cycling")
        with self.assertRaises(ValidationError):
            self.rules.validate_create(self.admin, "athletes", {"name": "A"})


if __name__ == "__main__":
    unittest.main()
