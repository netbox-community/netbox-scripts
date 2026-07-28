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
}

PLUGINS = [
    'netbox_custom_scripts',
]

PLUGINS_CONFIG = {
    'netbox_custom_scripts': {},
}

# The plugin's storage entry is required. InMemoryStorage keeps the suite off the
# filesystem, and tests that need a backend of their own override STORAGES themselves.
# NetBox merges this with its built-in aliases, so defining only this entry is safe.
STORAGES = {
    'netbox_custom_scripts': {
        'BACKEND': 'django.core.files.storage.InMemoryStorage',
    },
}

REDIS = {
    'tasks': {
        'HOST': 'localhost',
        'PORT': 6379,
        'PASSWORD': '',
        'DATABASE': 0,
        'SSL': False,
    },
    'caching': {
        'HOST': 'localhost',
        'PORT': 6379,
        'PASSWORD': '',
        'DATABASE': 1,
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
