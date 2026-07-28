from . import controllers
from . import models
from . import services

from .load_watchdog import start as _start_load_watchdog


def post_load():
    """Manifest ``post_load`` hook — runs once per process, before any
    ``_register_hook``, and needs no database. See ``load_watchdog`` for why
    that placement is the only one early enough to catch a wedged load.
    """
    _start_load_watchdog()
