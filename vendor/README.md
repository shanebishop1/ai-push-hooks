# Bundled Python Dependency

The npm package includes the unmodified pure-Python wheel for [Tomli](https://pypi.org/project/tomli/2.4.0/). Python 3.10 uses it to parse TOML; Python 3.11+ uses the standard-library `tomllib` module.

The wrapper adds the wheel to `PYTHONPATH`, so no pip install, install script, or runtime download is needed. The wheel includes Tomli's MIT license under `tomli-2.4.0.dist-info/licenses/LICENSE`.

To reproduce the bundled download from the repository root:

```bash
python -m pip download --index-url https://pypi.org/simple --require-hashes \
  --only-binary=:all: --no-deps --platform any --implementation py --abi none \
  --python-version 3.10 --dest vendor --requirement vendor/requirements.txt
```

When updating, verify the pure-Python wheel's SHA-256 against PyPI, update the pin and wrapper path, and run the npm installed-hook tests on Python 3.10.
