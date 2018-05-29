#!/usr/bin/env python

from setuptools import setup

import versioneer


setup(name='metsim',
      version=versioneer.get_version(),
      cmdclass=versioneer.get_cmdclass(),
      description='Meteorology Simulator',
      url='https://github.com/UW-Hydro/MetSim',
      download_url='https://github.com/UW-Hydro/MetSim/archive/v1.0.tar.gz',
      author='Andrew Bennett',
      author_email='bennett.andr@gmail.com',
      packages=['metsim', 'metsim.methods'],
      entry_points={
          'console_scripts': ['ms = metsim.cli.ms:main']},
      install_requires=['xarray', 'numba'],
      keywords=['meteorology', 'disaggregation', 'hydrology',
                'climate', 'mtclim'],
      tests_require=['pytest'],)
