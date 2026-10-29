# Caching between Script runs

Use Django's cache API to reuse an expensive external lookup across Script runs.
Cache only data that can be fetched again, and choose an expiry that suits how
quickly the source changes.

## Use the deployment's cache

`django.core.cache.cache` uses Django's default cache. NetBox configures it from
[`REDIS['caching']`](https://docs.netbox.dev/en/stable/configuration/required-parameters/#redis).
Runs share values when every NetBox process points `REDIS['caching']` at the
same Redis server and database. Ask the deployment administrator to confirm that
configuration.

Module-level variables and `functools.lru_cache()` in Project modules do not
keep values between runs. See [Runtime behavior](execution.md#runtime-behavior).

## Cache an external lookup

This example reads a JSON list of site codes, such as `["ams", "fra"]`, from an
external inventory service. The list must be safe for every user of this Script
to read. Replace the example URL and `PROJECT_KEY` with your service and the
Project's [`key`](../reference/models/scriptproject.md#public-and-storage-identifiers).

```python
import requests
from django.core.cache import cache

from netbox_scripts import AbortScript, Script

PROJECT_KEY = 'inventory-sync'
CACHE_KEY = f'netbox-scripts:{PROJECT_KEY}:site-codes:v1'
CACHE_TIMEOUT = 300


def get_site_codes():
    """Fetch site codes when no cached result is available."""
    missing = object()
    site_codes = cache.get(CACHE_KEY, missing)
    if site_codes is not missing:
        return site_codes

    try:
        response = requests.get(
            'https://inventory.example.com/api/site-codes/',
            timeout=10,
        )
        response.raise_for_status()
        site_codes = response.json()
    except (requests.RequestException, ValueError) as error:
        raise AbortScript('Could not fetch site codes from the inventory service.') from error

    if not isinstance(site_codes, list) or not all(isinstance(code, str) for code in site_codes):
        raise AbortScript('The inventory service returned invalid site codes.')

    cache.set(CACHE_KEY, site_codes, timeout=CACHE_TIMEOUT)
    return site_codes


class ShowSiteCodes(Script):
    class Meta:
        name = 'Show site codes'
        description = 'Look up site codes from the inventory service'
        commit_default = False

    def run(self, data, commit):
        return '\n'.join(get_site_codes())
```

The entry expires after five minutes, but may be evicted or removed sooner.
A miss fetches fresh data with an HTTP timeout. Failed requests and unexpected
responses stop the run without caching a result. An empty list is a valid
result, and the sentinel distinguishes it from a missing entry.

All cache and network access happens from `run()`, so importing the module for
validation or form preparation does not perform the lookup. Helpers shared by
several Scripts can live in a separate Project module and use relative imports.

This example handles missing entries. Cache errors, such as a lost connection, a
full Redis or a value that cannot be serialized, still propagate and fail the
run, even after a successful lookup. To bypass an unavailable cache, catch
`redis.exceptions.RedisError` around the cache calls, but keep lookup failures
visible.

## Choose keys and invalidate values

Build each key from a fixed prefix, the Project's `key` and the lookup's
purpose, as in `netbox-scripts:inventory-sync:site-codes:v1`. This avoids
collisions with NetBox and other applications using the same cache.

- Include every input that can change the result, such as an external object ID
  or service endpoint. Keep keys short, and hash long or arbitrary input values.
- Use an explicit version token when a change to the lookup or value format
  makes old entries unsuitable. Use a different token for revisions that must
  not share values. Activating a revision does not invalidate these entries.
- Include the user's ID when results depend on that user, and check permissions
  before using them. Read the user from `self.request.user` when
  [a request is available](authoring.md#run-context). Skip per-user caching when
  no authenticated user is available. Never share permission-filtered results
  under a common key.
- Separate environments that share a backend. Namespacing avoids collisions.
  It does not control who can read the cache.

To invalidate one known entry, use `cache.delete(CACHE_KEY)`. Changing the version
token starts a fresh namespace and lets old entries expire. A concurrent lookup
can repopulate a deleted key, so deletion alone does not guarantee fresh data.

**Do not call `cache.clear()` from a Script.** The default cache also holds
NetBox's data and other applications' entries. With NetBox's Redis backend,
`cache.clear()` empties the whole Redis database, and a deployment that shares
that database with the task queue also loses queued background tasks.

## Concurrent misses

Several workers can miss the same key and perform the lookup at the same time.
Keep the lookup read-only and safe to repeat. This example and Django's
`get_or_set()` do not prevent duplicate requests or guarantee which fetch's
result is stored.

If duplicate requests are unacceptable, use coordination supported by the
deployment's backend, with bounded waits and a defined fallback. Django's common
cache API does not promise cross-process locking, atomic read-modify-write
operations or LRU eviction. Those guarantees depend on the backend and its
configuration.

Do not refresh entries from a background thread. With NetBox's default worker,
a queued run executes in a child process that exits as soon as the run ends,
which stops the thread.

## Data and dry runs

Store plain values such as strings, lists and dictionaries. Avoid model instances,
querysets and instances of classes defined in Project source. Do not cache
passwords, tokens or other secrets.

Redis cache writes are outside the Script's database transaction. This example
can populate the cache during a dry run because it reads independent external
data. Do not cache values derived from uncommitted database changes. When a
cache update depends on a successful database write, defer it with
[`transaction.on_commit()`](https://docs.djangoproject.com/en/stable/topics/db/transactions/#performing-actions-after-commit)
and pass `robust=True`, so a cache error after the commit does not fail a run
whose changes were kept.

This cache holds disposable application data. The plugin's
[runtime cache](../administration/configuration.md#runtime-cache) holds local
copies of revision source for Python imports. Keep lookup data out of that
directory and Project storage. Use a persistent model or an external store when
data must survive cache loss.

See Django's [cache API documentation](https://docs.djangoproject.com/en/stable/topics/cache/#the-low-level-cache-api)
for the supported operations.
