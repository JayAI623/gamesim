import os
import unittest

from gamesim import templates

ROUND_TEMPLATES = ("entity.md", "game_master.md", "gm_commit.md")
SETUP_TEMPLATES = ("assemble.md", "conditions.md", "result_summary.md", "utility_research.md")
HORMUZ_WORDS = ("海峡", "伊朗", "美伊", "美国", "IRGC", "p_recovery", "停火", "H ≥ 95", "H、B、W", "6 个月")


class TestPrompts(unittest.TestCase):
    def test_setup_templates_render_without_leftovers(self):
        for name in SETUP_TEMPLATES:
            body = templates.template_body(name)
            names = templates.placeholders(body)
            self.assertTrue(names, name)
            out = templates.render(name, {k: f"<{k}>" for k in names})
            self.assertNotIn("{{", out, name)
            self.assertFalse(out.startswith("#  ") or out.startswith("> "), name)

    def test_missing_placeholder_raises(self):
        with self.assertRaises(KeyError):
            templates.render("result_summary.md", {"question": "q"})

    def test_single_pass_fill(self):
        self.assertEqual(templates.fill("{{a}}-{{b}}", {"a": "{{b}}", "b": "x"}), "{{b}}-x")

    def test_round_templates_have_no_scenario_hardcoding(self):
        for name in ROUND_TEMPLATES:
            with open(os.path.join(templates.PROMPTS_DIR, name), encoding="utf-8") as f:
                txt = f.read()
            for w in HORMUZ_WORDS:
                self.assertNotIn(w, txt, f"{name} still contains {w!r}")

    def test_setup_templates_have_no_hormuz_specifics(self):
        for name in SETUP_TEMPLATES:
            with open(os.path.join(templates.PROMPTS_DIR, name), encoding="utf-8") as f:
                txt = f.read()
            for w in ("海峡", "伊朗", "IRGC", "p_recovery", "2026-02-28", "1979"):
                self.assertNotIn(w, txt, f"{name} still contains {w!r}")

    def test_utility_research_keeps_markdown_heading(self):
        out = templates.render("utility_research.md",
                               {k: "X" for k in templates.placeholders(templates.template_body("utility_research.md"))})
        self.assertIn("# X 效用函数（显示偏好）", out)

    def test_stdout_prompts_forbid_tools(self):
        for name in ("assemble.md", "conditions.md", "result_summary.md"):
            self.assertIn("不要调用任何工具", templates.template_body(name), name)
            self.assertIn("只输出一个 JSON 代码块", templates.template_body(name), name)


if __name__ == "__main__":
    unittest.main()
