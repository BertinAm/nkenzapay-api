"""Package init.

Two jobs: expose the Celery app, and make MySQL work on hosting that cannot
build a C extension.
"""
try:
    import MySQLdb  # noqa: F401
except ImportError:
    # mysqlclient needs gcc and mysql_config, which a cPanel account usually
    # does not have. PyMySQL is pure Python, installs anywhere, and presents
    # the same interface Django looks for.
    try:
        import pymysql

        pymysql.install_as_MySQLdb()
    except ImportError:
        # Neither is installed. Fine on SQLite or PostgreSQL; Django will say
        # so clearly if the configured database actually needs one.
        pass

from .celery import app as celery_app  # noqa: E402

__all__ = ("celery_app",)
