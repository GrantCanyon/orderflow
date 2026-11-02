"""Отказы до подключения и запрет перезаписи результатов."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class Guards(unittest.TestCase):
    def module(self):
        path = ROOT / "scripts/query_plans.py"
        self.assertTrue(path.is_file(), "Измеритель ещё не перенесён")
        spec = importlib.util.spec_from_file_location("query_plans", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_url_guards(self):
        module = self.module()
        for url in (
            "sqlite:///a",
            "postgresql://u:p@remote.invalid/x_test",
            "postgresql://u:p@127.0.0.1/production",
            "postgresql://u:p@127.0.0.1/x_test?host=remote.invalid",
            "postgresql://u:p@127.0.0.1/x_test?options=-csearch_path=public",
            "postgresql:///x_test",
        ):
            with self.subTest(url=url.split(":")[0]):
                with self.assertRaises(module.ProbeError):
                    module.validate_url(url)
        self.assertEqual(
            module.validate_url("postgresql://u:p@127.0.0.1/x_test").database, "x_test"
        )

    def test_libpq_overrides_are_rejected(self):
        module = self.module()
        for name in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS"):
            with self.subTest(variable=name):
                with patch.dict("os.environ", {name: "synthetic_override"}):
                    with self.assertRaises(module.ProbeError):
                        module.validate_url("postgresql://u:p@127.0.0.1/x_test")

    def test_output_guards(self):
        module = self.module()
        with self.assertRaises(module.ProbeError):
            module.prepare_output("../escape", False)
        with self.assertRaises(module.ProbeError):
            module.prepare_output(str(ROOT), False)
        artifacts = ROOT / "artifacts"
        artifacts.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="test_query_plans_", dir=artifacts) as folder:
            path = Path(folder)
            self.assertEqual(module.prepare_output(str(path), True), path.resolve())
            (path / "keep.txt").write_text("сохранить", encoding="utf-8")
            with self.assertRaises(module.ProbeError):
                module.prepare_output(str(path), True)
            self.assertEqual((path / "keep.txt").read_text(encoding="utf-8"), "сохранить")


if __name__ == "__main__":
    unittest.main()
