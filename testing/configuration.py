###################################################################
#  This file serves as a base configuration for testing purposes  #
#  only. It is not intended for production use.                   #
###################################################################

ALLOWED_HOSTS = ['*']

DATABASE = {
    'NAME': 'netbox',
    'USER': 'netbox',
    'PASSWORD': 'netbox',
    'HOST': 'localhost',
    'PORT': '',
    'CONN_MAX_AGE': 300,
    # Named so this plugin's test database is its own. The concurrency suite runs as
    # TransactionTestCase, which commits and then flushes, so it cannot share a database with
    # any other suite that might be running at the same time.
    'TEST': {
        'NAME': 'test_netbox_scripts',
    },
}

PLUGINS = [
    'netbox_scripts',
]

PLUGINS_CONFIG = {
    'netbox_scripts': {},
}

# The plugin's storage entry is required. InMemoryStorage keeps the suite off the
# filesystem, and tests that need a backend of their own override STORAGES themselves.
# NetBox merges this with its built-in aliases, so defining only this entry is safe.
STORAGES = {
    'netbox_scripts': {
        'BACKEND': 'django.core.files.storage.InMemoryStorage',
    },
}

# Databases 14 and 15 rather than the 0 and 1 a running NetBox uses, because the suite really
# enqueues. Django's test runner isolates the database but nothing isolates Redis, so a
# TransactionTestCase commits, its on_commit callback enqueues a live RQ job, and any worker on this
# host executes it. The job's kwargs carry pickled model instances holding TEST primary keys, which
# Django then inserts verbatim, so the queue is a write path into whichever database that worker
# serves. That corrupted a development database on 2026-08-13, leaving its core_job sequence behind
# max(id) so every later insert collided.
REDIS = {
    'tasks': {
        'HOST': 'localhost',
        'PORT': 6379,
        'PASSWORD': '',
        'DATABASE': 15,
        'SSL': False,
    },
    'caching': {
        'HOST': 'localhost',
        'PORT': 6379,
        'PASSWORD': '',
        'DATABASE': 14,
        'SSL': False,
    },
}

SECRET_KEY = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'

API_TOKEN_PEPPERS = {
    1: 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789',
}

# NetBox's manage.py makemigrations refuses to run in non-developer setups.
# This testing config is dev/test only, so enabling it unconditionally is safe.
DEVELOPER = True
