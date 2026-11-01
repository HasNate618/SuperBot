"""The dashboard preview must mount on elements the client script can bind.

Regression test: the game-cam UI once shipped an `<img id="preview">` while
app.js still dereferenced `#vnc-auth`/`#mute-audio`, so the script threw on
load and no preview (or state polling) ever ran. Every id the script binds
directly must exist in index.html, and the preview mount must be a container
noVNC can attach a canvas to.
"""

import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "clank" / "static"


def _bound_ids(script: str) -> set[str]:
    return set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', script))


def _directly_bound_ids(script: str) -> set[str]:
    bound = _bound_ids(script)
    direct = set()
    for element_id in bound:
        if re.search(
            r'\$\("' + re.escape(element_id) + r'"\)\s*\.\s*(addEventListener|onclick|onchange)\s*(\(|=)',
            script,
        ):
            direct.add(element_id)
    return direct


class PreviewUiContractTest(unittest.TestCase):
    def test_html_provides_every_directly_bound_element(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        missing = sorted(
            element_id
            for element_id in _directly_bound_ids(script)
            if f'id="{element_id}"' not in html
        )
        self.assertEqual(missing, [])

    def test_preview_mount_is_a_container(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertRegex(html, r'<div[^>]*\bid="preview"')
        self.assertNotRegex(html, r'<img[^>]*\bid="preview"')


if __name__ == "__main__":
    unittest.main()
