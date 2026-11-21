"""Build only the optional shared frame-restoration extension, not the engine."""
import os
from pathlib import Path

from Cython.Build import cythonize
from setuptools import Extension, setup

directory = Path(__file__).resolve().parent
os.chdir(directory.parents[3])
setup(name="bomber-feature-restoration", packages=[], py_modules=[],
    ext_modules=cythonize([Extension("bomber.framework.datahub.features._restoration_native",
        [str(directory/"_restoration_native.pyx")])],
        compiler_directives={"language_level": 3, "boundscheck": True, "wraparound": True},
        build_dir="build/feature-restoration"))
