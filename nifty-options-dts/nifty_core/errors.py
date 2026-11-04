"""Exceptions for nifty_core. Every refusal raises a specific, named error."""


class NiftyError(Exception):
    """Base class for every nifty_core error."""


class LiveTradingNotSupported(NiftyError):
    """Raised on any attempt to enable live trading. Version 1 is paper-only."""


class ConfigError(NiftyError):
    """The configuration is invalid or unsafe."""


class ContractError(NiftyError):
    """The versioned contract specification file is invalid or has no value for the request."""


class UnverifiedContractValue(ContractError):
    """An exchange-dependent value has no primary-source verification and the mode forbids it."""


class KillSwitchActive(NiftyError):
    """The kill switch file exists, so no entry or execution is allowed."""


class RateNotCovered(ContractError):
    """No version of this value covers the requested date."""


class CostRateUnverified(UnverifiedContractValue):
    """One or more cost rates needed for non-fixture use are unverified. `.missing` lists them."""

    def __init__(self, message, missing=()):
        super(CostRateUnverified, self).__init__(message)
        self.missing = tuple(missing)


class CalendarNotCovered(ContractError):
    """The NSE calendar has no verified data for the requested date."""


class DataValidationError(NiftyError):
    """Market data failed a validation rule. `.code` is a stable machine-readable reason."""

    def __init__(self, code, message):
        super(DataValidationError, self).__init__("%s: %s" % (code, message))
        self.code = code


class DataNotAvailable(NiftyError):
    """The provider has no data for the requested time (nothing is invented)."""


class DataNotUsable(NiftyError):
    """Data exists but may not be used for a new entry. `.problems` is a list of (code, text)."""

    def __init__(self, message, problems=()):
        super(DataNotUsable, self).__init__(message)
        self.problems = tuple(problems)


class SpreadError(NiftyError):
    """A proposed structure is not a valid Version 1 debit spread.

    `.code` is the most specific primary reason; `.codes` lists every reason found, and
    `.problems` pairs each code with its explanation. Nothing is repaired.
    """

    def __init__(self, problems):
        problems = list(problems)
        super(SpreadError, self).__init__("; ".join("%s: %s" % p for p in problems))
        self.problems = tuple(problems)
        self.codes = tuple(p[0] for p in problems)
        self.code = self.codes[0] if self.codes else "INVALID_SPREAD"


class RiskViolation(NiftyError):
    """An attempt to use, change or bypass a risk approval. `.code` is a stable machine-readable reason."""

    def __init__(self, code, message):
        super(RiskViolation, self).__init__("%s: %s" % (code, message))
        self.code = code


class JournalError(NiftyError):
    """The trading journal is damaged, mixed, or was asked to do something it must never do. `.code` says why."""

    def __init__(self, code, message):
        super(JournalError, self).__init__("%s: %s" % (code, message))
        self.code = code
