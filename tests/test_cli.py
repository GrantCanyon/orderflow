import pytest

from orderflow.cli import main


@pytest.mark.parametrize(
    "arguments, expected_code, expected_text",
    [
        (["--help"], 0, "Показать справку"),
        (["unknown"], 2, "недопустимый вариант"),
        (["serve", "--port", "abc"], 2, "недопустимое целое число"),
    ],
)
def test_cli_help_and_errors_are_russian(
    monkeypatch, capsys, arguments, expected_code, expected_text
):
    monkeypatch.setattr("sys.argv", ["orderflow", *arguments])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == expected_code
    captured = capsys.readouterr()
    assert expected_text in captured.out + captured.err
    assert "Использование:" in captured.out + captured.err
    assert "usage:" not in captured.out + captured.err
