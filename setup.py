from distutils.core import setup
from glob import glob
import os
import sys

import curtin


def is_f(p):
    return os.path.isfile(p)


def in_virtualenv():
    try:
        return sys.real_prefix != sys.prefix
    except AttributeError:
        return False


USR = "usr" if in_virtualenv() else "/usr"

setup(
    name="curtin",
    description='The curtin installer',
    version=curtin.__version__,
    author='Scott Moser',
    author_email='scott.moser@canonical.com',
    license="AGPL",
    url='http://launchpad.net/curtin/',
    packages=[
        'curtin',
        'curtin.block',
        'curtin.deps',
        'curtin.commands',
        'curtin.net',
        'curtin.reporter',
        'curtin.reporter.legacy',
    ],
    scripts=glob('bin/*'),
    data_files=[
        (f'{USR}/share/doc/curtin', [f for f in glob('doc/*') if is_f(f)]),
        (
            f'{USR}/lib/curtin/helpers',
            [f for f in glob('helpers/*') if is_f(f)],
        ),
    ],
)
