#!/usr/bin/python3

import tarfile
import pathlib
import sys
import os
import shutil
import subprocess
import configparser
from collections import OrderedDict
import click


class RPMBuildError(Exception):
    pass


class RPM:
    """ Given a python source distribution and a template config, build source, binary, or spec RPM files

    Args:
        config (`str` or path-like): Path to a configuration file
        flavour (`str` or `None`): Name of an rpm-based linux flavour with a template config in the templates/ directory
    """
    def __init__(self, config=None, flavour=None, **kwargs):
        # Load default templates
        templates_path = pathlib.Path('templates/base.cfg')
        parser = configparser.RawConfigParser()
        parser.read(templates_path)
        self.templates = OrderedDict(parser.items('templates'))

        # Set config params from defaults, config file, from CLI arguments (in kwargs)
        self.config = configparser.ConfigParser()
        self.load_config('defaults.conf')

        if config is not None:
            config_templates = self.load_config(config)

        self.config.read_dict({'pysrpm': kwargs}, source='CLI')

        # Set config params from loaded config
        self.dest_dir = pathlib.Path(self.config.get('pysrpm', 'dest_dir'))
        self.rpm_base = pathlib.Path(self.config.get('pysrpm', 'rpm_base'))

        # Load flavoured templates
        flavour = self.config.get('pysrpm', 'flavour', fallback=flavour)
        if flavour is not None:
            self.templates.update(self.load_config(templates_path.with_name(flavour + templates_path.suffix)) or {})

        # Load the config file’s templates last, to override
        if config is not None and config_templates is not None:
            self.templates.update(config_templates)


    def load_config(self, path):
        """ Load a config file into the RPM’s configparser, and separately return any section templates

        Args:
            path (`str` or path-like): Path to a configuration file to load

        Returns:
            `dict` or `None`: A dictionary of section name to section template
        """
        parser = configparser.ConfigParser()
        parser.read(path)
        prefix = 'pysrpm.' if any(section.startswith('pysrpm.') for section in parser.sections()) else ''
        tplsec = f'{prefix}templates'

        self.config.read_dict({cfgsec.replace(prefix, ''): dict(parser.items(cfgsec)) for cfgsec in parser.sections()
                               if cfgsec.startswith(prefix) and cfgsec != tplsec or cfgsec == 'pysrpm'}, source=path)

        if parser.has_section(tplsec):
            return {rpmsec: template for rpmsec, template in parser.items(tplsec, raw=True) if rpmsec in self.templates}


    @staticmethod
    def load_source_metadata(source):
        """ Extract package metadata from a python source package, from the {package}-{version}/PKG-INFO file

        Args:
            source (`str` or path-like): Path to a source distribution of the package
        """
        source = pathlib.Path(source)
        if '.tar' not in source.suffixes[-2:]:
            raise ValueError('Expected tarball as source file')

        with tarfile.open(source) as tf:
            for path in tf.getnames():
                if path.endswith('/PKG-INFO') and path.count('/') == 1:
                    break
            else:
                raise ValueError('Malformed source file: can’t find metadata')

            with tf.extractfile(path) as f:
                lines = [l.decode('utf-8') for l in f.readlines()]

        metadata = {}

        try:
            desc_sep = lines.index('\n')
        except ValueError:
            pass
        else:
            metadata['long-description'] = ''.join(lines[desc_sep + 1:])
            lines = lines[:desc_sep]

        for key, val in (line.rstrip('\n').split(': ', 1) for line in lines):
            key = key.lower()
            if type(metadata.get(key, None)) is list:
                metadata[key].append(val)
            elif key in metadata:
                metadata[key] = [metadata[key], val]
            else:
                metadata[key] = val

        return metadata


    def make_spec(self, pkg_info):
        """ Build the spec file for this RPM according to package pkg_info and templates

        Args:
            pkg_info (`dict`): The information to interpolate in the template

        Returns:
            `str`: the contents of the spec file
        """
        first, *mid_sections, last = self.templates.keys()
        assert first == 'header' and last == 'footer'

        spec = self.templates['header'].format(**pkg_info)
        # TODO: here, add all additional dependencies

        for section in mid_sections:
            try:
                spec += f'\n\n%{section}' + self.templates[section].format(**pkg_info)
            except KeyError as err:
                if (section, *err.args) in [
                            ('description', 'long-description'),
                            ('license', 'license-file'),
                            ('doc', 'doc-file'),
                        ]:
                    # Some errors are tolerated, just don’t include that section
                    print(f'Skipping section %{section} because pkg_info {err.args[0]} missing', file=sys.stderr)
                else:
                    raise

        return spec + self.templates['footer'].format(**pkg_info)


    def make_directories(self, spec_only):
        """ Ensure the necessary directories exist

        Args:
            spec_only (`bool`): Whether we only build the spec file or also some RPM files
        """
        self.dest_dir.mkdir(parents=True, exist_ok=True)
        if spec_only:
            return

        for d in ('SOURCES', 'SPECS', 'BUILD', 'RPMS', 'SRPMS'):
            self.rpm_base.joinpath(d).mkdir(parents=True, exist_ok=True)


    def copy(self, orig, dest):
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
        except Exception as e:
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

        self.make_directories(spec_only)

        spec_file = (
            self.dest_dir if spec_only else self.rpm_base.joinpath('SPECS')
        ).joinpath(f'{pkg_info["rpmname"]}.spec')

        with open(spec_file, 'w') as f:
            print(spec, file=f)

        if spec_only:
            return

        # Determine the binary rpm names that should be built out of this spec file
        query = [*r'rpm -q --qf %{arch}/%{name}-%{version}-%{release}.%{arch}.rpm\n --specfile'.split(), str(spec_file)]
        query_output = subprocess.run(query, capture_output=True, check=True).stdout.decode('utf-8')
        binary_rpms = [pathlib.Path(out) for out in query_output.strip().split('\n')]
        source_rpm = pathlib.Path(binary_rpms[0].stem).with_suffix('.src.rpm')

        # Make a source distribution and copy to SOURCES directory with optional icon.
        self.copy(source, self.rpm_base.joinpath('SOURCES', source.name))

        icon = self.config.get('pysrpm', 'icon', fallback=None)
        if icon:
            icon = pathlib.Path(icon)
            if not icon.exists():
                raise FileNotFoundError(str(icon))
            self.copy(self.icon, source.joinpath(self.icon.name))

        # build package
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

        # Dry run generates files with rpmbuild anyway, just don’t replace targets − so we check files are generated
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


