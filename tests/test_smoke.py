from click.testing import CliRunner

from curator import __version__
from curator.cli import cli


def test_version():
    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output
