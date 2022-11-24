""" Main module containing :class:`~RPM`, the package-to-spec (or RPM) converter """
import os
import re
import sys
import tomli
import email
import shutil
import tarfile
import pathlib
import fnmatch
import tempfile
import functools
import subprocess
import contextlib
import configparser
import pep517.meta
import pep517.build
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from collections import OrderedDict

try:
    import importlib.resources as importlib_resources
except ImportError:
    import importlib_resources

try:
    import importlib.metadata as importlib_metadata
except ImportError:
    import importlib_metadata

from pysrpm.convert import specifier_to_rpm_version, simplify_marker_to_rpm_condition, python_version_to_rpm_version


class RPMBuildError(Exception):
    """ rpmbuild encountered an error """
    pass


class RPM:
    """ Given a python source distribution and a template config, build source, binary, or spec RPM files """
    def __init__(self, source, config=None, templates={}, **options):
        """ Setup the various configurations and templates to start converting

        Args:
            source (`str` or path-like): Path to the source package to convert
            config (`str` or path-like): Path to a configuration file
            templates (`dict`): CLI-specified template items
        """
        self.config = configparser.ConfigParser(interpolation=configparser.ExtendedInterpolation(),
                                                dict_type=OrderedDict)
        with self.preset_configs() as config_lists:
            self.config.read(config_lists)

        self.config_file = config
        self.cli_options = options
        self.cli_templates = templates
        source = pathlib.Path(source)
        if not source.exists():
            raise FileNotFoundError(str(source))
        self.root = source if source.is_dir() else None
        self.source = source if source.is_file() else None


    @staticmethod
    @contextlib.contextmanager
    def preset_configs():
        """ Context manager that ensures the preset configurations are in a directory, and yields the sorted list """
        with importlib_resources.path('pysrpm', 'presets') as defaults_path:
            yield sorted(defaults_path.glob('*.conf'))


    def __enter__(self):
        """ Context manager to inspect the contents of a source package in a temporary non-archive directory

        We could for some cases simply inspect the file and load files from it without extracting them to a directory,
        but it’s a hassle to keep the several code paths. Now we uniquely manage source directories, tarballs that
        require full extraction, and tarballs that require only partial extraction.
        """
        if self.root is not None:
            return self

        if '.tar' not in self.source.suffixes[-2:]:
            raise ValueError('Expected tarball as source file')

        self.tempdir = pathlib.Path(tempfile.mkdtemp())
        with tarfile.open(self.source) as tf:
            for name in tf.getnames():
                if any(fnmatch.fnmatch(name, pat) for pat in ['*/pyproject.toml', '*/PKG-INFO', '*/setup.cfg',
                                                              '*/setup.py', '*/entry_points.txt']):
                    tf.extract(name, self.tempdir)
        try:
            self.root = next(self.tempdir.glob('*/PKG-INFO')).parent
        except StopIteration:
            raise ValueError('Malformed source file: can’t find metadata')
        return self


    def full_extraction(self):
        """ Ensures the source package is fully extracted to the temporary directory """
        if not hasattr(self, 'tempdir') or not self.source:
            return

        with tarfile.open(self.source) as tf:
            # Only extract files that will resolve within the destination directory − also skip existing files
            tempdir = self.tempdir.resolve()
            safe_members = [info for info, dest in ((info, (tempdir / info.name).resolve()) for info in tf.getmembers())
                            if dest.parts[:len(tempdir.parts)] == tempdir.parts and not dest.exists()]
            tf.extractall(self.tempdir, safe_members)


    def _make_directories(self):
        """ Ensure the necessary directories exist """
        self.dest_dir.mkdir(parents=True, exist_ok=True)
        if self.config.getboolean('pysrpm', 'spec_only'):
            return

        for d in ('SOURCES', 'SPECS', 'BUILD', 'RPMS', 'SRPMS'):
            self.rpm_base.joinpath(d).mkdir(parents=True, exist_ok=True)


    def __exit__(self, exc_type, exc_value, traceback):
        """ Cleanup any temporary directories and files we extracted """
        if not self.config.getboolean('pysrpm', 'keep_temp') and not self.config.getboolean('pysrpm', 'spec_only'):
            try:
                shutil.rmtree(self.rpm_base, ignore_errors=True)
            except AttributeError:
                pass  # Early exit? load_configuration() did not finish

        try:
            shutil.rmtree(self.tempdir)
        except AttributeError:
            pass  # We did not have to create a temporary directory


    def load_configuration(self):
        """ Configure the RPM building, loading the various configurations in order """
        options = self.config.options('pysrpm')
        # Load any config file, specified or from the package
        toml = self.root / 'pyproject.toml'
        cfg = self.root / 'setup.cfg'
        if self.config_file is not None:
            self.load_user_config(self.config_file, from_project=self.cli_options.pop('project_config', False))
        elif toml.exists() and 'pysrpm' in RPM.load_toml_config(toml).get('tool', {}).keys():
            self.load_user_config(toml, from_project=True)
        elif cfg.exists():
            self.load_user_config(cfg, from_project=True)

        if self.config.has_section('__templates__'):
            raise ValueError('The section name "__templates__" is reserved, do not use it in your configuration files')

        self.config.add_section('__templates__')
        for opt, arg in self.config.items('pysrpm')[:]:
            if opt not in options:
                self.config.remove_option('pysrpm', opt)
                self.config.set('__templates__', opt, arg)

        # Load CLI last as it needs to override previous options.
        self.config.read_dict({
            'pysrpm': {opt: arg for opt, arg in self.cli_options.items()},
            '__templates__': {opt: arg for opt, arg in self.cli_templates.items()},
        }, source='CLI')

        # Set config params from loaded config
        self.dest_dir = pathlib.Path(self.config.get('pysrpm', 'dest_dir'))
        self.rpm_base = pathlib.Path(self.config.get('pysrpm', 'rpm_base'))

        # Inherit from parent sections by copying raw templates down, which allows the interpolations to be re-evaluated
        self.config.set('__templates__', 'inherits', self.config.get('pysrpm', 'flavour', fallback='base'))
        for section in self.config.sections():
            if section in ['base', 'pysrpm']:
                continue

            parent = self.config.get(section, 'inherits', fallback='base')
            self.config.read_dict({section: {key: value for key, value in self.config.items(parent, raw=True) if not
                                             self.config.has_option(section, key)}}, 'inherited')

        self.templates = dict(self.config.items('__templates__'))
        self.templates['optional_keys'] = set(self.templates['optional_keys'].split())

        env_markers = (line.split(':', 1) for line in self.templates['environment_markers'].strip().split('\n'))
        self.environments = {key.strip(): val.strip() for key, val in env_markers}

        # Extract additional files if required to get the package metadata
        if self.config.getboolean('pysrpm', 'extract_dependencies'):
            self.full_extraction()


    @functools.cache
    @staticmethod
    def load_toml_config(path):
        """ Load a TOML file

        Args:
            path (`str` or path-like): Path to a toml file to load

        Returns:
            `dict` or `None`: A dictionary of file contents
        """
        with open(path, 'rb') as f:
            return tomli.load(f)


    def load_user_config(self, path, from_project=False):
        """ Load a config file into the RPM’s configparser, only evaluating interpolations in our own config parser

        Args:
            path (`str` or path-like): Path to a configuration file to load
            from_project (`bool`): Whether the config file is obtained from the project source

        Returns:
            `dict` or `None`: A dictionary of section name to section template
        """
        if pathlib.Path(path).suffix == '.toml':
            config = RPM.load_toml_config(path).get('tool', {}).get('pysrpm', {})
            # Move anything directy under 'pysrpm' into a nested table
            config['pysrpm'] = {key: config.pop(key) for key, value in config.copy().items() if type(value) is not dict}
        else:
            parser = configparser.ConfigParser() if from_project else configparser.RawConfigParser()
            parser.read(path)
            prefix = 'pysrpm.' if any(section.startswith('pysrpm.') for section in parser.sections()) else ''
            config = {cfgsec.replace(prefix, ''): dict(parser.items(cfgsec)) for cfgsec in parser.sections()
                      if cfgsec.startswith(prefix) or cfgsec == 'pysrpm'}

        self.config.read_dict(config, source=path)


    def load_source_metadata(self, root, with_deps=False):
        """ Extract package metadata from a python source package

        Ideally we could just read from the {package}-{version}/PKG-INFO file inside the tarball,
        but it’s unfortunately incomplete, see e.g. https://github.com/pypa/setuptools/issues/1716

        So in order to be as compliant as possible with any future build system enabled by pep517, just extract
        the full tarball and get the metadata from pep517.meta. This is of course much slower, as we extract the full
        package and the pep517 package ensures backends are installed, etc.

        Args:
            root (:class:`~pathlib.Path`): Path to a directory containing source distribution of the package
            with_deps (`bool`): Whether to extract

        Returns:
            `dict`: The metadata as a dictionary of keys to strings, or to lists of strings for multiple metadata
        """
        # Location of PKG-INFO for extracted tarballs, which is the main use case. If missing, get/rebuild the metadata
        pkg_info = root / 'PKG-INFO'

        # Also try usual location for setuptools develop packages
        if not pkg_info.exists():
            try:
                pkg_info = next(root.glob('*.egg-info/PKG-INFO'))
            except StopIteration:
                pass

        if with_deps or not pkg_info.exists():
            self.full_extraction()  # May be needed?
            dist = pep517.meta.load(root)
            dist_meta = dist.metadata
            entry_points = dist.entry_points
        else:
            # Metadata from importlib.metadata is an email.Message, so do the same here
            with open(pkg_info, 'rb') as f:
                dist_meta = email.message_from_binary_file(f)
            dist_meta.set_param('charset', 'utf8')

            # May not exist, only when egg-info is built (wheels, setuptools develop installs…)
            ep_parser = configparser.ConfigParser()
            try:
                ep_parser.read_file([str(root / 'entry_points.txt')])
            except configparser.MissingSectionHeaderError:
                pass
            entry_points = [importlib_metadata.EntryPoint(key, value, sec) for sec in ep_parser.sections()
                            for key, value in ep_parser.items(sec)]

        pyproject = root / 'pyproject.toml'
        self.build_system = {
            'requires': ['setuptools>=40.8.0', 'wheel'],
            'build-backend': 'setuptools.build_meta:__legacy__',
        }
        metadata = {
            'build-requires': self.build_system['requires'],
            'long-description': dist_meta.get_payload().replace('%', '%%'),
        }

        enabled_entry_points = []
        disabled_entry_points = []
        extra_patterns = self.templates['requires_extras'].split() + self.templates['suggests_extras'].split()
        for ep in entry_points:
            extras = re.match(r'.* \[(.+)\]$', ep.value.strip())
            for extra in ((extra.strip() for extra in extras.group(1).split(',')) if extras else []):
                if not any(fnmatch.fnmatch(extra, pattern) for pattern in extra_patterns):
                    # A specified extra on the entry point is not matching any of the extras included in the package
                    disabled_entry_points.append(f'"{ep.name}"' if ' ' in ep.name else ep.name)
                    break
            else:
                enabled_entry_points.append(f'"{ep.name}"' if ' ' in ep.name else ep.name)

        if enabled_entry_points:
            metadata['entry-points'] = ' '.join(enabled_entry_points)
        if disabled_entry_points:
            metadata['disabled-entry-points'] = ' '.join(disabled_entry_points)

        if pyproject.exists():
            self.build_system.update(RPM.load_toml_config(pyproject)['build-system'])
            metadata['build-requires'] = self.build_system.get('build-requires', metadata['build-requires'])
        # If running setup.py, we don’t need setuptools to be >= 40.8.0 as it is required for pyproject.toml:
        elif (root / 'setup.py').exists():
            metadata['build-requires'] = ['setuptools']

        # See https://packaging.python.org/specifications/core-metadata/
        multiple_use = {'dynamic', 'platform', 'supported-platform', 'classifier', 'requires-dist',
                        'requires-external', 'project-url', 'provides-extra', 'provides-dist', 'obsoletes-dist'}

        for key, value in ((key.lower(), value.replace('%', '%%')) for key, value in dist_meta.items()):
            if key == 'content-type':
                continue
            elif key in multiple_use:
                metadata.setdefault(key, []).append(value)
            else:
                metadata[key] = f'{metadata[key]} {value}' if key in metadata else value

        epoch, rpmversion = python_version_to_rpm_version(metadata['version']).rpartition(':')[::2]
        return {
            **metadata,
            'rpmname': self.templates['python_package'].format(name=re.sub('[._-]+', '-', metadata['name'].lower())),
            'rpmversion': rpmversion,
            'release': self.config.get('pysrpm', 'release'),
            'arch': self.templates['arch'],
            'sourcefile': self.templates['source_name'].format(**metadata),
            **({'epoch': epoch} if epoch != '' else {}),
        }


    def convert_python_req(self, reqs, extras=[], package_template='python_dist'):
        """ Compute the version-specified dependency list for a package

        Args:
            reqs (`list` of `str`): The string representations of python dependencies
            package_template (`str`): The name of the template to format package names
            extras (`list` of `str`): the list of extras to incldue in the requirement, to evaluate requirement markers

        Returns:
            `list`: A list of string representations for the package dependency with versions
        """
        rpm_reqs = []
        environments = {**self.environments, 'extra': extras}
        for req in (Requirement(req) for req in reqs):
            condition = simplify_marker_to_rpm_condition(req.marker, environments, self.templates)
            if condition is False:
                continue

            package = self.templates[package_template].format(name=req.name)
            versioned_package = specifier_to_rpm_version(package, req.specifier)

            if condition is True:
                rpm_reqs.append(versioned_package)
            else:
                rpm_reqs.append(f'{versioned_package} {condition}')

        return rpm_reqs


    def _format_lines(self, template, **kwargs):
        """ Split a template into lines and .format() them, returning a list of successfully formatted lines

        Lines with missing keys are removed, unless the keys are not marked optional in which case a KeyError is raised.

        Args:
            template (`str`): The template of a spec file section

        Returns:
            `list`: the successfully formatted lines
        """
        successful_lines = []
        for line in template.split('\n'):
            try:
                successful_lines.append(line.format(**kwargs))
            except KeyError as err:
                if not set(err.args) <= self.templates['optional_keys']:
                    raise KeyError(f'Missing template key(s) {", ".join(err.args)}') from err
            except ValueError as err:
                raise ValueError(f'Error formatting line: {line}') from err
        return successful_lines


    def make_spec(self, pkg_info):
        """ Build the spec file for this RPM according to package pkg_info and templates

        Args:
            pkg_info (`dict`): The information to interpolate in the template

        Returns:
            `str`: the contents of the spec file
        """
        spec = self._format_lines(self.templates['preamble'].lstrip('\n'), **pkg_info)
        # In BuildRequires, python3 is not yet installed, ensure we do not need its version macro by using a template
        # that is not python-version dependent (as python_dist is). RPM should figure out what to do regardless.
        spec.append('BuildRequires: ' + ', '.join(self.convert_python_req(pkg_info['build-requires'],
                                                                          package_template='python_package')))

        # Handle automatically extracting dependencies
        extract_deps = self.config.getboolean('pysrpm', 'extract_dependencies')
        deps = pkg_info.get('requires-dist', []) if extract_deps else []
        extras = pkg_info.get('provides-extra', []) if extract_deps else []

        requires_extras = {match for pattern in self.templates['requires_extras'].split()
                           for match in fnmatch.filter(extras, pattern)}
        suggests_extras = {match for pattern in self.templates['suggests_extras'].split()
                           for match in fnmatch.filter(extras, pattern)}

        # Generate required dependencies
        required = self.templates['requires'].replace('\n', ' ')
        required = ([required] if required else []) + self.convert_python_req(deps, requires_extras)
        python_version = self.templates['python_version'] or pkg_info.get('requires-python')
        if python_version:
            required.append(specifier_to_rpm_version('python(abi)', SpecifierSet(python_version)))
        if required:
            spec.extend(f'Requires: {req}' for req in required)

        # Generate optional dependencies
        optional = self.templates['suggests'].replace('\n', ' ')
        optional = ([optional] if optional else []) + self.convert_python_req(deps, suggests_extras)
        opt_key = self.templates['optional_dependency_tag']
        if opt_key and set(optional) - set(required):
            spec.extend(f'{opt_key}: {req}' for req in required if req not in required)

        # Generate the rest of the file
        sections = self.config.options('base')
        for section in sections[sections.index('preamble') + 1:]:
            section_spec = self._format_lines(self.templates[section], **pkg_info)
            if any(section_spec):
                spec.extend(['', f'%{section}{" " if section_spec[0] else ""}{section_spec[0]}', *section_spec[1:]])

        return '\n'.join(spec)


    def _copy(self, orig, dest):
        """ Copy file from orig to dest, replacing if it exists, hard-linking if possible

        Args:
            orig (`pathlib.Path`): original file, must exst
            dest (`pathlib.Path`): destination path
        """
        if dest.is_dir():
            dest = dest / orig.name
        if dest.exists():
            dest.unlink()
        try:
            os.link(orig, dest)
        except Exception:
            shutil.copy2(orig, dest)


    def run(self):
        """ Create the requested files: either spec, source RPM, and/or binary RPM """
        self.load_configuration()
        pkg_info = self.load_source_metadata(self.root, self.config.getboolean('pysrpm', 'extract_dependencies'))

        # Start by building the spec
        spec = self.make_spec(pkg_info)

        dry_run = self.config.getboolean('pysrpm', 'dry_run')
        # Priority of options: if spec_only is set the other 2 are ignored,
        # if binary_only then source_only (which is set by default) is ignored
        spec_only = self.config.getboolean('pysrpm', 'spec_only')
        binary_only = self.config.getboolean('pysrpm', 'binary_only')
        source_only = self.config.getboolean('pysrpm', 'source_only')

        if spec_only and dry_run:
            print(spec)
            return

        self._make_directories()

        spec_file = (self.dest_dir if spec_only else self.rpm_base / 'SPECS') / f'{pkg_info["rpmname"]}.spec'
        with open(spec_file, 'w') as f:
            print(spec, file=f)

        if spec_only:
            print(spec_file)
            return

        # Determine the binary and source rpm names that should be built out of this spec file
        query = [*r'rpm -q --qf %{arch}/%{name}-%{version}-%{release}.%{arch}.rpm\n --specfile'.split(), str(spec_file)]
        try:
            query_output = subprocess.run(query, capture_output=True, encoding='utf-8', check=True).stdout
        except subprocess.CalledProcessError as err:
            print(f'ERROR The command returned {err.returncode}:', ' '.join(
                arg if ' ' not in arg else f"'{arg}'" if "'" not in arg else f'"{arg}"' for arg in query
            ), file=sys.stderr)
            print(err.stderr, file=sys.stderr)
            raise RPMBuildError('rpm querying of specfile failed') from err
        binary_rpms = [pathlib.Path(out) for out in query_output.strip().split('\n')]
        source_rpm = pathlib.Path(binary_rpms[0].stem).with_suffix('.src.rpm')

        # Make a source distribution and copy to SOURCES directory with optional icon.
        if self.source is not None:
            self._copy(self.source, self.rpm_base / 'SOURCES' / pkg_info['sourcefile'])
        else:
            # Unclear whether this is deprecated − it seems not, just the CLI
            pep517.build.build(self.root, 'sdist', self.rpm_base / 'SOURCES', system=self.build_system)

        icon = self.config.get('pysrpm', 'icon', fallback=None)
        if icon:
            icon = pathlib.Path(icon)
            if not icon.exists():
                raise FileNotFoundError(str(icon))
            self._copy(self.icon, self.rpm_base / 'SOURCES' / icon.name)

        # Construct the rpmbuild command
        rpm_cmd = ['rpmbuild', '-bb' if binary_only else '-bs' if source_only else '-ba',
                   '--define', f'_topdir {self.rpm_base.resolve()}',
                   '--define', f'__python {self.templates["python"]}',
                   str(spec_file)]

        if not self.config.getboolean('pysrpm', 'keep_temp'):
            rpm_cmd.insert(-1, '--clean')

        # Run rpmbuild
        try:
            subprocess.run(rpm_cmd, check=True, encoding='utf-8', capture_output=True)
        except subprocess.CalledProcessError as err:
            print(f'ERROR The command returned {err.returncode}:', ' '.join(
                arg if ' ' not in arg else f"'{arg}'" if "'" not in arg else f'"{arg}"' for arg in rpm_cmd
            ), file=sys.stderr)
            print(err.stderr, file=sys.stderr)
            raise RPMBuildError('rpmbuild command failed') from err

        # Replace target files only if we don’t dry run − check files are generated in any case
        if not binary_only:
            srpm = self.rpm_base / 'SRPMS' / source_rpm
            if not srpm.exists():
                raise RPMBuildError('Expected source rpm not found')
            if not dry_run:
                srpm.replace(self.dest_dir / source_rpm.name)
                print(self.dest_dir / source_rpm.name)

        if binary_only or not source_only:
            if not any((self.rpm_base / 'RPMS' / rpm).exists() for rpm in binary_rpms):
                raise RPMBuildError('No binary rpm found, expected at least one')

            for rpm in binary_rpms:
                rpm = self.rpm_base / 'RPMS' / rpm
                if rpm.exists() and not dry_run:
                    rpm.replace(self.dest_dir / rpm.name)
                    print(self.dest_dir / rpm.name)
