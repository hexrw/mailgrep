default: test

test:
    python3 -m unittest discover -s tests -t tests -v

test-system-python:
    /usr/bin/python3 -m unittest discover -s tests -t tests

doctor:
    PYTHONPATH=src python3 -m mailgrep doctor

run *args:
    PYTHONPATH=src python3 -m mailgrep {{args}}

install:
    pip install .
