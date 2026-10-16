from setuptools import setup, find_packages

setup(
    name="mayacardio-pipeline",
    version="1.0.0",
    packages=find_packages(),
    install_requires=[
        "psutil>=5.9.0",
    ],
    entry_points={
        "console_scripts": [
            "pipeline=src.cli.main:main",
            "biosignal-mock=src.mock.biosignal_mock:main",
            "check-ground-truth=src.mock.check_ground_truth:main",
        ],
    },
)
