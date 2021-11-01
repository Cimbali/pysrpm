# Make RPM files from python releases

Imagined as a drop-in replacement for setuptools’s `bdist_rpm` command ([being deprecated](https://github.com/pypa/setuptools/issues/1988)),
this package does much of the same things than [pyp2rpm](https://github.com/fedora-python/pyp2rpm) does, except simpler.
In particular all the parsing of dependencies, package build systems, versions, etc. is done by external libraries,
such as [packaging](packaging.pypa.io/) and [pep517](https://pep517.readthedocs.io/en/latest/).

At its simplest:
```bash
$ pysrpm package-release.tar.gz
```
or
```bash
$ pysrpm package-source-directory/
```

## Configuration

You can override any options from the command line, see `pysrpm --help` for a full list.
The system is building the specfile purely by interpolating (possibly inherited) templates. With time, more system-specific templates will be available. You can choose them with the `flavour` option, e.g.:

```bash
$ pysrpm --flavour fedora package-release.tar.gz
```
or
```bash
$ cat pysrpm.cfg
[pysrpm]
flavour = fedora
$ pysrpm --config pysrpm.cfg package-release.tar.gz
```

### Templating
Most configurability is done through config files however, that can be provided on the command line, or options can be fecthed from the project’s `setup.cfg` (under `[pysrpm]` and `[pysrpm.*]`), or the project’s `pyproject.toml` (under `tool.pysrpm`).

Any new sections under `pysrpm` can become a new flavour, and can inherit from any existing template. E.g. in a project’s `setup.cfg`:
```ini
[pysrpm]
flavour = add_post
[pysrpm.add_post]
# Pick a parent (defaults to base if absent)
inherits = base
# Add a post script
post =
	rm -rf $$$$RPM_BUILD_ROOT
```
See [`presets/00-base.conf`](pysrpm/presets/00-base.conf) for an explanation of available parameters.

Currently there are no other presents than the `base` template, but specialisations can be added if needed, for example for platforms that are stuck on older rpm versions.

### Escaping characters
To make templates extendable, interpolation is used. This means some characters need to be protected (by doubling them) if they aren’t meant as interpolation.

Up to 3 levels of expansion can happen:
- templates are evaluating as python, which requires escaping any `{` and `}`
- this project uses configparser `ExtendedInterpolation` notation, i.e. `${key}` or `${section:key}`, which requires escaping `$`
- RPM uses `%{macro}` for its macros, which requires escaping `%`
- `setup.cfg` uses configparser `BasicInterpolation` notation, i.e. `%(key)s` interpolation, which requires escaping `%` if using the project’s `setup.cfg`
- `pyproject.toml` has backslahes for double quotes in double-quoted strings, which requires escaping `\` if using double-quoted strings in toml

Here are a couple of examples:
|                | example specfile result | toml config                     | setup.cfg                     |
| -------------- | ----------------------- | ------------------------------- | ----------------------------- |
| package version|  1.2.3a4                | `val = "{version}"`             | `val = {version}`             |
| parent template|  value from `[base]`    | `val = "${base:key}"`           | `val = ${base:key}`           |
| shell variable | `$RPM_BUILD_ROOT`       | `val = "$$RPM_BUILD_ROOT"`      | `val = $$RPM_BUILD_ROOT`      |
| RPM macro      | `%define foo 1`         | `val = "%%define foo 1"`        | `val = %%define foo 1`        |
| RPM macro      | `%{error:message}`      | `val = "%%{{error:message}}"`   | `val = %%{{error:message}}"`  |
| A literal %    | `echo "100%%"`          | `val = 'echo "100%%"'`          | `val = echo "100%%%%"`        |
| A literal `\`  | `printf '%s\n' foo`     | `val = "printf '%s\\n' foo"`    | `val = printf '%s\n' foo`     |



## RPM dependency and automatic generation

Dependencies can be extracted automatically from the package metadata, but only for python packages.
Expressing complex dependency requirements − especially conditional ones − may be a little shaky currently.
Dependencies on other python packages are expressed by default as `python3Xdist(package)` with `X` the version. This is controlled by the `python_package` template − see [`presets/00-base.conf`](pysrpm/presets/00-base.conf) for more options.

Dependencies can also be specified by the `requires` and `suggests` template entries.
