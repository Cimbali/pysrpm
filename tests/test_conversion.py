from packaging.markers import Marker
from packaging.specifiers import SpecifierSet
from pysrpm.convert import _single_marker_to_rpm_condition, simplify_marker_to_rpm_condition, specifier_to_rpm_version
import pysrpm.rpm

import sys
import pathlib

TEMPLATES = {
    'python_abi': 'python(abi)',
    'python_arch': 'python({arch})',
    'python_package': 'python-{name}',
}

ENVIRONMENT = {
    'os_name': 'posix',
    'sys_platform': 'linux',
    'platform_system': 'Linux',
    'implementation_name': 'cpython',
    'platform_python_implementation': 'CPython',
    'extra': [],
}


def single_marker(text):
    return _single_marker_to_rpm_condition(Marker(text)._markers[0], TEMPLATES)

def complex_marker(text, extras=[]):
    return simplify_marker_to_rpm_condition(Marker(text), {**ENVIRONMENT, 'extra': extras}, TEMPLATES)

def version(text):
    return specifier_to_rpm_version('package', SpecifierSet(text))

def multiple_requirements(texts, extras=[]):
    with pysrpm.rpm.RPM(pathlib.Path(__file__).parent / 'setupcfg_package') as rpm:
        rpm.environments = ENVIRONMENT
        rpm.templates = TEMPLATES

        converted = rpm.convert_python_req(texts, extras=extras)
        assert len(converted) <= 1
        return set().union(*(set(req.split(', ')) for req in converted))

def single_requirement(text, extras=[]):
    return multiple_requirements([text], extras=extras)


def test_single_marker():
    assert single_marker('platform_machine == "x86-64"') == 'with python(x86-64)'
    assert single_marker('platform_release > "3.4"') == 'with kernel > 3.4'


def test_marker_extras():
    assert complex_marker('extra == "micro"') == False
    assert complex_marker('extra == "micro"', extras=['micro']) == True
    assert complex_marker('extra == "micro" and os_name == "nt"') == False
    assert complex_marker('extra == "micro" and os_name == "nt"', extras=['micro']) == False
    assert complex_marker('extra == "micro" and os_name == "posix"') == False
    assert complex_marker('extra == "micro" and os_name == "posix"', extras=['micro']) == True
    assert complex_marker('extra == "micro" or platform_machine == "x86-64"', extras=[]) == 'with python(x86-64)'
    assert complex_marker('extra == "micro" or platform_machine == "x86-64"', extras=['micro']) == True


def test_complex_marker():
    assert complex_marker('os_name == "nt"') == False
    assert complex_marker('os_name != "nt" and implementation_name == "cpython"') == True
    assert complex_marker('platform_machine == "x86-64"') == 'with python(x86-64)'
    assert complex_marker('platform_release > "3.4"') == 'with kernel > 3.4'
    assert complex_marker('platform_machine == "x86-64" and platform_release > "3.4"') == 'with python(x86-64) with kernel > 3.4'
    assert complex_marker('os_name == "nt" and platform_machine == "x86-64" or platform_release > "3.4"') == 'with kernel > 3.4'
    assert complex_marker('platform_machine != "x86" and platform_release > "5.14"') == 'without python(x86) with kernel > 5.14'
    assert complex_marker('python_version < "3.4"') == 'with python(abi) < 3.4'


def test_version_comparison():
    assert version('== 1.5') == 'package = 1.5'
    assert version('> 1.5') == 'package > 1.5'
    assert version('< 1.5') == 'package < 1.5'
    assert version('>= 2.1.3a0') == 'package >= 2.1.3a0'
    assert version('<= 1.5') == 'package <= 1.5'
    assert version('== 1.5.*') == 'package = 1.5'
    assert version('~= 1.5.3b7') == 'package >= 1.5.3b7, package < 1.6'
    assert version('!= 1.5.*') == 'package < 1.5 or package > 1.5'


def test_full_conversion():
    assert single_requirement('package (!=2.0.4,!=2.1.2,!=2.1.6,>=2.0.1)') == {
            'python-package < 2.1.6 or python-package > 2.1.6', 'python-package < 2.0.4 or python-package > 2.0.4',
            'python-package < 2.1.2 or python-package > 2.1.2', 'python-package >= 2.0.1',
    }
    assert single_requirement('package (!=2.0,>=1.3)') == {'python-package < 2.0 or python-package > 2.0', 'python-package >= 1.3'}
    assert single_requirement('package (!=2.1.0,>=2.0.0)') == {'python-package >= 2.0.0', 'python-package < 2.1.0 or python-package > 2.1.0'}
    assert single_requirement('package ; os_name == "nt"') == set()
    assert single_requirement('package ; os_name != "nt" and implementation_name == "cpython"') == {'python-package'}
    assert single_requirement('package ; platform_machine != "x86" and platform_release > "5.14"') == {'python-package without python(x86) with kernel > 5.14'}
    assert single_requirement('package (~=5.0) ; extra == "test"') == set()
    assert single_requirement('package (~=5.0) ; extra == "test"', extras=['test']) == {'python-package >= 5.0', 'python-package < 6'}
    assert single_requirement('package == 5.* ; python_version < "3.4"') == {'python-package = 5 with python(abi) < 3.4'}
