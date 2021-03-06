#
# Copyright 2017-2019 European Centre for Medium-Range Weather Forecasts (ECMWF).
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Authors:
#   Alessandro Amici - B-Open - https://bopen.eu
#

from __future__ import absolute_import, division, print_function, unicode_literals
from builtins import bytes, isinstance, str, type

# Python 2 compatibility bit not in python-future
try:
    FileExistsError
except NameError:
    FileExistsError = OSError

import collections
import contextlib
import hashlib
import io
import logging
import os
import pickle
import typing as T

import attr

from . import bindings


LOG = logging.getLogger(__name__)
_MARKER = object()

#
# No explicit support for MULTI-FIELD at Message level, let ecCodes simulate normal messages.
#
bindings.codes_grib_multi_support_on()


@attr.attrs()
class Message(collections.MutableMapping):
    """Dictionary-line interface to access Message headers."""
    codes_id = attr.attrib()
    encoding = attr.attrib(default='ascii', type=str)
    errors = attr.attrib(
        default='warn',
        validator=attr.validators.in_(['ignore', 'warn', 'raise']),
    )

    @classmethod
    def from_file(cls, file, offset=None, product_kind=bindings.CODES_PRODUCT_GRIB, **kwargs):
        # type: (T.IO[bytes], int, int, T.Any) -> Message
        if offset is not None:
            file.seek(offset)
        codes_id = bindings.codes_handle_new_from_file(file, product_kind)
        return cls(codes_id=codes_id, **kwargs)

    @classmethod
    def from_sample_name(cls, sample_name, product_kind=bindings.CODES_PRODUCT_GRIB, **kwargs):
        codes_id = bindings.codes_new_from_samples(sample_name.encode('ASCII'), product_kind)
        return cls(codes_id=codes_id, **kwargs)

    @classmethod
    def from_message(cls, message, **kwargs):
        codes_id = bindings.codes_handle_clone(message.codes_id)
        return cls(codes_id=codes_id, **kwargs)

    def __del__(self):
        bindings.codes_handle_delete(self.codes_id)

    def message_get(self, item, key_type=None, size=None, length=None, default=_MARKER):
        # type: (str, int, int, int, T.Any) -> T.Any
        """Get value of a given key as its native or specified type."""
        key = item.encode(self.encoding)
        try:
            values = bindings.codes_get_array(self.codes_id, key, key_type, size, length)
        except bindings.EcCodesError as ex:
            if ex.code == bindings.lib.GRIB_NOT_FOUND:
                if default is _MARKER:
                    raise KeyError(item)
                else:
                    return default
            else:  # pragma: no cover
                raise
        if values and isinstance(values[0], bytes):
            values = [v.decode(self.encoding) for v in values]
        if len(values) == 1:
            return values[0]
        return values

    def message_set(self, item, value):
        # type: (str, T.Any) -> None
        key = item.encode(self.encoding)
        set_array = isinstance(value, T.Sequence) and not isinstance(value, (str, bytes))
        if set_array:
            bindings.codes_set_array(self.codes_id, key, value)
        else:
            if isinstance(value, str):
                value = value.encode(self.encoding)
            bindings.codes_set(self.codes_id, key, value)

    def message_iterkeys(self, namespace=None):
        # type: (str) -> T.Generator[str, None, None]
        if namespace is not None:
            bnamespace = namespace.encode(self.encoding)  # type: T.Optional[bytes]
        else:
            bnamespace = None
        iterator = bindings.codes_keys_iterator_new(self.codes_id, namespace=bnamespace)
        while bindings.codes_keys_iterator_next(iterator):
            yield bindings.codes_keys_iterator_get_name(iterator).decode(self.encoding)
        bindings.codes_keys_iterator_delete(iterator)

    def __getitem__(self, item):
        # type: (str) -> T.Any
        return self.message_get(item)

    def __setitem__(self, item, value):
        # type: (str, T.Any) -> None
        try:
            return self.message_set(item, value)
        except bindings.EcCodesError as ex:
            if self.errors == 'ignore':
                pass
            elif self.errors == 'raise':
                raise KeyError("failed to set key %r to %r" % (item, value))
            else:
                if ex.code == bindings.lib.GRIB_READ_ONLY:
                    # Very noisy error when trying to set computed keys
                    pass
                else:
                    LOG.warning("failed to set key %r to %r", item, value)

    def __delitem__(self, item):
        raise NotImplementedError

    def __iter__(self):
        # type: () -> T.Generator[str, None, None]
        for key in self.message_iterkeys():
            yield key

    def __len__(self):
        # type: () -> int
        return sum(1 for _ in self)

    def write(self, file):
        bindings.codes_write(self.codes_id, file)


