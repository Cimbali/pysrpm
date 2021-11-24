from packaging.markers import Marker
from packaging.version import Version
from packaging.specifiers import SpecifierSet
from pysrpm.convert import _single_marker_to_rpm_condition, simplify_marker_to_rpm_condition, specifier_to_rpm_version
from pysrpm.convert import python_version_to_rpm_version
import pysrpm.rpm

import sys
import rpm
import pathlib

TEMPLATES = {
    'python_abi': 'python(abi)',
    'python_arch': 'python({arch})',
    'python_dist': 'python-{name}',
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

class RPMVersion:
    def __init__(self, verstring):
        epoch, version_release = python_version_to_rpm_version(verstring).rpartition(':')[::2]
        version, release = version_release.partition('-')[::2]
        assert all('-' not in tag for tag in (epoch, version, release)), f'Unexpected dash “-” in {verstring}'
        self.version = (epoch or None, version or None, release or None)

    def __lt__(self, other): return rpm.labelCompare(self.version, other.version) < 0
    def __le__(self, other): return rpm.labelCompare(self.version, other.version) <= 0
    def __gt__(self, other): return rpm.labelCompare(self.version, other.version) > 0
    def __ge__(self, other): return rpm.labelCompare(self.version, other.version) >= 0
    def __eq__(self, other): return rpm.labelCompare(self.version, other.version) == 0


def test_version_sorting():
    for l in [
            '0.9 0.9.1 0.9.2 0.9.10 0.9.11 1.0 1.0.1 1.1 2.0 2.0.1'.split(),
            '2012.4 2012.7 2012.10 2013.1 2013.6'.split(),
            '1.0 1.1 2.0 2013.10 2014.04 1!1.0 1!1.1 1!2.0'.split(),
            '0.9 1.0.dev1 1.0.dev2 1.0a0.dev1 1.0a0 1.0c1 1.0c2 1.0 1.0.post1 1.1.dev1'.split(),
            '1.0.dev456 1.0a1 1.0a2.dev456 1.0a12.dev456 1.0a12 1.0b1.dev456 1.0b2 1.0b2.post345.dev456 1.0b2.post345 '\
                '1.0rc1.dev456 1.0rc1 1.0 1.0+abc.5 1.0+abc.7 1.0+5 1.0.post456.dev34 1.0.post456 1.1.dev1'.split(),
            '1.0.dev1 1.0b0 1.0rc2.dev1+patched 1.0rc2 1.0rc2+patched 1.0 1.0+patched 1.0.post0'.split(),
        ]:
        assert sorted(l, key=Version) == l, 'Specified version order incorrect'
        assert sorted(l, key=RPMVersion) == l, 'Version sorting not maintained by RPM conversion'


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


def test_full_requirement_conversion():
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
