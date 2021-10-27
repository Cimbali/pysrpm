""" Main module containing :class:`~RPM`, the package-to-spec (or RPM) converter """
import os
import sys
import tomli
import email
import shutil
import tarfile
import pathlib
import fnmatch
import tempfile
import subprocess
import configparser
import pep517.meta
from contextlib import contextmanager
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

try:
    import importlib.resources as importlib_resources
except ImportError:
    import importlib_resources

from pysrpm.convert import specifier_to_rpm_version, simplify_marker_to_rpm_condition


class RPMBuildError(Exception):
    """ rpmbuild encountered an error """
    pass


class RPM:
    """ Given a python source distribution and a template config, build source, binary, or spec RPM files """
    def __init__(self, config=None, flavour='base_templates', **kwargs):
        """ Setup the various configurations and templates to start converting

        Args:
            config (`str` or path-like): Path to a configuration file
            flavour (`str`): Name of an rpm-based linux flavour, i.e. the name of a config section containing templates
        """
        # Set config params from defaults, config file, from CLI arguments (in kwargs)
        self.config = configparser.ConfigParser(interpolation=configparser.ExtendedInterpolation())

        with importlib_resources.path('pysrpm', 'presets') as defaults_path:
            self.config.read(sorted(defaults_path.glob('*.conf')))

        if config is not None:
            self.load_user_config(config)
        elif pathlib.Path('pyproject.toml').exists():
            self.load_user_config('pyproject.toml')
        else:
            self.load_user_config('setup.cfg')
        self.config.read_dict({'pysrpm': kwargs}, source='CLI')

        # Set config params from loaded config
        self.dest_dir = pathlib.Path(self.config.get('pysrpm', 'dest_dir'))
        self.rpm_base = pathlib.Path(self.config.get('pysrpm', 'rpm_base'))
        env_markers = (line.split(':') for line in self.config.get('pysrpm', 'environment_markers').strip().split('\n'))
        self.environments = {key.strip(): val.strip() for key, val in env_markers}
        self.optional_keys = set(self.config.get('pysrpm', 'optional_keys').split())

        # Load flavoured templates
        inherit_templates = [self.config.get('pysrpm', 'flavour', fallback=flavour)]
        while inherit_templates[-1] != 'base_templates':
            inherit_templates.append(self.config.get(inherit_templates[-1], 'inherits', fallback='base_templates'))

        self.templates = {}
        for section in reversed(inherit_templates):
            self.templates.update(dict(self.config.items(section)))

    def load_user_config(self, path):
        """ Load a config file into the RPM’s configparser, only evaluating interpolations in our own config parser

        Args:
            path (`str` or path-like): Path to a configuration file to load

        Returns:
            `dict` or `None`: A dictionary of section name to section template
        """
        if pathlib.Path(path).suffix == '.toml':
            with open(path, 'rb') as f:
                config = tomli.load(f).get('tool', {}).get('pysrpm', {})
            # Move anything directy under 'pysrpm' into a nested table
            config['pysrpm'] = {key: config.pop(key) for key, value in data.copy().items() if type(value) is not dict}
        else:
            parser = configparser.ConfigParser()
            parser.read(path)
            prefix = 'pysrpm.' if any(section.startswith('pysrpm.') for section in parser.sections()) else ''
            config = {cfgsec.replace(prefix, ''): dict(parser.items(cfgsec)) for cfgsec in parser.sections()
                      if cfgsec.startswith(prefix) or cfgsec == 'pysrpm'}

        self.config.read_dict(config, source=path)

    @staticmethod
    @contextmanager
    def extract_package_to_tempdir(source):
        """ Context manager to inspect the contents of a source package in a temporary non-archive directory

        Args:
            source (`str` or path-like): Path to the source file

        Yields:
            :class:`~pathlib.Path`: The directory containing the package, inside the temporary directory
        """
        tempdir = tempfile.mkdtemp()
        with tarfile.open(source) as tf:
            tf.extractall(tempdir)
        try:
            yield next(pathlib.Path(tempdir).glob('*/PKG-INFO')).parent
        except StopIteration:
            raise ValueError('Malformed source file: can’t find metadata')
        finally:
            shutil.rmtree(tempdir)

    @staticmethod
    def load_source_metadata(source, with_deps=False):
        """ Extract package metadata from a python source package

        Ideally we could just read from the {package}-{version}/PKG-INFO file inside the tarball,
        but it’s unfortunately incomplete, see e.g. https://github.com/pypa/setuptools/issues/1716

        So in order to be as compliant as possible with any future build system enabled by pep517, just extract
        the full tarball and get the metadata from pep517.meta. This is of course much slower, as we extract the full
        package and the pep517 package ensures backends are installed, etc.

        Args:
            source (`str` or path-like): Path to a source distribution of the package
            with_deps (`bool`): Whether to extract

        Returns:
            `dict`: The metadata as a dictionary of keys to strings, or to lists of strings for multiple metadata
        """
        source = pathlib.Path(source)
        if '.tar' not in source.suffixes[-2:]:
            raise ValueError('Expected tarball as source file')

        pyproject_data = {
            'requires': ['setuptools>=40.8.0', 'wheel'],
            'build-backend': 'setuptools.build_meta:__legacy__',
        }

        if with_deps:
            with RPM.extract_package_to_tempdir(source) as root:
                pyproject = root.joinpath('pyproject.toml')
                if pyproject.exists():
                    with open(pyproject, 'rb') as f:
                        pyproject_data.update(tomli.load(f)['build-system'])
                dist_meta = pep517.meta.load(root).metadata

        else:
            with tarfile.open(source) as tf:
                pyproject = fnmatch.filter(tf.getnames(), '*/pyproject.toml')
                if pyproject:
                    with tf.extractfile(pyproject[0]) as f:
                        pyproject_data.update(tomli.load(f)['build-system'])

                pkg_info = fnmatch.filter(tf.getnames(), '*/PKG-INFO')
                if not pkg_info:
                    raise ValueError('Malformed source file: can’t find metadata')
                # Metadata from importlib.metadata is an email.Message, so do the same here
                with tf.extractfile(pkg_info[0]) as f:
                    dist_meta = email.message_from_binary_file(f)
                dist_meta.set_param('charset', 'utf8')

        metadata = {
            'build-requires': pyproject_data['requires'],
            'build-backend': pyproject_data['build-backend'],
            'long-description': dist_meta.get_payload(),
        }

        # See https://packaging.python.org/specifications/core-metadata/
        multiple_use = {'Dynamic', 'Platform', 'Supported-Platform', 'Classifier', 'Requires-Dist',
                        'Requires-External', 'Project-URL', 'Provides-Extra', 'Provides-Dist', 'Obsoletes-Dist'}

        for key, value in dist_meta.items():
            if key == 'Content-Type':
                continue
            elif key in multiple_use:
                metadata.setdefault(key.lower(), []).append(value)
            else:
                metadata[key.lower()] = value

        return metadata

    def convert_python_req(self, reqs, extras=[]):
        """ Compute the version-specified dependency list for a package

        Args:
            reqs (`list` of `str`): The string representations of python dependencies
            extras (`list` of `str`): the list of extras to incldue in the requirement, to evaluate requirement markers

        Returns:
            `list`: A list of string representations for the package dependency with versions
        """
        rpm_reqs = []
        for req in (Requirement(req) for req in reqs):
            condition = simplify_marker_to_rpm_condition(req.marker, self.environments, self.templates)
            if condition is False:
                continue

            package = self.templates['python_package'].format(name=req.name)
            versioned_package = specifier_to_rpm_version(package, req.specifier)

            if condition is True:
                rpm_reqs.append(versioned_package)
            else:
                rpm_reqs.append(f'({versioned_package} {condition})')

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
                line = line.format(**kwargs)
            except KeyError as err:
                if not set(err.args) <= self.optional_keys:
                    raise
            except ValueError:
                print('Error formatting line:', line, file=sys.stderr)
                raise
            else:
                successful_lines.append(line)
        return successful_lines

    def make_spec(self, pkg_info):
        """ Build the spec file for this RPM according to package pkg_info and templates

        Args:
            pkg_info (`dict`): The information to interpolate in the template

        Returns:
            `str`: the contents of the spec file
        """
        spec = self._format_lines(self.templates['preamble'].lstrip('\n'), **pkg_info)
        spec.append('BuildRequires: ' + ', '.join(self.convert_python_req(pkg_info['build-requires'])))

        # Handle automatically extracting dependencies
        extract_deps = self.config.getboolean('pysrpm', 'extract_dependencies')
        deps = pkg_info.get('requires-dist', []) if extract_deps else []
        extras = pkg_info.get('provides-extra', []) if extract_deps else []

        requires_extras = {match for pattern in self.config.get('pysrpm', 'requires_extras').split()
                           for match in fnmatch.filter(extras, pattern)}
        suggests_extras = {match for pattern in self.config.get('pysrpm', 'suggests_extras').split()
                           for match in fnmatch.filter(extras, pattern)}

        # Generate required dependencies
        required = self.config.get('pysrpm', 'requires').replace('\n', ' ')
        required = ([required] if required else []) + self.convert_python_req(deps, requires_extras)
        python_version = self.config.get('pysrpm', 'python_version', fallback=pkg_info.get('requires-python'))
        if python_version:
            required.append(specifier_to_rpm_version('python(abi)', SpecifierSet(python_version)))
        if required:
            spec.append('Requires: ' + ', '.join(required))

        # Generate optional dependencies
        optional = self.config.get('pysrpm', 'suggests').replace('\n', ' ')
        optional = ([optional] if optional else []) + self.convert_python_req(deps, suggests_extras)
        opt_key = self.config.get('pysrpm', 'optional_dependency_tag')
        if opt_key and set(optional) - set(required):
            spec.append(f'{opt_key}: ' + ', '.join(req for req in optional if req not in required))

        # Generate the rest of the file
        sections = self.config.options('base_templates')
        for section in sections[sections.index('preamble') + 1:]:
            section_spec = self._format_lines(self.templates[section], **pkg_info)
            if any(section_spec):
                spec.extend(['', f'%{section}{" " if section_spec[0] else ""}{section_spec[0]}', *section_spec[1:]])

        return '\n'.join(spec)

    def _make_directories(self, spec_only):
        """ Ensure the necessary directories exist

        Args:
            spec_only (`bool`): Whether we only build the spec file or also some RPM files
        """
        self.dest_dir.mkdir(parents=True, exist_ok=True)
        if spec_only:
            return

        for d in ('SOURCES', 'SPECS', 'BUILD', 'RPMS', 'SRPMS'):
            self.rpm_base.joinpath(d).mkdir(parents=True, exist_ok=True)

    def _copy(self, orig, dest):
        """ Copy file from orig to dest, replacing if it exists, hard-linking if possible

        Args:
            orig (`pathlib.Path`): original file, must exst
            dest (`pathlib.Path`): destination path
        """
        if dest.is_dir():
            dest = dest.joinpath(orig.name)
        if dest.exists():
            dest.unlink()
        try:
            os.link(orig, dest)
        except Exception:
            shutil.copy2(orig, dest)

    def run(self, source):
        """ Create the requested files: either spec, source RPM, and/or binary RPM

        Args:
            source (`str` or path-like): Path to a source distribution of the package
        """
        # Setup information to interpolate the templates
        source = pathlib.Path(source)
        if not source.exists():
            raise FileNotFoundError(str(source))

        pkg_info = self.load_source_metadata(source)
        pkg_info['rpmname'] = self.config.get('pysrpm', 'package_prefix') + pkg_info['name']
        pkg_info['rpmversion'] = pkg_info['version'].replace('-', '_')
        pkg_info['release'] = self.config.get('pysrpm', 'release')
        pkg_info['arch'] = self.config.get('pysrpm', 'arch')
        pkg_info['sourcefile'] = source.name

        # Start by building the spec
        spec = self.make_spec(pkg_info)

        dry_run = self.config.getboolean('pysrpm', 'dry_run')
        spec_only = self.config.getboolean('pysrpm', 'spec_only')
        source_only = self.config.getboolean('pysrpm', 'source_only')
        binary_only = self.config.getboolean('pysrpm', 'binary_only')

        if spec_only and dry_run:
            print(spec)
            return

        self._make_directories(spec_only)

        spec_file = (
            self.dest_dir if spec_only else self.rpm_base.joinpath('SPECS')
        ).joinpath(f'{pkg_info["rpmname"]}.spec')

        with open(spec_file, 'w') as f:
            print(spec, file=f)

        if spec_only:
            return

        # Determine the binary and source rpm names that should be built out of this spec file
        query = [*r'rpm -q --qf %{arch}/%{name}-%{version}-%{release}.%{arch}.rpm\n --specfile'.split(), str(spec_file)]
        query_output = subprocess.run(query, capture_output=True, check=True).stdout.decode('utf-8')
        binary_rpms = [pathlib.Path(out) for out in query_output.strip().split('\n')]
        source_rpm = pathlib.Path(binary_rpms[0].stem).with_suffix('.src.rpm')

        # Make a source distribution and copy to SOURCES directory with optional icon.
        self._copy(source, self.rpm_base.joinpath('SOURCES', source.name))

        icon = self.config.get('pysrpm', 'icon', fallback=None)
        if icon:
            icon = pathlib.Path(icon)
            if not icon.exists():
                raise FileNotFoundError(str(icon))
            self._copy(self.icon, source.joinpath(self.icon.name))

        # build package: generate rpmbuild command and run it
        rpm_cmd = ['rpmbuild', '-bs' if source_only else '-bb' if binary_only else '-ba',
                   '--define', f'_topdir {self.rpm_base.resolve()}',
                   '--define', f'__python {self.config.get("pysrpm", "python")}',
                   str(spec_file)]

        if not self.config.getboolean('pysrpm', 'keep_temp'):
            rpm_cmd.insert(-1, '--clean')

        try:
            subprocess.run(rpm_cmd, check=True, encoding='utf-8', capture_output=True)
        except subprocess.CalledProcessError as err:
            print(f'ERROR The command returned {err.returncode}:', ' '.join(
                arg if ' ' not in arg else f"'{arg}'" if "'" not in arg else f'"{arg}"' for arg in rpm_cmd
            ), file=sys.stderr)
            print(err.stderr, file=sys.stderr)
            return RPMBuildError('command failed: ' + err.stderr.split('\n', 1)[0])

        # Replace target files only if we don’t dry run − check files are generated in any case
        if not binary_only:
            srpm = self.rpm_base.joinpath('SRPMS', source_rpm)
            if not srpm.exists():
                raise RPMBuildError('Expected source rpm not found')
            if not dry_run:
                srpm.replace(self.dest_dir.joinpath(source_rpm.name))

        if not source_only:
            if not any(self.rpm_base.joinpath('RPMS', rpm).exists() for rpm in binary_rpms):
                raise RPMBuildError('No binary rpm found, expected at least one')

            for rpm in binary_rpms:
                rpm = self.rpm_base.joinpath('RPMS', rpm)
                if srpm.exists() and not dry_run:
                    rpm.replace(self.dest_dir.joinpath(rpm.name))
