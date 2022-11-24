""" Utility functions to convert complex python dependency expressions to a format that is usable in RPM spec files """
from packaging.version import Version
from packaging.markers import Marker
import re

_rpm_operator_correspondance = {
    '==': '=',
    '===': '=',
    '<': '<',
    '<=': '<=',
    '>=': '>=',
    '>': '>',
}


def _single_marker_to_rpm_condition(marker, templates):
    """ Helper for :func:`~simplify_marker_to_rpm_condition` that expresses a single-cluse marker as a string

    Args:
        marker (`tuple`): A leaf of :class:`~packaging.markers.Marker`’s `_marker` attribute
        templates (`dict`): templates for python version and architecture

    Returns:
        `str`: The RPM-compatible string representation of the marker
    """
    operator, version = marker[1].value, marker[2].value
    if marker[0].value == 'platform_machine':  # arch / uname -m
        arch_template = templates['python_arch']
        if operator == '==':
            return 'with ' + arch_template.format(arch=version)
        elif operator == '!=':
            return 'without ' + arch_template.format(arch=version)
        elif operator == 'in':
            return f'with ({" or ".join(arch_template.format(arch=arch) for arch in version.split())})'
        else:
            raise ValueError(f'Unsupported operator {operator} for platform_machine')

    elif marker[0].value in {'python_full_version', 'python_version', 'implementation_version'}:
        package = templates['python_abi']
    elif marker[0].value == 'platform_release':
        package = 'kernel'
    else:
        raise ValueError(f'Unsupported marker {marker[0].value}')

    rpm_op = _rpm_operator_correspondance.get(operator)
    if rpm_op is not None:
        return f'with {package} {rpm_op} {version}'
    elif operator == '~=':
        return f'with ({package} >= {version} and {package} < {version}^next)'
    elif operator == '!=':
        return f'with ({package} < {version} or {package} > {version})'
    elif operator == 'in':
        return f'with ({" or ".join(f"{package} = {each_version}" for each_version in version.split())})'
    else:
        raise ValueError(f'Unsupported operator {operator} for dependency marker {marker}')


def simplify_marker_to_rpm_condition(marker, environments, templates):
    """ Express a dependency marker in terms useful for RPM packaging, evaluate clauses in the marker if possible

    This should remove markers that are always false in the given environments, identify markers that are always true,
    and return a RPM-spec compliant string condition for any remaining clauses

    Args:
        marker (:class:`~packaging.markers.Marker`): The marker to evaluate
        environments (`dict`): the possible environments, with keys are PEP508 environment markers, values are either
                               a single value as a string, or an iterable of strings for possible values
       templates (`dict`): templates to express python version (`python_abi`) and architecture (`python_arch`)

    Returns:
        `str` or `bool`: A string representing the remaining conditions from the marker, or `True` or `False` if the
                         marker can be evaluated completely
    """
    if marker is None:
        return True

    if isinstance(marker, Marker):
        marker = marker._markers

    if type(marker) is str and marker in {'or', 'and'}:
        return marker

    elif type(marker) is bool:
        return marker

    elif type(marker) is tuple:
        env = marker[0].value
        if env == 'extra':
            return marker[2].value in environments.get(env, [])
        elif not environments.get(env):
            return _single_marker_to_rpm_condition(marker, templates)

        evaluator = Marker(f'{marker[0].value} {marker[1].value} "{marker[2].value}"')
        if type(environments[env]) is str:
            evaluations = [evaluator.evaluate(environments)]
        else:
            evaluations = [evaluator.evaluate({env: val}) for val in environments[env]]

        return (True if all(evaluations) else False if all(not ev for ev in evaluations) else True if env == 'extra'
                else _single_marker_to_rpm_condition(marker, templates))

    elif type(marker) is list:
        simple_markers = [simplify_marker_to_rpm_condition(mk, environments, templates) for mk in marker]
        # disjunctive normal form: drop nested `True`s, `False`s, promote single-item lists and interpret empty lists
        splits = [n for n, v in enumerate(marker) if v == 'or']
        dnf = [[mk for mk in simple_markers[slice(before + 1, last)] if mk != 'and' and mk is not True]
               for before, last in zip([-1, *splits], [*splits, None])]
        dnf = [True if not cl else cl[0] if len(cl) == 1 else cl for cl in dnf if not any(ev is False for ev in cl)]
        return (False if not dnf else True if any(cl is True for cl in dnf)
                else ' '.join(dnf[0]) if len(dnf) == 1 and type(dnf[0]) is list else dnf[0] if len(dnf) == 1
                else '(' + ' or '.join(' '.join(mk) if type(mk) is list else mk for mk in dnf) + ')')


def python_version_to_rpm_version(verstring):
    """ Convert a complex python version to an appropriate and similarly ordered RPM version """
    version = Version(verstring)
    return ''.join([
        f'{version.epoch}:' if version.epoch else '', version.base_version,
        '~{}{}'.format(*version.pre) if version.pre else '', f'^post{version.post}' if version.post is not None else '',
        # The following 2 should likely not be used in RPM released python versions:
        f'~a.dev{version.dev}' if version.dev is not None else '',  # a. prefix to sort before a / b / rc
        f'^local.{version.local}' if version.local is not None else '',  # local. prefix to sort before post
    ])


def specifier_to_rpm_version(package, version):
    """ Compute the version-specified dependency of a package to a RPM version requirement

    The input is a PEP440 version specifier: https://www.python.org/dev/peps/pep-0440/#version-specifiers

    Args:
        package (`str`): A string that is the python package on which to depend
        version (:class:`~packaging.specifiers.SpecifierSet`): the version specifications

    Returns:
        `str`: A string representing the package dependency with versions
    """
    rpm_specs = []
    for spec in version:
        version = spec.version.rstrip(".*")
        rpm_op = _rpm_operator_correspondance.get(spec.operator)
        if rpm_op is not None:
            rpm_specs.append(f'{package} {rpm_op} {version}')
        elif spec.operator == '~=':
            # Caret forces higher sorting (tilde lower)
            bits = re.sub(r'(.?[abc][0-9]*)?(.[a-z]+[0-9]*)?$', '', version).split('.')
            rpm_specs.extend([f'{package} >= {version}',
                              f'{package} < {".".join([*bits[:-2], str(int(bits[-2]) + 1)])}'])
        elif spec.operator == '!=':
            rpm_specs.append(f'{package} < {version} or {package} > {version}')

    if rpm_specs:
        return ', '.join(rpm_specs)

    return package