@attr.attrs()
class ComputedKeysMessage(Message):
    """Extension of Message class for adding computed keys."""
    computed_keys = attr.attrib(
        default={},
        type=T.Dict[str, T.Tuple[T.Callable[[Message], T.Any], T.Callable[[Message], T.Any]]],
    )

    def __getitem__(self, item):
        if item in self.computed_keys:
            getter, _ = self.computed_keys[item]
            return getter(self)
        else:
            return super(ComputedKeysMessage, self).__getitem__(item)

    def __iter__(self):
        seen = set()
        for key in super(ComputedKeysMessage, self).__iter__():
            yield key
            seen.add(key)
        for key in self.computed_keys:
            if key not in seen:
                yield key

    def __setitem__(self, item, value):
        if item in self.computed_keys:
            _, setter = self.computed_keys[item]
            return setter(self, value)
        else:
            return super(ComputedKeysMessage, self).__setitem__(item, value)


def make_message_schema(message, schema_keys, log=LOG):
    schema = collections.OrderedDict()
    for key in schema_keys:
        bkey = key.encode(message.encoding)
        try:
            key_type = bindings.codes_get_native_type(message.codes_id, bkey)
        except bindings.EcCodesError as ex:
            if ex.code != bindings.lib.GRIB_NOT_FOUND:  # pragma: no cover
                log.exception("key %r failed", key)
            schema[key] = ()
            continue
        size = bindings.codes_get_size(message.codes_id, bkey)
        if key_type == bindings.CODES_TYPE_STRING:
            length = bindings.codes_get_length(message.codes_id, bkey)
            schema[key] = (key_type, size, length)
        else:
            schema[key] = (key_type, size)
    return schema


@attr.attrs()
class FileStream(collections.Iterable):
    """Iterator-like access to a filestream of Messages."""
    path = attr.attrib(type=str)
    message_class = attr.attrib(default=Message, type=Message, repr=False)
    errors = attr.attrib(
        default='warn',
        validator=attr.validators.in_(['ignore', 'warn', 'raise']),
    )

    def __iter__(self):
        # type: () -> T.Generator[Message, None, None]
        with open(self.path, 'rb') as file:
            valid_grib_message_found = False
            while True:
                try:
                    yield self.message_from_file(file, errors=self.errors)
                    valid_grib_message_found = True
                except EOFError:
                    if not valid_grib_message_found:
                        raise EOFError("No valid GRIB message found in file: %r" % self.path)
                    break
                except Exception:
                    if self.errors == 'ignore':
                        pass
                    elif self.errors == 'raise':
                        raise
                    else:
                        LOG.exception("skipping corrupted Message")

    def message_from_file(self, file, offset=None, **kwargs):
        return self.message_class.from_file(file=file, offset=offset, **kwargs)

    def first(self):
        # type: () -> Message
        return next(iter(self))

    def index(self, index_keys, indexpath='{path}.{short_hash}.idx'):
        # type: (T.List[str], str) -> FileIndex
        return FileIndex.from_indexpath_or_filestream(self, index_keys, indexpath)


@contextlib.contextmanager
def compat_create_exclusive(path, *args, **kwargs):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with io.open(fd, mode='wb', *args, **kwargs) as file:
        try:
            yield file
        except Exception:
            os.unlink(path)
            raise


