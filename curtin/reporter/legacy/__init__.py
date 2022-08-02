# This file is part of curtin. See LICENSE file for copyright and license info.

from curtin.util import (
    try_import_module,
    )

from abc import (
    ABCMeta,
    abstractmethod,
    )

from curtin.log import LOG


class BaseReporter:
    """Skeleton for a report."""

    __metaclass__ = ABCMeta

    @abstractmethod
    def report_success(self):
        """Report installation success."""

    @abstractmethod
    def report_failure(self, failure):
        """Report installation failure."""


class EmptyReporter(BaseReporter):
    def report_success(self):
        """Empty."""

    def report_failure(self, failure):
        """Empty."""


class LoadReporterException(Exception):
    """Raise exception if desired reporter not loaded."""
    pass


def load_reporter(config):
    """Loads and returns reporter instance stored in config file."""

    reporter = config.get('reporter')
    if reporter is None:
        LOG.info("'reporter' not found in config file.")
        return EmptyReporter()
    name, options = reporter.popitem()
    module = try_import_module(f'curtin.reporter.legacy.{name}')
    if module is None:
        LOG.error(f"Module for {name} reporter could not load.")
        return EmptyReporter()
    try:
        return module.load_factory(options)
    except LoadReporterException:
        LOG.error(f"Failed loading {name} reporter with {options}")
        return EmptyReporter()

# vi: ts=4 expandtab syntax=python
