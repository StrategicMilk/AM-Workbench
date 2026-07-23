"""
Test Template: New Skill / Tool

Copy alongside new_skill_template.py and replace:
    MySkill       → YourSkillName
    sample_skill  → your_skill_name
    my-skill      → your-skill-name

Save to: tests/test_your_skill_name.py.
The import path below is illustrative; replace it before executing the
generated test file.
"""

import unittest
from importlib import import_module
from unittest.mock import MagicMock, patch

# TODO: update this import before running generated tests.
MODULE_UNDER_TEST = "vetinari.skills.your_skill_name"
_TEMPLATE_SKIP: str | None = None

if "your_skill_name" in MODULE_UNDER_TEST:
    _TEMPLATE_SKIP = "Replace MODULE_UNDER_TEST before executing generated skill tests."
    MySkill = MySkillInput = MySkillOutput = MySkillCapability = MySkillTool = None
else:
    try:
        _skill_module = import_module(MODULE_UNDER_TEST)
    except ModuleNotFoundError as exc:
        _TEMPLATE_SKIP = f"Skill module {MODULE_UNDER_TEST!r} is not importable: {exc}"
        MySkill = MySkillInput = MySkillOutput = MySkillCapability = MySkillTool = None
    else:
        MySkill = _skill_module.MySkill
        MySkillInput = _skill_module.MySkillInput
        MySkillOutput = _skill_module.MySkillOutput
        MySkillCapability = _skill_module.MySkillCapability
        MySkillTool = _skill_module.MySkillTool


def setUpModule():
    if _TEMPLATE_SKIP:
        raise unittest.SkipTest(_TEMPLATE_SKIP)


class TestMySkillInput(unittest.TestCase):
    """Validate the input schema."""

    def test_valid_input_no_errors(self):
        inp = MySkillInput(target="some_target")
        self.assertEqual(inp.validate(), [])

    def test_missing_target_is_invalid(self):
        inp = MySkillInput(target="")
        errors = inp.validate()
        self.assertGreater(len(errors), 0)


class TestMySkillOutput(unittest.TestCase):
    def test_to_dict_keys(self):
        out = MySkillOutput(success=True, result="done")
        d = out.to_dict()
        for k in ("success", "result", "metadata"):
            self.assertIn(k, d)


class TestMySkill(unittest.TestCase):
    """Core logic tests — no external dependencies."""

    def setUp(self):
        self.skill = MySkill()

    def test_operation_a_succeeds(self):
        inp = MySkillInput(capability=MySkillCapability.OPERATION_A, target="foo")
        out = self.skill.run(inp)
        self.assertTrue(out.success)

    def test_operation_b_succeeds(self):
        inp = MySkillInput(capability=MySkillCapability.OPERATION_B, target="bar")
        out = self.skill.run(inp)
        self.assertTrue(out.success)

    def test_invalid_input_returns_failure(self):
        inp = MySkillInput(target="")   # missing required field
        out = self.skill.run(inp)
        self.assertFalse(out.success)
        self.assertIn("errors", out.metadata)

    # TODO: add capability-specific tests


class TestMySkillTool(unittest.TestCase):
    """Tool interface contract tests."""

    def setUp(self):
        self.tool = MySkillTool()

    def test_execute_returns_tool_result(self):
        from vetinari.tool_interface import ToolResult
        result = self.tool.execute(target="test_target")
        self.assertIsInstance(result, ToolResult)

    def test_execute_success(self):
        result = self.tool.execute(target="valid_target")
        self.assertTrue(result.success)

    def test_execute_missing_target_fails(self):
        result = self.tool.execute()   # no target
        self.assertFalse(result.success)

    def test_get_schema_valid(self):
        schema = MySkillTool.get_schema()
        self.assertIn("properties", schema)
        self.assertIn("target", schema["properties"])

    def test_tool_name_and_version(self):
        self.assertEqual(MySkillTool.NAME, "my-skill")
        self.assertTrue(MySkillTool.VERSION)

    # TODO: add mock tests for external calls
    # Example:
    # @patch("vetinari.skills.your_skill_name.some_external_call")
    # def test_external_call_mock(self, mock_call):
    #     mock_call.return_value = "mocked"
    #     result = self.tool.execute(target="x")
    #     mock_call.assert_called_once()


if __name__ == "__main__":
    unittest.main()
