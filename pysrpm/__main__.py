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
    if not value or context.resilient_parsing:
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
@click.option('--flavour', '-f', help='RPM targets a specific linux flavour', type=str, default=None)
@click.option('--config', '-c', type=click.Path(exists=True, dir_okay=False),
              help='Specify a config file manually, replaces any configuration from within the package')
@click.option('--project-config', '-p', is_flag=True,
              help='Specified config file is a project config')
@click.option('--list-flavours', help='List the possible flavours', is_flag=True, callback=list_flavours,
              expose_value=False, is_eager=True)
# Override options whose defaults are under [pysrpm] in defaults.conf, with "_" replaced by "-"
@click.option('--release', '-r', help='Release of the RPM package', type=str)
@click.option('--rpm-base', help='Build directory', type=click.Path(exists=False, file_okay=False))
@click.option('--dest-dir', '-d', type=click.Path(exists=False, file_okay=False),
              help='Directory for final RPM or spec file')
@click.option('--spec-only/--no-spec-only', '-s', help='Only build spec file', default=None)
@click.option('--source-only/--no-source-only', help='Only build source RPM file (is the default)', default=None)
@click.option('--binary-only/--no-binary-only', '-b', help='Only build binary RPM file(s)', default=None)
@click.option('--keep-temp/--no-keep-temp', '-k', default=None,
              help='Do not remove temporary files in the build hierarchy')
@click.option('--dry-run/--no-dry-run', '-n', default=None,
              help='Do not replace target files even if building RPMs succeed')
@click.option('--no-extract-dependencies/--extract-dependencies',
              help='Automatically convert python dependencies to RPM package dependencies', default=None)
@click.option('--template', '-t', 'templates', multiple=True, type=(str, str), metavar='<template-key> <value>',
              help='Override a specific template (repeatable)')
@click.help_option('--help', '-h')
def cli(source, templates=[], **kwargs):
    """ Handle command line interface. Options passed on the command line override options from any config file.

    Args:
        source (:class:`~click.Path`): the source package to convert
        templates (`list` of 2-`str`-tuples): the key / value pairs to define templates on the command line
    """
    options = {option.replace('-', '_'): value for option, value in kwargs.items() if value is not None}
    templates = {option.replace('-', '_'): value for option, value in templates}
    with RPM(source, **options, cli_templates=templates) as rpm_builder:
        rpm_builder.run()


if __name__ == '__main__':
    cli()
