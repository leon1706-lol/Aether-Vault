import pytest
from click.testing import CliRunner

from python.av_cli.main import cli


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """An initialized .av repository in a temp directory, with cwd set to it."""
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["init"])
    assert result.exit_code == 0, result.output
    return tmp_path
