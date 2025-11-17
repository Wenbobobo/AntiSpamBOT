from setuptools import find_packages, setup

setup(
    name="jurybot",
    version="0.1.0",
    packages=find_packages(include=("jurybot", "jurybot.*")),
    package_dir={"": "."},
)
