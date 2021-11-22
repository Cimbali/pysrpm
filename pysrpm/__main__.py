#!/usr/bin/python3
""" Command line interface to the RPM converter tool """

import click
import collections
import configparser
from pysrpm.rpm import RPM


def list_flavours(context, option, value):
    """ Print the list of flavour presets that are available

    Args:
        context (:class:`~click.Context`): The click context
        option (:class:`~click.Option`): The list-flavours flag
        value (`bool`): The list-flavours flag’s option, `True` iff the flag is present
    """
    if not value:
        return

    config = configparser.RawConfigParser(dict_type=collections.OrderedDict)
    with RPM.preset_configs() as presets_list:
        config.read(presets_list)

    click.echo('Available flavours:')
    for section in config.sections():
        if section != 'pysrpm':
            click.echo(f'- {section}')

    context.exit(0)


@click.command(help='Convert a python source package to RPM')
@click.argument('source', type=click.Path(exists=True), nargs=1)
@click.option('--flavour', help='RPM targets a specific linux flavour', type=str, default=None)
@click.option('--config', help='Specify a config file manually, replaces any configuration from within the package',
              type=click.Path(exists=True, dir_okay=False))
@click.option('--list-flavours', help='List the possible flavours', is_flag=True, callback=list_flavours)
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
def cli(source, **kwargs):
    """ Handle command line interface. Options passed on the command line override options from any config file.

    Args:
        source (:class:`~click.Path`): the source package to convert
    """
    with RPM(source, **{option: value for option, value in kwargs.items() if value is not None}) as rpm_builder:
        rpm_builder.run()


if __name__ == '__main__':
    cli()
