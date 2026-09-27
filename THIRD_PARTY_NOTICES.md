# Third-party dependency notices

This inventory covers the direct packages declared for this application. The application itself is licensed under GNU GPL v3.0 only (`GPL-3.0-only`); these notices cover its dependencies separately.

## Runtime profiling dependencies

- **pandas** (`>=2.2,<3.0`) — BSD 3-Clause. Upstream license: <https://github.com/pandas-dev/pandas/blob/main/LICENSE>.
- **openpyxl** (`>=3.1,<4.0`) — MIT/Expat. Upstream documentation and license statement: <https://openpyxl.readthedocs.io/en/stable/>.

The desktop UI and basic directory scan use Python's standard library. The packages above enable enhanced local data profiling; the profiler may resolve additional transitive packages at installation time.

## Optional executable build dependency

- **PyInstaller** (`>=6.0,<7.0`) — GPL-2.0-or-later with the PyInstaller Bootloader Exception. Upstream license: <https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt>.

The build environment may resolve additional transitive packages. Before redistributing a bundled executable, review and preserve notices for the exact resolved packages and bundled components. NumPy's upstream project license is BSD 3-Clause; its binary wheels can also contain separately licensed components: <https://github.com/numpy/numpy/blob/main/LICENSE.txt> and <https://github.com/numpy/numpy/blob/main/pyproject.toml>.

## Research and market data

Third-party software licenses do not grant rights to research documents or licensed datasets. Do not add licensed data, exports, database copies, or paper materials to this source repository.