@click.command()
@click.argument('sources', type=click.Path(exists=True), nargs=-1)

# Override options whose defaults are under [pysrpm] in defaults.conf, with _ becoming -
@click.option('--release', help='Release of the RPM package', type=str)
@click.option('--rpm-base', help='Build directory', type=click.Path(exists=False, file_okay=False))
@click.option('--dest-dir', help='Directory for final RPM files', type=click.Path(exists=False, file_okay=False))
@click.option('--flavour', help='RPM targets a specific linux flavour', type=str)
@click.option('--spec-only/--no-spec-only', help='Only build spec file', default=None)
@click.option('--source-only/--no-source-only', help='Only build source RPM file', default=None)
@click.option('--binary-only/--no-binary-only', help='Only build binary RPM file(s)', default=None)
@click.option('--keep-temp/--no-keep-temp', help='Do not remove temporary files in the build hierarchy', default=None)
@click.option('--dry-run/--no-dry-run', help='Do not replace target files even if building RPMs succeed', default=None)
@click.option('--python', help='Set the name of the python executable the RPM should use during build', type=str)
@click.option('--package-prefix', help='Prefix to the package name, e.g. python3-', type=str)
@click.option('--icon', help='An icon to copy to the source build dir', type=click.Path(exists=True, dir_okay=False))

def cli(sources, **kwargs):
    rpm_builder = RPM(**{option: value for option, value in kwargs.items() if value is not None})
    for src in sources:
        rpm_builder.run(src)

if __name__ == '__main__':
    cli()
