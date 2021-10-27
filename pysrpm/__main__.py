#!/usr/bin/python3
""" Command line interface to the RPM converter tool """

import click
from pysrpm.rpm import RPM


@click.command()
@click.argument('sources', type=click.Path(exists=True), nargs=-1)
@click.option('--flavour', help='RPM targets a specific linux flavour', type=str)
@click.option('--config', help='Specify an additional config file', type=click.Path(exists=True, dir_okay=False))
# Override options whose defaults are under [pysrpm] in defaults.conf, with "_" replaced by "-"
@click.option('--release', help='Release of the RPM package', type=str)
@click.option('--rpm-base', help='Build directory', type=click.Path(exists=False, file_okay=False))
@click.option('--dest-dir', help='Directory for final RPM or spec file', type=click.Path(exists=False, file_okay=False))
@click.option('--spec-only/--no-spec-only', help='Only build spec file', default=None)
@click.option('--source-only/--no-source-only', help='Only build source RPM file', default=None)
@click.option('--binary-only/--no-binary-only', help='Only build binary RPM file(s)', default=None)
@click.option('--keep-temp/--no-keep-temp', help='Do not remove temporary files in the build hierarchy', default=None)
@click.option('--dry-run/--no-dry-run', help='Do not replace target files even if building RPMs succeed', default=None)
@click.option('--python', help='Set the name of the python executable the RPM should use during build', type=str)
@click.option('--package-prefix', help='Prefix to the package name, e.g. python3-', type=str)
@click.option('--icon', help='An icon to copy to the source build dir', type=click.Path(exists=True, dir_okay=False))
@click.option('--optional-dependency-tag', help='', type=str)
@click.option('--requires', help='RPM packages on which to depend', type=str)
@click.option('--suggests', help='RPM packages to suggest', type=str)
@click.option('--extract-dependencies/--no-extract-dependencies',
              help='Automatically convert python dependencies to RPM package dependencies', default=None)
@click.option('--requires-extras', help='Extras from python package to include as requires (if extracting)', type=str)
@click.option('--suggests-extras', help='Extras from python package to include as suggests (if extracting)', type=str)
def cli(sources, **kwargs):
    """ Handle command line interface """
    rpm_builder = RPM(**{option: value for option, value in kwargs.items() if value is not None})
    for src in sources:
        rpm_builder.run(src)


if __name__ == '__main__':
    cli()