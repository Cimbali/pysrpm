Make RPM files from python releases

Imagined as a drop-in replacement for setuptools’s `bdist_rpm` command ([being deprecated](https://github.com/pypa/setuptools/issues/1988)),
this package does much of the same things than [pyp2rpm](https://github.com/fedora-python/pyp2rpm) does, except simpler.

In particular all the parsing of dependencies, package build systems, versions, etc. is done by external libraries,
such as [packaging](packaging.pypa.io/) and [pep517](https://pep517.readthedocs.io/en/latest/)

All configurability is done through a config file, that can be overrided with a user config file,
which will at some point include the option to use the project’s setup.cfg or pyproject.toml.

Dependencies can be extracted automatically from the package metadata.

Running pysrpm to generate the package requires
