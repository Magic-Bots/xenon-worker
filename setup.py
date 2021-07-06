from setuptools import setup, find_packages

setup(
    name='xenon_worker',
    version='0.0.1',
    description='Small Library for building a discord bot on top of rabbitmq',
    url='git@github.com:Magic-Bots/xenon-worker',
    author='Merlintor',
    license='MIT',
    packages=find_packages(),
    python_requires='>=3.6',
    install_requires=[
        "aiohttp==3.7.4",
        "aiormq==3.2.0",
        "motor==2.1.0",
        "ujson==1.35",
        "msgpack==1.0.0",
        'aioredis>=1.3.1'
    ]
)
