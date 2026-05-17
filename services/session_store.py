from collections.abc import MutableMapping

from db.bot_session import BotSession


class PersistentNestedDict(dict):
    def __init__(self, parent, key, initial=None):
        super().__init__(initial or {})
        self._parent = parent
        self._key = key

    def _sync(self):
        self._parent[self._key] = dict(self)

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._sync()

    def __delitem__(self, key):
        super().__delitem__(key)
        self._sync()

    def clear(self):
        super().clear()
        self._sync()

    def pop(self, key, default=None):
        value = super().pop(key, default)
        self._sync()
        return value

    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)
        self._sync()


class PersistentBucket(MutableMapping):
    def __init__(self, bucket_name):
        self.bucket_name = bucket_name
        self.cache = {}
        self.loaded_keys = set()

    def _normalize_key(self, key):
        return int(key)

    def _wrap(self, key, value):
        if isinstance(value, dict) and not isinstance(value, PersistentNestedDict):
            return PersistentNestedDict(self, key, value)
        return value

    def _load(self, key):
        key = self._normalize_key(key)
        if key not in self.loaded_keys:
            self.cache[key] = BotSession.get_bucket(key, self.bucket_name)
            self.loaded_keys.add(key)
        return self.cache.get(key)

    def __getitem__(self, key):
        key = self._normalize_key(key)
        value = self._load(key)
        if value is None:
            raise KeyError(key)
        return self._wrap(key, value)

    def __setitem__(self, key, value):
        key = self._normalize_key(key)
        self.cache[key] = value
        self.loaded_keys.add(key)
        BotSession.set_bucket(key, self.bucket_name, value)

    def __delitem__(self, key):
        key = self._normalize_key(key)
        self.cache.pop(key, None)
        self.loaded_keys.add(key)
        BotSession.clear_bucket(key, self.bucket_name)

    def __iter__(self):
        return iter(self.cache)

    def __len__(self):
        return len([value for value in self.cache.values() if value is not None])

    def __contains__(self, key):
        return self._load(key) is not None

    def get(self, key, default=None):
        value = self._load(key)
        if value is None:
            return default
        return self._wrap(self._normalize_key(key), value)

    def pop(self, key, default=None):
        key = self._normalize_key(key)
        value = self._load(key)
        if value is None:
            return default
        self.__delitem__(key)
        return value

    def setdefault(self, key, default=None):
        key = self._normalize_key(key)
        value = self._load(key)
        if value is None:
            value = default if default is not None else {}
            self[key] = value
        return self._wrap(key, value)
