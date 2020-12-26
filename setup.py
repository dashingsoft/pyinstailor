from setuptools import setup

__version__ = '1.4'

with open('README.md') as f:
    long_description = f.read()

setup(
    name='pyinstailor',
    version=__version__,
    description='pyinstailor is a tailor to replace files directly '
                'in the executable file generated by PyInstaller.',
    long_description=long_description,

    url='https://github.com/dashingsoft/pyinstailor',
    author='Jondy Zhao',
    author_email='jondy.zhao@gmail.com',

    scripts=['pyinstailor.py'],

    entry_points={
        'console_scripts': [
            'pyinstailor=pyinstailor:main',
        ],
    },

    install_requires=['pyinstaller>=2.1']
)
