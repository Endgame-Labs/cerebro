"""Conformance suites, one module per contract.

A suite is plain test functions that take one fixture, named for the contract. To run a suite against an
implementation, import its tests into a test module and define that fixture there:

    from suites.store import *  # noqa: F403

    @pytest.fixture
    def store(tmp_path):
        return MyStore(tmp_path)
"""
