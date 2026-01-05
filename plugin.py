import logging

# logger by package name
LOG = logging.getLogger(__package__)

def get_log_level(level_name):
    """Maps log level names to logging constants."""
    return getattr(logging, level_name.upper(), logging.ERROR)


def update_log_level(settings):
    """
    Reads the log_level from settings and reconfigures the logger.
    """
    level_name = settings.get("log_level", "ERROR")
    level = get_log_level(level_name)
    LOG.setLevel(level)
    LOG.propagate = False
    if not LOG.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(message)s')
        handler.setFormatter(formatter)
        LOG.addHandler(handler)
