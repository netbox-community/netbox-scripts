###################################################################
#  This file serves as a base configuration for testing purposes  #
#  only. It is not intended for production use.                   #
###################################################################

from configuration import *
from netbox_branching.utilities import DynamicSchemaDict

# NetBox refuses a configuration defining both DATABASE and DATABASES, and the star-import brought
# the base module's DATABASE in.
_base_database = globals().pop('DATABASE')

# NetBox Branching serves a branch's connection by resolving the alias it is asked for, which a
# plain dict cannot do.
DATABASES = DynamicSchemaDict(
    {
        'default': {
            **_base_database,
            'ENGINE': 'django.db.backends.postgresql',
            # Not shared: provisioning creates and drops schemas, and ObjectType.features is a
            # stored column written at post_migrate, so it would record whichever config ran last.
            'TEST': {
                'NAME': 'test_netbox_custom_scripts_branching',
            },
        }
    }
)

DATABASE_ROUTERS = [
    'netbox_branching.database.BranchAwareRouter',
]

PLUGINS = [
    'netbox_custom_scripts',
    'netbox_branching',
]

# Both entries are what NetBox would create anyway, kept explicit so the pair is visible here.
PLUGINS_CONFIG = {
    'netbox_custom_scripts': {},
    'netbox_branching': {},
}
