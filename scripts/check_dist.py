"""Check release contents without importing the package or accessing an account."""

import argparse
from email.parser import BytesParser
import hashlib
from pathlib import Path, PurePosixPath
import re
import tarfile
import zipfile


def check(directory):
    wheels, sources = list(directory.glob('*.whl')), list(directory.glob('*.tar.gz'))
    if len(wheels) != 1 or len(sources) != 1:
        raise ValueError('Expected exactly one wheel and one source archive.')
    root = Path(__file__).resolve().parents[1]
    expected_code = {p.relative_to(root / 'src').as_posix() for p in (root / 'src/quota_window').glob('*.py')}
    expected_version = re.search(r'__version__ = "([^"]+)"',
                                 (root / 'src/quota_window/__init__.py').read_text()).group(1)
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Wheel has duplicate members.')
        metadata_name = f'codex_nudge-{expected_version}.dist-info'
        allowed_metadata = {'METADATA', 'WHEEL', 'entry_points.txt', 'RECORD', 'licenses/LICENSE'}
        for name in names:
            if name not in expected_code and name not in {f'{metadata_name}/{n}' for n in allowed_metadata}:
                raise ValueError(f'Unexpected wheel member: {name}')
        if not expected_code <= set(names):
            raise ValueError('Wheel is missing source modules.')
        metadata = BytesParser().parsebytes(wheel.read(metadata_name + '/METADATA'))
        if (metadata['Name'] != 'codex-nudge' or metadata['Version'] != expected_version
                or metadata['Requires-Python'] != '>=3.10' or metadata['License-Expression'] != 'MIT'):
            raise ValueError('Wheel metadata differs from release settings.')
        entry = wheel.read(metadata_name + '/entry_points.txt').decode()
        if 'codex-nudge = quota_window.cli:main' not in entry:
            raise ValueError('Console entry point is missing.')
        if wheel.read('quota_window/__init__.py') != (root / 'src/quota_window/__init__.py').read_bytes():
            raise ValueError('Wheel runtime version differs from source.')
    top_files = {'README.md', 'LICENSE', 'AGENTS.md', 'CONTRIBUTING.md', 'CHANGELOG.md',
                 'pyproject.toml', 'requirements.txt', 'PKG-INFO', '.gitignore'}
    with tarfile.open(sources[0], 'r:gz') as source:
        members = source.getmembers()
        if len({m.name for m in members}) != len(members):
            raise ValueError('Source archive has duplicate members.')
        expected_prefix = f'codex_nudge-{expected_version}'
        actual = {str(PurePosixPath(*PurePosixPath(m.name).parts[1:])) for m in members}
        required = {'pyproject.toml', 'README.md', 'LICENSE', 'PKG-INFO', 'scripts/check_dist.py'}
        if not required.union({'src/' + p for p in expected_code}) <= actual:
            raise ValueError('Source archive is missing release files.')
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts or name.parts[0] != expected_prefix:
                raise ValueError('Source archive has an unsafe path.')
            if not member.isfile():
                raise ValueError('Source archive must contain only regular files.')
            relative = PurePosixPath(*name.parts[1:])
            value = str(relative)
            allowed = (value in top_files or value in {'src/' + p for p in expected_code}
                       or len(relative.parts) == 2 and relative.parts[0] == 'docs' and relative.suffix == '.md'
                       or len(relative.parts) == 2 and relative.parts[0] == 'tests'
                       and relative.name.startswith('test_') and relative.suffix == '.py'
                       or len(relative.parts) == 2 and relative.parts[0] == 'scripts' and relative.suffix == '.py')
            if not allowed:
                raise ValueError(f'Unexpected source member: {relative}')
    artifacts = (wheels[0], sources[0])
    (directory / "SHA256SUMS").write_text("".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in artifacts))
    for artifact in artifacts:
        print(f'{artifact.name}: {artifact.stat().st_size} bytes')
    print('Release contents and metadata verified.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    check(parser.parse_args().directory)
