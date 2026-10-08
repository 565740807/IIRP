"""The hard-coded interface text check finds real text and ignores code and comments."""

from scripts import check_ui_text

SAMPLE = """
import { useTranslation } from "react-i18next";
// 注释里的中文 and English sentences are fine.
/** "Director, President and CEO" in a doc comment. */
const pattern = /[a-z.'&-]*/g;
export function Page() {
  const label = "Something went wrong.";
  return (
    <div className="flex items-center gap-2" title={t("ui.page.title")}>
      <p>加载中</p>
      <p>Loading your data</p>
      <input placeholder="Enter a ticker" />
      <input placeholder="MSFT, AAPL, NVDA" />
      {value > 0 ? <Up /> : "—"}
      <span>English</span>
      <code>{`${name} ${email}`}</code>
      <p>{text}</p>
    </div>
  );
}
"""


def test_flags_text_and_ignores_code(tmp_path):
    source = tmp_path / "Page.tsx"
    source.write_text(SAMPLE)
    found = {value for _, value in check_ui_text.problems(source)}
    assert found == {"Something went wrong.", "加载中", "Loading your data", "Enter a ticker"}


def test_current_frontend_source_is_clean():
    assert check_ui_text.main() == 0
