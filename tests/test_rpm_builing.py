import pathlib
import shutil
import email
import copy
import pytest
import unittest.mock

import pysrpm.rpm

# Some static elements and mocks
package_root = pathlib.Path(__file__).parent / 'setupcfg_package'
# Make directories intentionally non-standard to check options
dirs = dict(dest_dir=package_root / 'test-dist', rpm_base=package_root / 'test-build')


# Mock distribution metadata, update with:
# python3 -c 'from pep517.meta import load; print(load("setupcfg_package").metadata)'
dist = unittest.mock.Mock()
dist.metadata = email.message_from_string('''Metadata-Version: 2.1
Name: package
Version: 0.0.0
Summary: A sample package
Home-page: https://sample-package.github.io
Author: Cimbali
Author-email: me@cimba.li
License: UNKNOWN
Keywords: test,hello world,sample,packaging
Platform: UNKNOWN
Classifier: Development Status :: 4 - Beta
Classifier: Programming Language :: Python
Requires-Dist: pyparsing (!=2.0.4,!=2.1.2,!=2.1.6,>=2.0.1)
Requires-Dist: babel (!=2.0,>=1.3)
Requires-Dist: pbr (!=2.1.0,>=2.0.0)
Requires-Dist: foo ; os_name != "nt" and implementation_name == "cython"
Requires-Dist: win32 ; os_name == "nt"
Requires-Dist: bar ; platform_machine != "x86" and platform_release > "5.14"
Requires-Dist: enum34 ; python_version < "3.4"
Provides-Extra: test
Requires-Dist: pytest (~=5.0) ; extra == 'test'

Hello world from a sample package?
''')
dist.entry_points = []

pysrpm.rpm.pep517.meta.load = unittest.mock.Mock()
pysrpm.rpm.pep517.meta.load.return_value = dist


# Mock output of subprocess.run(['rpm', '-q', ...]): the list of binary rpm packages to be generated
proc = unittest.mock.Mock()
proc.stdout = 'noarch/python3-package-0.0.0.noarch.rpm\n'

# Mock output of subprocess.run(...) with file creation side-effect
def mock_subproc(query, *a, **k):
    command, opt = query[:2]
    if command == 'rpm':
        return proc
    if command == 'rpmbuild' and opt in ['-bs', '-ba']:
        with open(dirs['rpm_base'] / 'SRPMS' / 'python3-package-0.0.0.src.rpm', 'wb') as f:
            pass
    if command == 'rpmbuild' and opt in ['-bb', '-ba']:
        binrpm = dirs['rpm_base'] / 'RPMS' / 'noarch' / 'python3-package-0.0.0.noarch.rpm'
        binrpm.parent.mkdir(parents=True, exist_ok=True)
        with open(binrpm, 'wb') as f:
            pass
    return unittest.mock.Mock()

rpmbuild_args = ['--define', f'_topdir {dirs["rpm_base"].resolve()}',
                 '--define', '__python python3', '--clean', f'{dirs["rpm_base"]}/SPECS/python3-package.spec']
suproc_args = dict(check=True, encoding='utf-8', capture_output=True)

# Helper functions
def dir_cleanup(func):
    """ Dectorator to remove any used directories before and after a test function """
    def wrapped_func():
        for dir_ in dirs.values():
            shutil.rmtree(dir_, ignore_errors=True)
        try:
            func()
        finally:
            for dir_ in dirs.values():
                shutil.rmtree(dir_, ignore_errors=True)
    return wrapped_func

def get_rpm_instance(source=package_root, **options):
    return pysrpm.rpm.RPM(source, **options)

def run(**options):
    with get_rpm_instance(**options) as rpm:
        rpm.run()


# Actual tests
@dir_cleanup
def test_meta_loading():
    # Check the meta.load() is called only when extract_dependencies are required,
    # use dry runs to check no destination dir is created
    pysrpm.rpm.pep517.meta.load.reset_mock()
    pysrpm.rpm.pep517.meta.load.assert_not_called()

    # ensure PKG-INFO file exists
    pkg_info = package_root / 'PKG-INFO'
    with open(pkg_info, 'w') as f:
        print(dist.metadata.as_string(), file=f)

    try:
        run(spec_only=True, dry_run=True, **dirs, extract_dependencies=False)
        pysrpm.rpm.pep517.meta.load.assert_not_called()
    finally:
        pkg_info.unlink()

    run(spec_only=True, dry_run=True, **dirs, extract_dependencies=True)
    pysrpm.rpm.pep517.meta.load.assert_called()

    assert not dirs['dest_dir'].exists()


@dir_cleanup
def test_rpmbuild_variants():
    with unittest.mock.patch('pysrpm.rpm.subprocess.run') as run_subproc, unittest.mock.patch('pysrpm.rpm.pep517.build.build') as build:
        run_subproc.side_effect = mock_subproc

        # Test spec only without dry-run: should create the spec file, no build directories, no subprocess or pep517.build calls
        run(spec_only=True, keep_temp=True, **dirs)

        run_subproc.assert_not_called()
        build.assert_not_called()
        assert (dirs['dest_dir'] / 'python3-package.spec').exists()
        assert not dirs['rpm_base'].exists()

        # Test source only, check dry run does not create any files in destination
        run(source_only=True, **dirs, dry_run=True)

        run_subproc.assert_called_with(['rpmbuild', '-bs', *rpmbuild_args], **suproc_args)
        build.assert_called_once()
        assert len(list(dirs['dest_dir'].glob('*.rpm'))) == 0

        # Test binary only, check dry run does not create any files in destination
        run(binary_only=True, **dirs, dry_run=True)

        run_subproc.assert_called_with(['rpmbuild', '-bb', *rpmbuild_args], **suproc_args)
        assert len(list(dirs['dest_dir'].glob('*.rpm'))) == 0

        # Test binary + source, check no-dry-run creates the destination files
        run(source_only=False, **dirs)

        run_subproc.assert_called_with(['rpmbuild', '-ba', *rpmbuild_args], **suproc_args)
        assert (dirs['dest_dir'] / 'python3-package-0.0.0.src.rpm').exists()
        assert (dirs['dest_dir'] / 'python3-package-0.0.0.noarch.rpm').exists()


@dir_cleanup
def test_rpmbuild_errors():
    with unittest.mock.patch('pysrpm.rpm.subprocess.run') as run_subproc, unittest.mock.patch('pysrpm.rpm.pep517.build.build') as build:
        # Just a return value without side-effect: simulates rpmbuild (silently) not generating file
        run_subproc.return_value = proc

        # check the build specfile is generated, the rpmbuild command is correct, and the verification of file outputs throws an error
        with pytest.raises(pysrpm.rpm.RPMBuildError, match='Expected source rpm not found'):
            run(**dirs, keep_temp=True)

        spec_path = dirs['rpm_base'] / 'SPECS' / 'python3-package.spec'
        assert spec_path.exists()
        spec_path.unlink()
        build.assert_called_once()
        run_subproc.assert_called_with(['rpmbuild', '-bs', *(arg for arg in rpmbuild_args if arg != '--clean')], **suproc_args)

        # Test cleanup of build, even in case of error − specifically binary only
        with pytest.raises(pysrpm.rpm.RPMBuildError, match='No binary rpm found, expected at least one'):
            run(binary_only=True, **dirs)

        assert not spec_path.exists()
        assert not dirs['rpm_base'].exists()
        run_subproc.assert_called_with(['rpmbuild', '-bb', *rpmbuild_args], **suproc_args)