@attr.attrs()
class FileIndex(collections.Mapping):
    filestream = attr.attrib(type=FileStream)
    index_keys = attr.attrib(type=T.List[str])
    offsets = attr.attrib(repr=False, type=T.List[T.Tuple[T.Tuple[T.Any, ...], T.List[int]]])

    @classmethod
    def from_filestream(cls, filestream, index_keys):
        # FIXME: using `Message.message_get` with an explicit message schema was a significant
        #   optimization at some point, due to less calls to the slow CFFI ABI interface.
        #   This doesn't appear to be reproducible at the moment so the optimisation is
        #   disabled and we may choose to remove `make_message_schema` altogether.
        schema = make_message_schema(filestream.first(), index_keys)
        offsets = collections.OrderedDict()
        for message in filestream:
            header_values = []
            for key, args in schema.items():
                try:
                    # if args and not key == 'time':
                    #     value = message.message_get(key, *args)
                    # else:
                    value = message[key]
                except:
                    value = 'undef'
                if isinstance(value, list):
                    value = tuple(value)
                header_values.append(value)
            offset = message.message_get('offset', bindings.CODES_TYPE_LONG)
            offsets.setdefault(tuple(header_values), []).append(offset)
        return cls(filestream=filestream, index_keys=index_keys, offsets=list(offsets.items()))

    @classmethod
    def from_indexpath(cls, indexpath):
        with io.open(indexpath, 'rb') as file:
            return pickle.load(file)

    @classmethod
    def from_indexpath_or_filestream(
            cls, filestream, index_keys, indexpath='{path}.{short_hash}.idx', log=LOG,
    ):
        # type: (FileStream, T.List[str], str, logging.Logger) -> FileIndex

        # Reading and writing the index can be explicitly suppressed by passing indexpath==''.
        if not indexpath:
            return cls.from_filestream(filestream, index_keys)

        hash = hashlib.md5(repr(index_keys).encode('utf-8')).hexdigest()
        indexpath = indexpath.format(path=filestream.path, hash=hash, short_hash=hash[:5])
        try:
            with compat_create_exclusive(indexpath) as new_index_file:
                self = cls.from_filestream(filestream, index_keys)
                pickle.dump(self, new_index_file)
                return self
        except FileExistsError:
            pass
        except Exception:
            log.exception("Can't create file %r", indexpath)

        try:
            index_mtime = os.path.getmtime(indexpath)
            filestream_mtime = os.path.getmtime(filestream.path)
            if index_mtime >= filestream_mtime:
                self = cls.from_indexpath(indexpath)
                if getattr(self, 'index_keys', None) == index_keys and \
                        getattr(self, 'filestream', None) == filestream:
                    return self
                else:
                    log.warning("Ignoring index file %r incompatible with GRIB file", indexpath)
            else:
                log.warning("Ignoring index file %r older than GRIB file", indexpath)
        except Exception:
            log.exception("Can't read index file %r", indexpath)

        return cls.from_filestream(filestream, index_keys)

    def __iter__(self):
        return iter(self.index_keys)

    def __len__(self):
        return len(self.index_keys)

    @property
    def header_values(self):
        if not hasattr(self, '_header_values'):
            self._header_values = {}
            for header_values, _ in self.offsets:
                for i, value in enumerate(header_values):
                    values = self._header_values.setdefault(self.index_keys[i], [])
                    if value not in values:
                        values.append(value)
        return self._header_values

    def __getitem__(self, item):
        # type: (str) -> list
        return self.header_values[item]

    def getone(self, item):
        values = self[item]
        if len(values) != 1:
            raise ValueError("not one value for %r: %r" % (item, len(values)))
        return values[0]

    def subindex(self, filter_by_keys={}, **query):
        query.update(filter_by_keys)
        raw_query = [(self.index_keys.index(k), v) for k, v in query.items()]
        offsets = []
        for header_values, offsets_values in self.offsets:
            for idx, val in raw_query:
                if header_values[idx] != val:
                    break
            else:
                offsets.append((header_values, offsets_values))
        return type(self)(filestream=self.filestream, index_keys=self.index_keys, offsets=offsets)

    def first(self):
        with open(self.filestream.path) as file:
            first_offset = self.offsets[0][1][0]
            return self.filestream.message_from_file(file, offset=first_offset)
